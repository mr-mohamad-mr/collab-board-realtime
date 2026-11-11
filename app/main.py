"""Collab Board — real-time collaborative boards (flagship 2, stage 1).

Stage 1 scope: architecture + FastAPI backend core — JWT auth, boards
with role-based membership (owner/editor/viewer), canvas items with
geometry validation, and a monotonic per-board ``revision`` counter
that stage 2's WebSocket sync will use for op ordering and conflict
detection.

Stage 3: ``GET /`` serves the vanilla-JS app shell and ``/static/*``
its CSS/JS assets (auth, boards list, canvas board UI, member
management); the API itself is unchanged.

Stage 4: activity feed — every board mutation records an entry in
the same transaction (``app/activity.py``) and broadcasts it live;
``GET /boards/{id}/activity`` serves the paginated feed.

Access rules, enforced on every endpoint:
- Any board read needs membership (any role).
- Item/board edits need ``editor`` or ``owner``; ``viewer`` is read-only.
- Membership changes and board deletion need ``owner``.
- A non-member gets 404, never 403 — board existence is not leaked.
- The board owner can never be demoted or removed (a board always has
  exactly one owner: its creator).
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from . import activity, database, rooms, schemas, security
from .websocket import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Install the event loop the room manager schedules broadcasts on."""
    rooms.rooms.set_loop(asyncio.get_running_loop())
    yield


app = FastAPI(title="Collab Board", version="0.4.0", lifespan=lifespan)

