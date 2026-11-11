"""Members: invites, role changes, removal, owner protection."""

from conftest import auth_headers, make_board, register


def _board_with_editor(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    return alice, bob, board


def test_add_member_as_editor(client):
    alice, bob, board = _board_with_editor(client)
    resp = client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "editor"
    assert resp.json()["username"] == "bob"


def test_add_member_unknown_user_404(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    resp = client.post(
        f"/boards/{board['id']}/members",
        json={"username": "ghost", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 404


def test_add_member_twice_conflicts(client):
    alice, bob, board = _board_with_editor(client)
    headers = auth_headers(alice["access_token"])
    client.post(f"/boards/{board['id']}/members", json={"username": "bob", "role": "editor"}, headers=headers)
    resp = client.post(f"/boards/{board['id']}/members", json={"username": "bob", "role": "viewer"}, headers=headers)
    assert resp.status_code == 409


def test_add_second_owner_rejected(client):
    alice, bob, board = _board_with_editor(client)
    resp = client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "owner"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 400


def test_add_member_by_non_owner_forbidden(client):
    alice, bob, board = _board_with_editor(client)
    carol = register(client, "carol")
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    resp = client.post(
        f"/boards/{board['id']}/members",
        json={"username": "carol", "role": "viewer"},
        headers=auth_headers(bob["access_token"]),
    )
    assert resp.status_code == 403


def test_list_members(client):
    alice, bob, board = _board_with_editor(client)
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "viewer"},
        headers=auth_headers(alice["access_token"]),
    )
    resp = client.get(f"/boards/{board['id']}/members", headers=auth_headers(bob["access_token"]))
    assert resp.status_code == 200
    roles = {m["username"]: m["role"] for m in resp.json()}
    assert roles == {"alice": "owner", "bob": "viewer"}


def test_update_member_role(client):
    alice, bob, board = _board_with_editor(client)
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "viewer"},
        headers=auth_headers(alice["access_token"]),
    )
    resp = client.patch(
        f"/boards/{board['id']}/members/{bob['user']['id']}",
        json={"role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "editor"


def test_owner_cannot_be_demoted(client):
    alice, bob, board = _board_with_editor(client)
    resp = client.patch(
        f"/boards/{board['id']}/members/{alice['user']['id']}",
        json={"role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 400


def test_remove_member(client):
    alice, bob, board = _board_with_editor(client)
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    resp = client.delete(
        f"/boards/{board['id']}/members/{bob['user']['id']}",
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 204
    assert client.get(f"/boards/{board['id']}", headers=auth_headers(bob["access_token"])).status_code == 404


def test_owner_cannot_be_removed(client):
    alice, bob, board = _board_with_editor(client)
    resp = client.delete(
        f"/boards/{board['id']}/members/{alice['user']['id']}",
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 400
