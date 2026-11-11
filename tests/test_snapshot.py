"""Snapshot: full board state at one revision (stage-2 resync payload)."""

from conftest import auth_headers, create_item, make_board, register


def test_snapshot_contains_board_members_items(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    create_item(client, alice["access_token"], board["id"], text="A")
    create_item(client, bob["access_token"], board["id"], text="B", type="rect")

    resp = client.get(
        f"/boards/{board['id']}/snapshot", headers=auth_headers(bob["access_token"])
    )
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["board"]["revision"] == 2
    assert snap["board"]["role"] == "editor"
    assert {m["username"] for m in snap["members"]} == {"alice", "bob"}
    assert [i["text"] for i in snap["items"]] == ["A", "B"]


def test_snapshot_empty_board(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    snap = client.get(
        f"/boards/{board['id']}/snapshot", headers=auth_headers(alice["access_token"])
    ).json()
    assert snap["board"]["revision"] == 0
    assert snap["items"] == [] and len(snap["members"]) == 1


def test_snapshot_non_member_404(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    assert client.get(
        f"/boards/{board['id']}/snapshot", headers=auth_headers(bob["access_token"])
    ).status_code == 404