# The SQLite file the WebSocket handlers open their own connections to.
# Tests point it at a throwaway DB (see tests/conftest.py).
app.state.db_path = database.DEFAULT_DB_PATH

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def frontend_index() -> FileResponse:
    """The web app shell (stage 3): auth + boards list + canvas board UI."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(ws_router)

bearer = HTTPBearer(auto_error=False)

EDIT_ROLES = {"owner", "editor"}


# --- Dependencies ---------------------------------------------------------

def get_db() -> Generator[sqlite3.Connection, None, None]:
    """One fresh connection per request (created in the request's own
    thread, closed when the request finishes) — a SQLite connection
    must never be shared across threads."""
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


def get_current_user(
    db: DbDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> sqlite3.Row:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token")
    try:
        payload = security.decode_token(creds.credentials)
    except security.AuthError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (payload["sub"],)
    ).fetchone()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return user


UserDep = Annotated[sqlite3.Row, Depends(get_current_user)]


def board_or_404(db: sqlite3.Connection, board_id: int) -> sqlite3.Row:
    board = db.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    if board is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "board not found")
    return board


def membership_or_404(
    db: sqlite3.Connection, board_id: int, user_id: int
) -> sqlite3.Row:
    """Return the membership row, or 404 (existence not leaked)."""
    board_or_404(db, board_id)
    member = db.execute(
        "SELECT * FROM board_members WHERE board_id = ? AND user_id = ?",
        (board_id, user_id),
    ).fetchone()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "board not found")
    return member


def require_roles(
    db: sqlite3.Connection, board_id: int, user_id: int, roles: set[str]
) -> sqlite3.Row:
    member = membership_or_404(db, board_id, user_id)
    if member["role"] not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
    return member


def board_out(board: sqlite3.Row, role: str) -> schemas.BoardOut:
    return schemas.BoardOut(
        id=board["id"],
        owner_id=board["owner_id"],
        title=board["title"],
        description=board["description"],
        revision=board["revision"],
        role=role,
        created_at=board["created_at"],
        updated_at=board["updated_at"],
    )


def item_out(item: sqlite3.Row) -> schemas.ItemOut:
    return schemas.ItemOut(**dict(item))


def _broadcast_op(
    board_id: int, revision: int, user: sqlite3.Row, op: dict
) -> None:
    """Tell the board's live sockets about a committed REST mutation.

    The payload matches the WebSocket ``op`` message (``op_id`` is
    tagged ``rest-*`` so a client that also sent the change over REST
    can tell it apart from its own WS ops). A WS client that missed
    the broadcast still stays consistent: its next op carries a stale
    ``base_revision`` and is rejected until it resyncs.
    """
    rooms.rooms.broadcast_sync(
        board_id,
        {
            "type": "op",
            "op_id": f"rest-{uuid.uuid4().hex[:12]}",
            "revision": revision,
            "by": {
                "user_id": user["id"],
                "username": user["username"],
                "display_name": user["display_name"],
            },
            "op": op,
        },
    )


def _broadcast_activity(board_id: int, entry: dict) -> None:
    """Push a just-recorded feed entry to the board's live sockets.

    Called after the mutation committed — the entry is already in the
    DB, so a client that missed the broadcast refetches the same
    entry from GET /boards/{id}/activity.
    """
    rooms.rooms.broadcast_sync(
        board_id, {"type": "activity", "activity": entry}
    )


# --- Health / auth --------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(data: schemas.RegisterIn, db: DbDep) -> schemas.TokenOut:
    existing = db.execute(
        "SELECT id FROM users WHERE username = ?", (data.username,)
    ).fetchone()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username taken")
    display = data.display_name.strip() or data.username
    cur = db.execute(
        "INSERT INTO users (username, display_name, password_hash)"
        " VALUES (?, ?, ?)",
        (data.username, display, security.hash_password(data.password)),
    )
    db.commit()
    user = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return schemas.TokenOut(
        access_token=security.create_token(user["id"], user["username"]),
        user=schemas.UserOut(**{k: user[k] for k in ("id", "username", "display_name")}),
    )


@app.post("/auth/login")
def login(data: schemas.LoginIn, db: DbDep) -> schemas.TokenOut:
    user = db.execute(
        "SELECT * FROM users WHERE username = ?", (data.username,)
    ).fetchone()
    if user is None or not security.verify_password(data.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    return schemas.TokenOut(
        access_token=security.create_token(user["id"], user["username"]),
        user=schemas.UserOut(**{k: user[k] for k in ("id", "username", "display_name")}),
    )


@app.get("/auth/me")
def me(user: UserDep) -> schemas.UserOut:
    return schemas.UserOut(**{k: user[k] for k in ("id", "username", "display_name")})


# --- Boards ---------------------------------------------------------------

@app.post("/boards", status_code=status.HTTP_201_CREATED)
def create_board(data: schemas.BoardCreate, db: DbDep, user: UserDep) -> schemas.BoardOut:
    cur = db.execute(
        "INSERT INTO boards (owner_id, title, description) VALUES (?, ?, ?)",
        (user["id"], data.title.strip(), data.description.strip()),
    )
    board_id = cur.lastrowid
    db.execute(
        "INSERT INTO board_members (board_id, user_id, role) VALUES (?, ?, 'owner')",
        (board_id, user["id"]),
    )
    entry = activity.record(
        db, board_id, user["id"], "board_created",
        {"title": data.title.strip()},
    )
    db.commit()
    _broadcast_activity(board_id, entry)
    board = db.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    return board_out(board, "owner")


@app.get("/boards")
def list_boards(db: DbDep, user: UserDep) -> list[schemas.BoardOut]:
    rows = db.execute(
        "SELECT b.*, m.role FROM boards b"
        " JOIN board_members m ON m.board_id = b.id"
        " WHERE m.user_id = ? ORDER BY b.updated_at DESC, b.id DESC",
        (user["id"],),
    ).fetchall()
    return [board_out(row, row["role"]) for row in rows]


@app.get("/boards/{board_id}")
def get_board(board_id: int, db: DbDep, user: UserDep) -> schemas.BoardOut:
    member = membership_or_404(db, board_id, user["id"])
    return board_out(board_or_404(db, board_id), member["role"])


@app.patch("/boards/{board_id}")
def update_board(
    board_id: int, data: schemas.BoardUpdate, db: DbDep, user: UserDep
) -> schemas.BoardOut:
    require_roles(db, board_id, user["id"], {"owner"})
    board = board_or_404(db, board_id)
    title = data.title.strip() if data.title is not None else board["title"]
    description = (
        data.description.strip() if data.description is not None else board["description"]
    )
    db.execute(
        "UPDATE boards SET title = ?, description = ?, revision = revision + 1,"
        " updated_at = datetime('now') WHERE id = ?",
        (title, description, board_id),
    )
    updated = board_out(board_or_404(db, board_id), "owner")
    entry = activity.record(
        db, board_id, user["id"], "board_updated",
        {
            "title": updated.title,
            "fields": sorted(
                f for f, v in (("title", data.title), ("description", data.description))
                if v is not None
            ),
        },
        revision=updated.revision,
    )
    db.commit()
    rooms.rooms.broadcast_sync(
        board_id,
        {
            "type": "board_updated",
            "revision": updated.revision,
            "board": updated.model_dump(),
        },
    )
    _broadcast_activity(board_id, entry)
    return updated


@app.delete("/boards/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_board(board_id: int, db: DbDep, user: UserDep) -> None:
    require_roles(db, board_id, user["id"], {"owner"})
    db.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    db.commit()
    # Live sockets get the news and are closed (see rooms.close_board).
    rooms.rooms.notify_board_deleted_sync(board_id)


# --- Members --------------------------------------------------------------

def _members(db: sqlite3.Connection, board_id: int) -> list[schemas.MemberOut]:
    rows = db.execute(
        "SELECT m.user_id, u.username, u.display_name, m.role"
        " FROM board_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.board_id = ? ORDER BY m.role, u.username",
        (board_id,),
    ).fetchall()
    return [schemas.MemberOut(**dict(row)) for row in rows]


@app.get("/boards/{board_id}/members")
def list_members(board_id: int, db: DbDep, user: UserDep) -> list[schemas.MemberOut]:
    membership_or_404(db, board_id, user["id"])
    return _members(db, board_id)


@app.post("/boards/{board_id}/members", status_code=status.HTTP_201_CREATED)
def add_member(
    board_id: int, data: schemas.MemberAdd, db: DbDep, user: UserDep
) -> schemas.MemberOut:
    require_roles(db, board_id, user["id"], {"owner"})
    target = db.execute(
        "SELECT * FROM users WHERE username = ?", (data.username,)
    ).fetchone()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if data.role == "owner":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "a board has exactly one owner"
        )
    existing = db.execute(
        "SELECT * FROM board_members WHERE board_id = ? AND user_id = ?",
        (board_id, target["id"]),
    ).fetchone()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "already a member")
    db.execute(
        "INSERT INTO board_members (board_id, user_id, role) VALUES (?, ?, ?)",
        (board_id, target["id"], data.role),
    )
    entry = activity.record(
        db, board_id, user["id"], "member_added",
        {
            "username": target["username"],
            "display_name": target["display_name"],
            "role": data.role,
        },
    )
    db.commit()
    _broadcast_activity(board_id, entry)
    return schemas.MemberOut(
        user_id=target["id"],
        username=target["username"],
        display_name=target["display_name"],
        role=data.role,
    )


@app.patch("/boards/{board_id}/members/{member_id}")
def update_member(
    board_id: int,
    member_id: int,
    data: schemas.MemberUpdate,
    db: DbDep,
    user: UserDep,
) -> schemas.MemberOut:
    require_roles(db, board_id, user["id"], {"owner"})
    member = db.execute(
        "SELECT * FROM board_members WHERE board_id = ? AND user_id = ?",
        (board_id, member_id),
    ).fetchone()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    if member["role"] == "owner":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "the board owner cannot be demoted"
        )
    if data.role == "owner":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "a board has exactly one owner"
        )
    db.execute(
        "UPDATE board_members SET role = ? WHERE board_id = ? AND user_id = ?",
        (data.role, board_id, member_id),
    )
    target = db.execute("SELECT * FROM users WHERE id = ?", (member_id,)).fetchone()
    entry = activity.record(
        db, board_id, user["id"], "member_role_changed",
        {
            "username": target["username"],
            "display_name": target["display_name"],
            "from_role": member["role"],
            "to_role": data.role,
        },
    )
    db.commit()
    # The member's live sockets learn their new role immediately.
    rooms.rooms.notify_role_change_sync(board_id, member_id, data.role)
    _broadcast_activity(board_id, entry)
    return schemas.MemberOut(
        user_id=target["id"],
        username=target["username"],
        display_name=target["display_name"],
        role=data.role,
    )


@app.delete("/boards/{board_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    board_id: int, member_id: int, db: DbDep, user: UserDep
) -> None:
    require_roles(db, board_id, user["id"], {"owner"})
    member = db.execute(
        "SELECT * FROM board_members WHERE board_id = ? AND user_id = ?",
        (board_id, member_id),
    ).fetchone()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    if member["role"] == "owner":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "the board owner cannot be removed"
        )
    target = db.execute("SELECT * FROM users WHERE id = ?", (member_id,)).fetchone()
    db.execute(
        "DELETE FROM board_members WHERE board_id = ? AND user_id = ?",
        (board_id, member_id),
    )
    entry = activity.record(
        db, board_id, user["id"], "member_removed",
        {"username": target["username"], "display_name": target["display_name"]},
    )
    db.commit()
    # The removed member's live sockets are told and closed.
    rooms.rooms.kick_sync(board_id, member_id)
    _broadcast_activity(board_id, entry)


# --- Items ----------------------------------------------------------------

@app.post("/boards/{board_id}/items", status_code=status.HTTP_201_CREATED)
def create_item(
    board_id: int, data: schemas.ItemCreate, db: DbDep, user: UserDep
) -> schemas.ItemOut:
    require_roles(db, board_id, user["id"], EDIT_ROLES)
    cur = db.execute(
        "INSERT INTO items (board_id, type, x, y, w, h, text, color, z, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            board_id, data.type, data.x, data.y, data.w, data.h,
            data.text, data.color, data.z, user["id"],
        ),
    )
    revision = database.bump_revision(db, board_id)
    created = item_out(
        db.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
    )
    entry = activity.record(
        db, board_id, user["id"], "item_created",
        {
            "item_id": created.id,
            "item_type": created.type,
            "text": (created.text or "")[:80],
        },
        revision=revision,
    )
    db.commit()
    _broadcast_op(
        board_id, revision, user, {"kind": "create", "item": created.model_dump()}
    )
    _broadcast_activity(board_id, entry)
    return created


@app.get("/boards/{board_id}/items")
def list_items(board_id: int, db: DbDep, user: UserDep) -> list[schemas.ItemOut]:
    membership_or_404(db, board_id, user["id"])
    rows = db.execute(
        "SELECT * FROM items WHERE board_id = ? ORDER BY z, id", (board_id,)
    ).fetchall()
    return [item_out(row) for row in rows]


@app.patch("/boards/{board_id}/items/{item_id}")
def update_item(
    board_id: int,
    item_id: int,
    data: schemas.ItemUpdate,
    db: DbDep,
    user: UserDep,
) -> schemas.ItemOut:
    require_roles(db, board_id, user["id"], EDIT_ROLES)
    item = db.execute(
        "SELECT * FROM items WHERE id = ? AND board_id = ?", (item_id, board_id)
    ).fetchone()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    patch = data.model_dump(exclude_unset=True)
    if not patch:
        return item_out(item)
    sets = ", ".join(f"{key} = ?" for key in patch)
    db.execute(
        f"UPDATE items SET {sets}, updated_at = datetime('now')"
        " WHERE id = ? AND board_id = ?",
        (*patch.values(), item_id, board_id),
    )
    revision = database.bump_revision(db, board_id)
    updated = item_out(
        db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    )
    entry = activity.record(
        db, board_id, user["id"], "item_updated",
        {
            "item_id": item_id,
            "item_type": updated.type,
            "fields": sorted(patch),
        },
        revision=revision,
    )
    db.commit()
    _broadcast_op(
        board_id, revision, user, {"kind": "update", "item": updated.model_dump()}
    )
    _broadcast_activity(board_id, entry)
    return updated


@app.delete("/boards/{board_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(board_id: int, item_id: int, db: DbDep, user: UserDep) -> None:
    require_roles(db, board_id, user["id"], EDIT_ROLES)
    item = db.execute(
        "SELECT * FROM items WHERE id = ? AND board_id = ?", (item_id, board_id)
    ).fetchone()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "item not found")
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    revision = database.bump_revision(db, board_id)
    entry = activity.record(
        db, board_id, user["id"], "item_deleted",
        {"item_id": item_id, "item_type": item["type"]},
        revision=revision,
    )
    db.commit()
    _broadcast_op(board_id, revision, user, {"kind": "delete", "item_id": item_id})
    _broadcast_activity(board_id, entry)


# --- Snapshot (stage-2 resync payload) ------------------------------------

@app.get("/boards/{board_id}/snapshot")
def snapshot(board_id: int, db: DbDep, user: UserDep) -> schemas.SnapshotOut:
    member = membership_or_404(db, board_id, user["id"])
    board = board_or_404(db, board_id)
    items = db.execute(
        "SELECT * FROM items WHERE board_id = ? ORDER BY z, id", (board_id,)
    ).fetchall()
    return schemas.SnapshotOut(
        board=board_out(board, member["role"]),
        members=_members(db, board_id),
        items=[item_out(row) for row in items],
    )


# --- Activity feed (stage 4) ----------------------------------------------

@app.get("/boards/{board_id}/activity")
def list_activity(
    board_id: int,
    db: DbDep,
    user: UserDep,
    limit: int = activity.DEFAULT_LIMIT,
    before_id: int | None = None,
) -> list[schemas.ActivityOut]:
    """Newest-first feed page (``?limit=`` 1–100, ``?before_id=`` to
    page backwards). Members only — a non-member gets 404, like every
    other board read."""
    membership_or_404(db, board_id, user["id"])
    entries = activity.list_entries(db, board_id, limit=limit, before_id=before_id)
    return [schemas.ActivityOut(**entry) for entry in entries]
