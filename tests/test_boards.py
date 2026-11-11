"""Boards: CRUD, listing, and role-gated access."""

from conftest import auth_headers, make_board, register


def test_create_board_makes_creator_owner(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    assert board["title"] == "Launch plan"
    assert board["role"] == "owner"
    assert board["revision"] == 0
    assert board["owner_id"] == alice["user"]["id"]


def test_list_boards_only_shows_memberships(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    make_board(client, alice["access_token"], "Alice board")
    assert len(client.get("/boards", headers=auth_headers(alice["access_token"])).json()) == 1
    assert client.get("/boards", headers=auth_headers(bob["access_token"])).json() == []


def test_get_board_as_member(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    resp = client.get(f"/boards/{board['id']}", headers=auth_headers(alice["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["id"] == board["id"]


def test_get_board_as_non_member_is_404(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    resp = client.get(f"/boards/{board['id']}", headers=auth_headers(bob["access_token"]))
    assert resp.status_code == 404  # existence not leaked


def test_get_missing_board_is_404(client):
    alice = register(client, "alice")
    resp = client.get("/boards/9999", headers=auth_headers(alice["access_token"]))
    assert resp.status_code == 404


def test_update_board_by_owner_bumps_revision(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    resp = client.patch(
        f"/boards/{board['id']}",
        json={"title": "Renamed"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"
    assert resp.json()["revision"] == 1


def test_update_board_by_editor_forbidden(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    resp = client.patch(
        f"/boards/{board['id']}",
        json={"title": "Hijack"},
        headers=auth_headers(bob["access_token"]),
    )
    assert resp.status_code == 403


def test_delete_board_by_owner(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    resp = client.delete(f"/boards/{board['id']}", headers=auth_headers(alice["access_token"]))
    assert resp.status_code == 204
    assert client.get(f"/boards/{board['id']}", headers=auth_headers(alice["access_token"])).status_code == 404


def test_delete_board_by_editor_forbidden(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    resp = client.delete(f"/boards/{board['id']}", headers=auth_headers(bob["access_token"]))
    assert resp.status_code == 403


def test_board_title_validation(client):
    alice = register(client, "alice")
    resp = client.post("/boards", json={"title": ""}, headers=auth_headers(alice["access_token"]))
    assert resp.status_code == 422
