"""Items: canvas CRUD, geometry validation, role gating, revisions."""

from conftest import auth_headers, make_board, register

ITEM = {"type": "note", "x": 100, "y": 200, "w": 240, "h": 160, "text": "Hello"}


def _setup(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    carol = register(client, "carol")
    board = make_board(client, alice["access_token"])
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "carol", "role": "viewer"},
        headers=auth_headers(alice["access_token"]),
    )
    return alice, bob, carol, board


def _create(client, token, board_id, **overrides):
    payload = {**ITEM, **overrides}
    return client.post(
        f"/boards/{board_id}/items", json=payload, headers=auth_headers(token)
    )


def test_create_item(client):
    alice, _, _, board = _setup(client)
    resp = _create(client, alice["access_token"], board["id"])
    assert resp.status_code == 201
    item = resp.json()
    assert item["type"] == "note" and item["text"] == "Hello"
    assert item["board_id"] == board["id"]
    assert item["created_by"] == alice["user"]["id"]


def test_create_item_bumps_board_revision(client):
    alice, _, _, board = _setup(client)
    _create(client, alice["access_token"], board["id"])
    after = client.get(f"/boards/{board['id']}", headers=auth_headers(alice["access_token"])).json()
    assert after["revision"] == 1


def test_create_item_by_editor_ok(client):
    _, bob, _, board = _setup(client)
    assert _create(client, bob["access_token"], board["id"]).status_code == 201


def test_create_item_by_viewer_forbidden(client):
    _, _, carol, board = _setup(client)
    assert _create(client, carol["access_token"], board["id"]).status_code == 403


def test_create_item_by_non_member_404(client):
    alice, _, _, board = _setup(client)
    dave = register(client, "dave")
    assert _create(client, dave["access_token"], board["id"]).status_code == 404


def test_create_item_invalid_geometry_rejected(client):
    alice, _, _, board = _setup(client)
    assert _create(client, alice["access_token"], board["id"], x=-5).status_code == 422
    assert _create(client, alice["access_token"], board["id"], w=0).status_code == 422
    assert _create(client, alice["access_token"], board["id"], y=10_001).status_code == 422


def test_create_item_invalid_type_and_color_rejected(client):
    alice, _, _, board = _setup(client)
    assert _create(client, alice["access_token"], board["id"], type="triangle").status_code == 422
    assert _create(client, alice["access_token"], board["id"], color="red").status_code == 422
    assert _create(client, alice["access_token"], board["id"], color="#FFD166").status_code == 201


def test_list_items_ordered_by_z(client):
    alice, _, _, board = _setup(client)
    _create(client, alice["access_token"], board["id"], z=5, text="top")
    _create(client, alice["access_token"], board["id"], z=1, text="bottom")
    items = client.get(
        f"/boards/{board['id']}/items", headers=auth_headers(alice["access_token"])
    ).json()
    assert [i["text"] for i in items] == ["bottom", "top"]


def test_update_item_moves_and_retexts(client):
    alice, _, _, board = _setup(client)
    item = _create(client, alice["access_token"], board["id"]).json()
    resp = client.patch(
        f"/boards/{board['id']}/items/{item['id']}",
        json={"x": 300, "text": "Moved"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["x"] == 300 and resp.json()["text"] == "Moved"
    assert resp.json()["y"] == 200  # untouched fields preserved
    after = client.get(f"/boards/{board['id']}", headers=auth_headers(alice["access_token"])).json()
    assert after["revision"] == 2  # create + update


def test_update_item_by_viewer_forbidden(client):
    alice, _, carol, board = _setup(client)
    item = _create(client, alice["access_token"], board["id"]).json()
    resp = client.patch(
        f"/boards/{board['id']}/items/{item['id']}",
        json={"text": "Nope"},
        headers=auth_headers(carol["access_token"]),
    )
    assert resp.status_code == 403


def test_update_missing_item_404(client):
    alice, _, _, board = _setup(client)
    resp = client.patch(
        f"/boards/{board['id']}/items/9999",
        json={"text": "Nope"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 404


def test_delete_item(client):
    alice, _, _, board = _setup(client)
    item = _create(client, alice["access_token"], board["id"]).json()
    resp = client.delete(
        f"/boards/{board['id']}/items/{item['id']}",
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 204
    items = client.get(
        f"/boards/{board['id']}/items", headers=auth_headers(alice["access_token"])
    ).json()
    assert items == []
    after = client.get(f"/boards/{board['id']}", headers=auth_headers(alice["access_token"])).json()
    assert after["revision"] == 2  # create + delete


def test_item_in_other_board_not_visible(client):
    alice, _, _, board = _setup(client)
    other = make_board(client, alice["access_token"], "Other")
    item = _create(client, alice["access_token"], board["id"]).json()
    resp = client.patch(
        f"/boards/{other['id']}/items/{item['id']}",
        json={"text": "Cross-board"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 404
