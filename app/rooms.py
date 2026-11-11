"""In-memory board rooms: live sockets + presence.

One room per board: the set of connected sockets and the presence
list (one entry per connected user, with a connection count so a
second tab from the same user does not double-announce them).

Single-process by design (one uvicorn worker). A multi-worker deploy
would move this state to Redis pub/sub — see the README scaling note.

Threading rule: ALL room state mutates only on the app's event loop,
so no lock is needed. The WebSocket handler (async) calls the
``async`` methods directly. REST handlers are sync ``def`` running in
a threadpool, so they never touch room state directly — they use the
``*_sync`` wrappers, which schedule the work on the loop with
``loop.call_soon_threadsafe``. The loop is installed by the app
lifespan (``app/main.py``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from fastapi import WebSocket

# Close codes (4000-4999 are application-defined per RFC 6455).
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404


@dataclass
class PresenceEntry:
    user_id: int
    username: str
    display_name: str
    role: str
    connections: int = 0

    def public(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
        }


@dataclass
class BoardRoom:
    sockets: set[WebSocket] = field(default_factory=set)
    # socket -> user_id, so role changes / kicks can find a user's sockets
    socket_user: dict[WebSocket, int] = field(default_factory=dict)
    presence: dict[int, PresenceEntry] = field(default_factory=dict)


class RoomManager:
    def __init__(self) -> None:
        self._rooms: dict[int, BoardRoom] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Installed by the app lifespan; the loop room work runs on."""
        self._loop = loop

    def reset(self) -> None:
        """Drop all rooms (tests only — each test gets a fresh DB)."""
        self._rooms.clear()

    # -- state helpers (loop thread only) ---------------------------------

    def _room(self, board_id: int) -> BoardRoom:
        return self._rooms.setdefault(board_id, BoardRoom())

    def presence_list(self, board_id: int) -> list[dict]:
        room = self._rooms.get(board_id)
        if room is None:
            return []
        return [entry.public() for entry in room.presence.values()]

    # -- join / leave (called from the async WebSocket handler) -----------

    async def join(
        self, board_id: int, ws: WebSocket, user: dict, role: str
    ) -> list[dict]:
        """Register a socket; returns the current presence list.

        Broadcasts a ``presence`` join event to the other members only
        when this is the user's first connection to the board (a second
        tab does not re-announce them).
        """
        room = self._room(board_id)
        room.sockets.add(ws)
        room.socket_user[ws] = user["id"]
        entry = room.presence.get(user["id"])
        newly_present = entry is None
        if newly_present:
            entry = PresenceEntry(
                user_id=user["id"],
                username=user["username"],
                display_name=user["display_name"],
                role=role,
            )
            room.presence[user["id"]] = entry
        entry.connections += 1
        if newly_present:
            await self.broadcast(
                board_id,
                {
                    "type": "presence",
                    "event": "join",
                    "user": entry.public(),
                    "presence": self.presence_list(board_id),
                },
                exclude=ws,
            )
        return self.presence_list(board_id)

    async def leave(self, board_id: int, ws: WebSocket, user: dict) -> None:
        room = self._rooms.get(board_id)
        if room is None:
            return
        room.sockets.discard(ws)
        room.socket_user.pop(ws, None)
        entry = room.presence.get(user["id"])
        if entry is not None:
            entry.connections -= 1
            if entry.connections <= 0:
                del room.presence[user["id"]]
                await self.broadcast(
                    board_id,
                    {
                        "type": "presence",
                        "event": "leave",
                        "user": entry.public(),
                        "presence": self.presence_list(board_id),
                    },
                )
        if not room.sockets and not room.presence:
            self._rooms.pop(board_id, None)

    # -- broadcast (loop thread only) --------------------------------------

    async def broadcast(
        self, board_id: int, message: dict, exclude: WebSocket | None = None
    ) -> None:
        """Send a JSON message to every socket in the room.

        Best-effort per socket: a dead socket is dropped, never allowed
        to break delivery to the rest of the room.
        """
        room = self._rooms.get(board_id)
        if room is None:
            return
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(room.sockets):
            if ws is exclude:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            room.sockets.discard(ws)
            room.socket_user.pop(ws, None)

    async def send_to_user(self, board_id: int, user_id: int, message: dict) -> None:
        """Send a JSON message to all of one user's sockets in a room."""
        room = self._rooms.get(board_id)
        if room is None:
            return
        payload = json.dumps(message)
        for ws in list(room.sockets):
            if room.socket_user.get(ws) == user_id:
                try:
                    await ws.send_text(payload)
                except Exception:
                    room.sockets.discard(ws)
                    room.socket_user.pop(ws, None)

    async def close_user_sockets(
        self, board_id: int, user_id: int, message: dict, code: int
    ) -> None:
        """Tell one user's sockets why they are going, then close them.

        The sockets leave the room BEFORE anything is sent: every
        send here awaits (yielding the loop), so a broadcast scheduled
        after this one — e.g. the stage-4 activity entry for the
        removal — could otherwise interleave and deliver a message to
        a socket that is about to close. Dropping them from the room
        first makes that impossible.
        """
        room = self._rooms.get(board_id)
        if room is None:
            return
        targets = [
            ws for ws in list(room.sockets)
            if room.socket_user.get(ws) == user_id
        ]
        for ws in targets:
            room.sockets.discard(ws)
            room.socket_user.pop(ws, None)
        payload = json.dumps(message)
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                pass
        for ws in targets:
            try:
                await ws.close(code=code)
            except Exception:
                pass

    async def close_board(self, board_id: int, message: dict) -> None:
        """Tell every socket in the room the board is gone, close them,
        and drop the room.

        The room is dropped first, for the same reason as
        ``close_user_sockets``: a later-scheduled broadcast (e.g. the
        stage-4 activity entry) must not deliver to closing sockets.
        """
        room = self._rooms.pop(board_id, None)
        if room is None:
            return
        payload = json.dumps(message)
        for ws in list(room.sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                pass
        for ws in list(room.sockets):
            try:
                await ws.close(code=1000, reason="board deleted")
            except Exception:
                pass

    # -- sync wrappers (called from sync REST handlers) --------------------

    def _schedule(self, coro) -> None:
        """Schedule a coroutine on the app loop from any thread.

        No-op when no loop is installed yet (e.g. before lifespan runs)
        — REST writes still commit; live clients simply resync on
        their next op via the stale-op path.
        """
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(coro)
        )

    def broadcast_sync(self, board_id: int, message: dict) -> None:
        self._schedule(self.broadcast(board_id, message))

    def notify_role_change_sync(
        self, board_id: int, user_id: int, new_role: str
    ) -> None:
        self._schedule(
            self.send_to_user(
                board_id,
                user_id,
                {"type": "role_changed", "board_id": board_id, "role": new_role},
            )
        )

    def kick_sync(self, board_id: int, user_id: int) -> None:
        self._schedule(
            self.close_user_sockets(
                board_id,
                user_id,
                {"type": "member_removed", "board_id": board_id},
                CLOSE_NOT_FOUND,
            )
        )

    def notify_board_deleted_sync(self, board_id: int) -> None:
        self._schedule(
            self.close_board(
                board_id, {"type": "board_deleted", "board_id": board_id}
            )
        )


rooms = RoomManager()
