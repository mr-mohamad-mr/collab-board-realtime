"""WebSocket sync protocol: op messages, conflict handling, rate limits.

Wire protocol (JSON text frames; the full contract is in the README):

Client -> server:
- ``{"type": "op", "op_id": "<client id>", "base_revision": N,
  "op": {"kind": "create", "item": {...}} | {"kind": "update",
  "item_id": 7, "patch": {...}} | {"kind": "delete", "item_id": 7}}``
- ``{"type": "cursor", "x": 123.5, "y": 456.0}`` — ephemeral, not stored
- ``{"type": "ping"}``

Server -> client:
- ``welcome`` (on join: current revision + role + presence list)
- ``op`` (a committed op, broadcast to the whole room including the
  sender — the sender matches on ``op_id``; ``revision`` is the new
  board revision after the op)
- ``stale`` (the op's ``base_revision`` is behind the board: the
  client must resync via ``GET /boards/{id}/snapshot`` and retry)
- ``error`` (validation / permission failure; the socket stays open)
- ``presence`` (join/leave + full presence list)
- ``cursor`` (another member's pointer, relayed, never stored)
- ``pong`` | ``role_changed`` | ``member_removed`` | ``board_deleted``
  | ``board_updated``

Conflict rule (the stage-1 design): an op is applied only when its
``base_revision`` equals the board's current revision. The first op
at a revision wins and bumps the revision; every concurrent op at the
same base revision is rejected as stale. Nothing is ever silently
overwritten — a rejected client resyncs and retries on the fresh
state. SQLite serialises the writes, so the check-and-bump is atomic
inside one connection/transaction.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import activity, database, schemas

EDIT_ROLES = {"owner", "editor"}

# Abuse guards (websocket_security skill: rate limiting + size cap).
MAX_MESSAGE_BYTES = 64_000
MAX_OPS_PER_WINDOW = 30
OP_WINDOW_SECONDS = 10.0
MAX_CURSORS_PER_SECOND = 20


class OpError(Exception):
    """A client op that cannot be applied (validation / permission)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotMemberError(OpError):
    """The socket's user is no longer a member of the board."""


class StaleOpError(OpError):
    """The op's base_revision is behind the board's current revision."""

    def __init__(self, current_revision: int) -> None:
        super().__init__(f"stale base_revision; current revision is {current_revision}")
        self.current_revision = current_revision


# --- Wire models ----------------------------------------------------------

class OpPayload(BaseModel):
    kind: Literal["create", "update", "delete"]
    item: schemas.ItemCreate | None = None
    item_id: int | None = None
    patch: schemas.ItemUpdate | None = None

    @model_validator(mode="after")
    def _fields_for_kind(self) -> "OpPayload":
        if self.kind == "create" and self.item is None:
            raise ValueError("create op needs 'item'")
        if self.kind == "update" and (self.item_id is None or self.patch is None):
            raise ValueError("update op needs 'item_id' and 'patch'")
        if self.kind == "delete" and self.item_id is None:
            raise ValueError("delete op needs 'item_id'")
        return self


class ClientMessage(BaseModel):
    type: Literal["op", "cursor", "ping"]
    op_id: str = Field(default="", max_length=64)
    base_revision: int | None = None
    op: OpPayload | None = None
    x: float | None = None
    y: float | None = None

    @model_validator(mode="after")
    def _fields_for_type(self) -> "ClientMessage":
        if self.type == "op":
            if self.op is None or self.base_revision is None:
                raise ValueError("op message needs 'op' and 'base_revision'")
            if not self.op_id:
                raise ValueError("op message needs 'op_id'")
        if self.type == "cursor":
            if self.x is None or self.y is None:
                raise ValueError("cursor message needs 'x' and 'y'")
            if not (0 <= self.x <= schemas.CANVAS_MAX) or not (
                0 <= self.y <= schemas.CANVAS_MAX
            ):
                raise ValueError("cursor coordinates out of canvas bounds")
        return self


# --- Op application -------------------------------------------------------

