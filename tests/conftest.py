"""Shared fixtures: a throwaway SQLite DB per test + TestClient.

The app's ``get_db`` dependency is overridden to return the test
connection, so no file on disk is touched and tests never share state.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database, rooms
from app.main import app, get_db


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.db"

    def _override_db():
        conn = database.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    # The WebSocket handlers open their own connections (one per op, in
    # a worker thread) — they must hit the same throwaway DB, so the
    # path lives on app.state (see app/main.py).
    app.state.db_path = db_path
    rooms.rooms.reset()
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    rooms.rooms.reset()
    app.state.db_path = database.DEFAULT_DB_PATH


def register(client, username, password="password123", display_name=""):
    resp = client.post(
        "/auth/register",
        json={"username": username, "password": password, "display_name": display_name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_board(client, token, title="Launch plan"):
    resp = client.post(
        "/boards", json={"title": title}, headers=auth_headers(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_item(client, token, board_id, **overrides):
    payload = {
        "type": "note", "x": 100, "y": 200, "w": 240, "h": 160, "text": "Hello",
        **overrides,
    }
    return client.post(
        f"/boards/{board_id}/items", json=payload, headers=auth_headers(token)
    )
