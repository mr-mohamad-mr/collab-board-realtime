"""WebSocket endpoint: ``WS /ws/boards/{board_id}?token=<jwt>``.

Auth happens BEFORE the socket is accepted (websocket_realtime_builder
skill: validate access before joining a room): the JWT is decoded and
board membership is checked in a worker thread with a fresh SQLite
connection, and the socket is closed with an application close code
when auth fails — 4401 bad/missing token, 4404 not a member (the same
code for a missing board, so existence is never leaked, mirroring the
REST 404 rule).

After accept the handler loops on incoming frames:

- ``ping`` -> ``pong``
- ``cursor`` -> relayed to the other room members (rate-limited,
  dropped silently when over the limit; never stored)
- ``op`` -> applied via ``sync.apply_op`` in a worker thread (fresh
  connection per op, mirroring the REST one-connection-per-request
  rule), then broadcast to the whole room including the sender.
  Stale ops get a ``stale`` reply with the current revision; the
  client resyncs via ``GET /boards/{id}/snapshot`` and retries.

DB work never runs on the event loop thread: every op/authorize call
goes through ``anyio.to_thread.run_sync`` with a connection opened
inside the worker thread, so a connection is never shared across
threads.
"""

from __future__ import annotations

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from . import database, rooms, security, sync

router = APIRouter()


def _authorize(db_path, board_id: int, token: str | None) -> tuple[dict, str, int]:
    """Validate token + membership; returns (user, role, board revision)."""
    if not token:
        raise security.AuthError("missing token")
    payload = security.decode_token(token)
    conn = database.connect(db_path)
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?", (payload["sub"],)
        ).fetchone()
        if user is None:
            raise security.AuthError("unknown user")
        member = conn.execute(
            "SELECT * FROM board_members WHERE board_id = ? AND user_id = ?",
            (board_id, user["id"]),
        ).fetchone()
        if member is None:
            raise sync.NotMemberError("not a member of this board")
        board = conn.execute(
            "SELECT revision FROM boards WHERE id = ?", (board_id,)
        ).fetchone()
        if board is None:
            raise sync.NotMemberError("board not found")
        return dict(user), member["role"], board["revision"]
    finally:
        conn.close()


def _apply_op(db_path, board_id: int, user_id: int, msg: sync.ClientMessage) -> dict:
    conn = database.connect(db_path)
    try:
        return sync.apply_op(conn, board_id, user_id, msg)
    finally:
        conn.close()


@router.websocket("/ws/boards/{board_id}")
async def board_socket(websocket: WebSocket, board_id: int) -> None:
    db_path = websocket.scope["app"].state.db_path
    token = websocket.query_params.get("token")
    try:
        user, role, revision = await anyio.to_thread.run_sync(
            _authorize, db_path, board_id, token
        )
    except security.AuthError:
        await websocket.close(code=rooms.CLOSE_UNAUTHORIZED, reason="invalid token")
        return
    except sync.NotMemberError:
        await websocket.close(code=rooms.CLOSE_NOT_FOUND, reason="board not found")
        return

    await websocket.accept()
    presence = await rooms.rooms.join(board_id, websocket, user, role)
    await websocket.send_json(
        {
            "type": "welcome",
            "board_id": board_id,
            "revision": revision,
            "role": role,
            "presence": presence,
        }
    )

    op_limiter = sync.RateLimiter(sync.MAX_OPS_PER_WINDOW, sync.OP_WINDOW_SECONDS)
    cursor_limiter = sync.RateLimiter(sync.MAX_CURSORS_PER_SECOND, 1.0)
    try:
        while True:
            try:
                text = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            if len(text.encode("utf-8")) > sync.MAX_MESSAGE_BYTES:
                await websocket.send_json(
                    {"type": "error", "detail": "message too large"}
                )
                await websocket.close(code=1008, reason="message too large")
                break
            try:
                msg = sync.ClientMessage.model_validate_json(text)
            except (ValidationError, ValueError):
                await websocket.send_json(
                    {"type": "error", "detail": "malformed message"}
                )
                continue
            if msg.type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if msg.type == "cursor":
                if cursor_limiter.allow():
                    await rooms.rooms.broadcast(
                        board_id,
                        {
                            "type": "cursor",
                            "user_id": user["id"],
                            "username": user["username"],
                            "x": msg.x,
                            "y": msg.y,
                        },
                        exclude=websocket,
                    )
                continue
            # --- op ------------------------------------------------------
            if not op_limiter.allow():
                await websocket.send_json(
                    {
                        "type": "error",
                        "op_id": msg.op_id,
                        "detail": "too many ops",
                    }
                )
                await websocket.close(code=1008, reason="rate limit")
                break
            try:
                result = await anyio.to_thread.run_sync(
                    _apply_op, db_path, board_id, user["id"], msg
                )
            except sync.StaleOpError as exc:
                await websocket.send_json(
                    {
                        "type": "stale",
                        "op_id": msg.op_id,
                        "current_revision": exc.current_revision,
                        "hint": "resync via GET /boards/{board_id}/snapshot",
                    }
                )
                continue
            except sync.NotMemberError:
                await websocket.send_json(
                    {
                        "type": "error",
                        "op_id": msg.op_id,
                        "detail": "no longer a member of this board",
                    }
                )
                await websocket.close(
                    code=rooms.CLOSE_NOT_FOUND, reason="no longer a member"
                )
                break
            except sync.OpError as exc:
                await websocket.send_json(
                    {"type": "error", "op_id": msg.op_id, "detail": exc.detail}
                )
                continue
            await rooms.rooms.broadcast(
                board_id,
                {
                    "type": "op",
                    "op_id": msg.op_id,
                    "revision": result["revision"],
                    "by": {
                        "user_id": user["id"],
                        "username": user["username"],
                        "display_name": user["display_name"],
                    },
                    "op": result["op"],
                },
            )
            # Stage 4: the feed entry recorded with the op, live.
            if result.get("activity") is not None:
                await rooms.rooms.broadcast(
                    board_id,
                    {"type": "activity", "activity": result["activity"]},
                )
    finally:
        await rooms.rooms.leave(board_id, websocket, user)
