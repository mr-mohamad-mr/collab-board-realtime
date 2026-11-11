"""SQLite persistence layer.

One connection per request (FastAPI dependency), WAL mode, foreign keys
enforced, every query parameterised, schema created idempotently.

Access control (see `websocket_realtime_builder` skill: validate access
before joining a room — here, before any board read/write): a user can
only touch a board they are a member of. Membership lives in
``board_members`` with a role (``owner`` / ``editor`` / ``viewer``); the
application layer checks it on every endpoint — SQLite has no row-level
security, so the application check IS the isolation boundary. Board-first
composite indexes keep those checks fast.

``boards.revision`` is a monotonic counter bumped on every mutation of
the board or its items. Stage 2 (live sync) uses it as the ordering
primitive: clients send the revision their edit is based on, and the
server rejects stale edits so conflicts are detected, never silently
overwritten.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(
    os.environ.get("COLLAB_DB_PATH", "")
    or (Path(__file__).resolve().parent.parent / "collab.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS boards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS board_members (
    board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (board_id, user_id)
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('note', 'rect', 'text', 'circle')),
    x REAL NOT NULL,
    y REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#ffd166',
    z INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_boards_owner ON boards(owner_id);
CREATE INDEX IF NOT EXISTS idx_members_user ON board_members(user_id, board_id);
CREATE INDEX IF NOT EXISTS idx_members_board ON board_members(board_id, user_id);
CREATE INDEX IF NOT EXISTS idx_items_board ON items(board_id, z, id);

-- Stage 4: per-board activity feed. One row per mutation, written in
-- the same transaction as the mutation (see app/activity.py). The
-- board FK cascades: deleting a board deletes its feed with it.
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    revision INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_board ON activity(board_id, id DESC);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def bump_revision(conn: sqlite3.Connection, board_id: int) -> int:
    """Atomically bump a board's revision; returns the new value."""
    conn.execute(
        "UPDATE boards SET revision = revision + 1,"
        " updated_at = datetime('now') WHERE id = ?",
        (board_id,),
    )
    row = conn.execute(
        "SELECT revision FROM boards WHERE id = ?", (board_id,)
    ).fetchone()
    return row["revision"]
