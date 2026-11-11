"""Pydantic request/response models.

Validation rules are the first line of defence for a collaborative
canvas: geometry is clamped to the board's coordinate space
(0..CANVAS_MAX on each axis, positive sizes) so a bad client can never
push an item off-canvas or break the stage-2 sync protocol with
nonsense coordinates.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

CANVAS_MAX = 10_000.0
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

Role = Literal["owner", "editor", "viewer"]
ItemType = Literal["note", "rect", "text", "circle"]


# --- Auth -----------------------------------------------------------------

class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=64)


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# --- Boards ---------------------------------------------------------------

class BoardCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class BoardUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class BoardOut(BaseModel):
    id: int
    owner_id: int
    title: str
    description: str
    revision: int
    role: str  # the caller's role on this board
    created_at: str
    updated_at: str


class MemberOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    role: Role


class MemberAdd(BaseModel):
    username: str
    role: Role = "editor"


class MemberUpdate(BaseModel):
    role: Role


# --- Items ----------------------------------------------------------------

class ItemCreate(BaseModel):
    type: ItemType
    x: float = Field(ge=0, le=CANVAS_MAX)
    y: float = Field(ge=0, le=CANVAS_MAX)
    w: float = Field(gt=0, le=CANVAS_MAX)
    h: float = Field(gt=0, le=CANVAS_MAX)
    text: str = Field(default="", max_length=5000)
    color: str = "#ffd166"
    z: int = 0

    @field_validator("color")
    @classmethod
    def _color_hex(cls, v: str) -> str:
        if not HEX_COLOR.match(v):
            raise ValueError("color must be a #rrggbb hex value")
        return v.lower()


class ItemUpdate(BaseModel):
    x: float | None = Field(default=None, ge=0, le=CANVAS_MAX)
    y: float | None = Field(default=None, ge=0, le=CANVAS_MAX)
    w: float | None = Field(default=None, gt=0, le=CANVAS_MAX)
    h: float | None = Field(default=None, gt=0, le=CANVAS_MAX)
    text: str | None = Field(default=None, max_length=5000)
    color: str | None = None
    z: int | None = None

    @field_validator("color")
    @classmethod
    def _color_hex(cls, v: str | None) -> str | None:
        if v is not None and not HEX_COLOR.match(v):
            raise ValueError("color must be a #rrggbb hex value")
        return v.lower() if v is not None else None


class ItemOut(BaseModel):
    id: int
    board_id: int
    type: ItemType
    x: float
    y: float
    w: float
    h: float
    text: str
    color: str
    z: int
    created_by: int
    created_at: str
    updated_at: str


class SnapshotOut(BaseModel):
    """Full board state at one revision — the resync payload for stage 2.

    A client that reconnects (or joins late) fetches the snapshot and
    then applies only ops with a higher revision, so it can never miss
    an edit or apply one twice.
    """

    board: BoardOut
    members: list[MemberOut]
    items: list[ItemOut]


# --- Activity feed (stage 4) ----------------------------------------------

class ActivityActor(BaseModel):
    user_id: int
    username: str
    display_name: str


class ActivityOut(BaseModel):
    """One feed entry: who did what, when, at which board revision.

    ``detail`` is a small JSON object whose keys depend on ``kind``
    (item id/type + short text snippet for item events, username +
    role for member events, title/fields for board events) — never
    secrets, never full item bodies.
    """

    id: int
    board_id: int
    kind: str
    actor: ActivityActor
    detail: dict
    revision: int | None
    created_at: str