def apply_op(
    conn: sqlite3.Connection, board_id: int, user_id: int, msg: ClientMessage
) -> dict:
    """Apply one client op inside a transaction.

    The caller's role is re-read from ``board_members`` on every op,
    so a role change (or removal) takes effect immediately, exactly
    like the REST endpoints. Returns ``{"revision": int, "op": {...},
    "activity": {...}}`` describing the committed op for broadcast
    (the ``activity`` entry is recorded in the same transaction, so a
    broadcast op always has a matching feed entry). Raises ``OpError``,
    ``NotMemberError`` or ``StaleOpError``; on any raise the caller
    must not broadcast and the transaction is left uncommitted.
    """
    member = conn.execute(
        "SELECT * FROM board_members WHERE board_id = ? AND user_id = ?",
        (board_id, user_id),
    ).fetchone()
    if member is None:
        raise NotMemberError("no longer a member of this board")
    if member["role"] not in EDIT_ROLES:
        raise OpError("insufficient role: viewers cannot edit")
    board = conn.execute(
        "SELECT * FROM boards WHERE id = ?", (board_id,)
    ).fetchone()
    if board is None:
        raise OpError("board not found")
    if msg.base_revision != board["revision"]:
        raise StaleOpError(board["revision"])

    op = msg.op
    assert op is not None  # validated by ClientMessage
    if op.kind == "create":
        item = op.item
        assert item is not None
        cur = conn.execute(
            "INSERT INTO items (board_id, type, x, y, w, h, text, color, z, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                board_id, item.type, item.x, item.y, item.w, item.h,
                item.text, item.color, item.z, user_id,
            ),
        )
        revision = database.bump_revision(conn, board_id)
        entry = activity.record(
            conn, board_id, user_id, "item_created",
            {
                "item_id": cur.lastrowid,
                "item_type": item.type,
                "text": (item.text or "")[:80],
            },
            revision=revision,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM items WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return {
            "revision": revision,
            "op": {"kind": "create", "item": dict(row)},
            "activity": entry,
        }

    if op.kind == "update":
        assert op.item_id is not None and op.patch is not None
        row = conn.execute(
            "SELECT * FROM items WHERE id = ? AND board_id = ?",
            (op.item_id, board_id),
        ).fetchone()
        if row is None:
            raise OpError("item not found")
        patch = op.patch.model_dump(exclude_unset=True)
        if not patch:
            raise OpError("empty patch")
        sets = ", ".join(f"{key} = ?" for key in patch)
        conn.execute(
            f"UPDATE items SET {sets}, updated_at = datetime('now')"
            " WHERE id = ? AND board_id = ?",
            (*patch.values(), op.item_id, board_id),
        )
        revision = database.bump_revision(conn, board_id)
        entry = activity.record(
            conn, board_id, user_id, "item_updated",
            {
                "item_id": op.item_id,
                "item_type": row["type"],
                "fields": sorted(patch),
            },
            revision=revision,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM items WHERE id = ?", (op.item_id,)
        ).fetchone()
        return {
            "revision": revision,
            "op": {"kind": "update", "item": dict(row)},
            "activity": entry,
        }

    # delete
    assert op.item_id is not None
    row = conn.execute(
        "SELECT * FROM items WHERE id = ? AND board_id = ?",
        (op.item_id, board_id),
    ).fetchone()
    if row is None:
        raise OpError("item not found")
    conn.execute("DELETE FROM items WHERE id = ?", (op.item_id,))
    revision = database.bump_revision(conn, board_id)
    entry = activity.record(
        conn, board_id, user_id, "item_deleted",
        {"item_id": op.item_id, "item_type": row["type"]},
        revision=revision,
    )
    conn.commit()
    return {
        "revision": revision,
        "op": {"kind": "delete", "item_id": op.item_id},
        "activity": entry,
    }


# --- Rate limiting --------------------------------------------------------

class RateLimiter:
    """Tiny sliding-window limiter (per socket, in-memory).

    ``allow()`` returns False once more than ``max_events`` arrived
    inside the last ``window_seconds``; callers decide the response
    (drop a cursor silently, reject + close an op flood).
    """

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._times: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._times and self._times[0] <= now - self.window:
            self._times.popleft()
        if len(self._times) >= self.max_events:
            return False
        self._times.append(now)
        return True
