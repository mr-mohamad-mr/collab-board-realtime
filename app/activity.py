"""Board activity feed (stage 4).

Every board mutation — item create/update/delete, board rename,
member add/role change/remove — writes one row to the ``activity``
table **in the same transaction** as the mutation itself, so the feed
can never disagree with the board state: a failed mutation leaves no
entry, and a committed mutation always has one. The entry carries the
board ``revision`` at the time, so the feed lines up with the
snapshot/op stream a client already follows.

Retention (audit_logging skill: define and enforce a retention
policy): at most ``ACTIVITY_RETENTION`` entries are kept per board;
older rows are pruned on insert, so the table cannot grow without
bound on a busy board. Entries never contain secrets — only ids,
usernames, item types and short text snippets.

Live delivery: REST handlers broadcast the new entry as a WebSocket
``{"type": "activity", "activity": {...}}`` message after commit
(see ``app/main.py``); WebSocket ops do the same in
``app/websocket.py``. A client that missed a broadcast just refetches
``GET /boards/{id}/activity`` — the feed is paginated newest-first
with a ``before_id`` cursor.
"""

from __future__ import annotations

import json
import sqlite3

ACTIVITY_RETENTION = 500
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

# Kinds recorded (see the call sites in app/main.py and app/sync.py).
KINDS = frozenset(
    {
        "board_created",
        "board_updated",
        "member_added",
        "member_role_changed",
        "member_removed",
        "item_created",
        "item_updated",
        "item_deleted",
    }
)


def record(
    conn: sqlite3.Connection,
    board_id: int,
    actor_id: int,
    kind: str,
    detail: dict,
    revision: int | None = None,
) -> dict:
    """Insert one activity entry (same transaction as the caller).

    The caller commits; on any later failure of the caller's
    transaction this row rolls back with it. Returns the public
    entry dict (actor resolved to username/display name).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown activity kind: {kind}")
    if revision is None:
        row = conn.execute(
            "SELECT revision FROM boards WHERE id = ?", (board_id,)
        ).fetchone()
        revision = row["revision"] if row is not None else None
    cur = conn.execute(
        "INSERT INTO activity (board_id, actor_id, kind, detail, revision)"
        " VALUES (?, ?, ?, ?, ?)",
        (board_id, actor_id, kind, json.dumps(detail, sort_keys=True), revision),
    )
    # Retention: keep only the newest ACTIVITY_RETENTION rows per board.
    conn.execute(
        "DELETE FROM activity WHERE board_id = ? AND id NOT IN"
        " (SELECT id FROM activity WHERE board_id = ?"
        "  ORDER BY id DESC LIMIT ?)",
        (board_id, board_id, ACTIVITY_RETENTION),
    )
    return _public(conn, cur.lastrowid)


def list_entries(
    conn: sqlite3.Connection,
    board_id: int,
    limit: int = DEFAULT_LIMIT,
    before_id: int | None = None,
) -> list[dict]:
    """Newest-first page of entries; ``before_id`` pages backwards."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    if before_id is None:
        rows = conn.execute(
            "SELECT * FROM activity WHERE board_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (board_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM activity WHERE board_id = ? AND id < ?"
            " ORDER BY id DESC LIMIT ?",
            (board_id, before_id, limit),
        ).fetchall()
    return [_public_row(conn, row) for row in rows]


def _public(conn: sqlite3.Connection, entry_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM activity WHERE id = ?", (entry_id,)
    ).fetchone()
    return _public_row(conn, row)


def _public_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    actor = conn.execute(
        "SELECT id, username, display_name FROM users WHERE id = ?",
        (row["actor_id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "board_id": row["board_id"],
        "kind": row["kind"],
        "actor": (
            {
                "user_id": actor["id"],
                "username": actor["username"],
                "display_name": actor["display_name"],
            }
            if actor is not None
            else {"user_id": row["actor_id"], "username": "?", "display_name": "?"}
        ),
        "detail": json.loads(row["detail"]),
        "revision": row["revision"],
        "created_at": row["created_at"],
    }
