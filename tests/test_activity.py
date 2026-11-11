"""Stage 4 tests: activity feed.

Covers recording (every mutation kind, same-transaction guarantee
via the failed-mutation case), the paginated REST endpoint + access
rules, the 500-entry retention cap, live ``activity`` WebSocket
broadcasts for both REST and WS mutations, and the board-delete
cascade.
"""

from __future__ import annotations

from app import activity, database
from tests.conftest import auth_headers, create_item, make_board, register
from tests.test_websocket import _CREATE, _connect, _op, _setup_board


def _feed(client, token, board_id, **params):
    return client.get(
        f"/boards/{board_id}/activity",
        params=params,
        headers=auth_headers(token),
    )


def _kinds(entries):
    return [e["kind"] for e in entries]


# --- Recording: REST mutations ---------------------------------------------

def test_board_create_records_entry(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    resp = _feed(client, alice["access_token"], board["id"])
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "board_created"
    assert entry["actor"]["username"] == "alice"
    assert entry["detail"]["title"] == "Launch plan"
    assert entry["board_id"] == board["id"]
    assert entry["revision"] == 0


def test_item_lifecycle_recorded(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    bid = board["id"]
    resp = create_item(client, alice["access_token"], bid)
    assert resp.status_code == 201
    item = resp.json()
    resp = client.patch(
        f"/boards/{bid}/items/{item['id']}",
        json={"text": "changed"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200
    resp = client.delete(
        f"/boards/{bid}/items/{item['id']}",
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 204

    entries = _feed(client, alice["access_token"], bid).json()
    assert _kinds(entries) == [
        "item_deleted", "item_updated", "item_created", "board_created",
    ]
    created = entries[2]
    assert created["actor"]["username"] == "alice"
    assert created["detail"]["item_id"] == item["id"]
    assert created["detail"]["item_type"] == "note"
    assert created["detail"]["text"] == "Hello"
    assert created["revision"] == 1
    updated = entries[1]
    assert updated["detail"]["fields"] == ["text"]
    assert updated["revision"] == 2
    deleted = entries[0]
    assert deleted["detail"]["item_id"] == item["id"]
    assert deleted["detail"]["item_type"] == "note"
    assert deleted["revision"] == 3


def test_member_events_recorded(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    bid = board["id"]
    resp = client.post(
        f"/boards/{bid}/members",
        json={"username": "bob", "role": "viewer"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 201
    resp = client.patch(
        f"/boards/{bid}/members/{bob['user']['id']}",
        json={"role": "editor"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200
    resp = client.delete(
        f"/boards/{bid}/members/{bob['user']['id']}",
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 204

    entries = _feed(client, alice["access_token"], bid).json()
    assert _kinds(entries) == [
        "member_removed", "member_role_changed", "member_added", "board_created",
    ]
    assert entries[2]["detail"] == {
        "username": "bob", "display_name": "bob", "role": "viewer",
    }
    assert entries[1]["detail"]["from_role"] == "viewer"
    assert entries[1]["detail"]["to_role"] == "editor"
    assert entries[0]["detail"]["username"] == "bob"


def test_board_update_recorded(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    resp = client.patch(
        f"/boards/{board['id']}",
        json={"title": "Renamed"},
        headers=auth_headers(alice["access_token"]),
    )
    assert resp.status_code == 200
    entries = _feed(client, alice["access_token"], board["id"]).json()
    assert _kinds(entries) == ["board_updated", "board_created"]
    assert entries[0]["detail"]["title"] == "Renamed"
    assert entries[0]["detail"]["fields"] == ["title"]
    assert entries[0]["revision"] == 1


def test_failed_mutation_leaves_no_entry(client):
    """Same-transaction rule: a rejected edit writes no feed row."""
    alice, bob, carol, bid = _setup_board(client)
    before = _feed(client, alice["access_token"], bid).json()
    # carol is a viewer: item creation must fail with 403.
    resp = create_item(client, carol["access_token"], bid)
    assert resp.status_code == 403
    after = _feed(client, alice["access_token"], bid).json()
    assert after == before


# --- Access + pagination ----------------------------------------------------

def test_feed_requires_membership(client):
    alice = register(client, "alice")
    bob = register(client, "bob")
    board = make_board(client, alice["access_token"])
    assert _feed(client, bob["access_token"], board["id"]).status_code == 404
    assert client.get(f"/boards/{board['id']}/activity").status_code == 401
    assert _feed(client, alice["access_token"], 999999).status_code == 404


def test_pagination_limit_and_before_id(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    bid = board["id"]
    for i in range(5):
        resp = create_item(client, alice["access_token"], bid, text=f"item {i}")
        assert resp.status_code == 201

    page1 = _feed(client, alice["access_token"], bid, limit=3).json()
    assert len(page1) == 3
    assert _kinds(page1) == ["item_created"] * 3
    assert page1[0]["detail"]["text"] == "item 4"  # newest first
    assert page1[0]["id"] > page1[1]["id"] > page1[2]["id"]

    page2 = _feed(
        client, alice["access_token"], bid, limit=3, before_id=page1[-1]["id"]
    ).json()
    assert len(page2) == 3
    assert _kinds(page2) == ["item_created", "item_created", "board_created"]
    assert {e["id"] for e in page1}.isdisjoint({e["id"] for e in page2})

    # limit is clamped to 1..100
    assert len(_feed(client, alice["access_token"], bid, limit=0).json()) == 1
    assert len(_feed(client, alice["access_token"], bid, limit=500).json()) == 6


def test_retention_prunes_old_entries(tmp_path):
    """Direct unit test of the 500-per-board cap (audit_logging skill:
    retention policy enforced, not just documented)."""
    conn = database.connect(tmp_path / "ret.db")
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash)"
        " VALUES ('alice', 'Alice', 'x')"
    )
    conn.execute("INSERT INTO boards (owner_id, title) VALUES (1, 'B')")
    conn.commit()
    for i in range(activity.ACTIVITY_RETENTION + 5):
        activity.record(conn, 1, 1, "item_created", {"item_id": i})
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    assert count == activity.ACTIVITY_RETENTION
    oldest = conn.execute(
        "SELECT detail FROM activity ORDER BY id ASC LIMIT 1"
    ).fetchone()[0]
    assert '"item_id": 5' in oldest  # the first five were pruned
    conn.close()


def test_unknown_kind_rejected(tmp_path):
    conn = database.connect(tmp_path / "k.db")
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash)"
        " VALUES ('alice', 'Alice', 'x')"
    )
    conn.execute("INSERT INTO boards (owner_id, title) VALUES (1, 'B')")
    conn.commit()
    try:
        activity.record(conn, 1, 1, "not_a_kind", {})
    except ValueError:
        pass
    else:  # pragma: no cover - the record must raise
        raise AssertionError("unknown kind was accepted")
    conn.close()


# --- Live broadcast + cascade ------------------------------------------------

def test_ws_op_records_activity_and_broadcasts(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, bob["access_token"]) as (bws, _bw):
            join = aws.recv()  # bob's presence join
            assert join["type"] == "presence"
            bws.send_json(_op("op-1", 0, "create", item=_CREATE))
            op_msg = bws.recv()
            assert op_msg["type"] == "op" and op_msg["revision"] == 1
            activity_msg = bws.recv()
            assert activity_msg["type"] == "activity"
            assert activity_msg["activity"]["kind"] == "item_created"
            assert activity_msg["activity"]["actor"]["username"] == "bob"
            # The other member gets the same live entry.
            aws.recv()  # the op broadcast
            live = aws.recv()
            assert live["type"] == "activity"
            assert live["activity"]["id"] == activity_msg["activity"]["id"]
    entries = _feed(client, alice["access_token"], bid).json()
    assert entries[0]["kind"] == "item_created"
    assert entries[0]["actor"]["username"] == "bob"


def test_rest_mutation_broadcasts_activity_to_live_socket(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        resp = create_item(client, bob["access_token"], bid)
        assert resp.status_code == 201
        op_msg = aws.recv()
        assert op_msg["type"] == "op"
        assert op_msg["op_id"].startswith("rest-")
        live = aws.recv()
        assert live["type"] == "activity"
        assert live["activity"]["kind"] == "item_created"
        assert live["activity"]["detail"]["item_id"] == resp.json()["id"]


def test_board_delete_cascades_feed(client):
    alice = register(client, "alice")
    board = make_board(client, alice["access_token"])
    assert len(_feed(client, alice["access_token"], board["id"]).json()) == 1
    resp = client.delete(
        f"/boards/{board['id']}", headers=auth_headers(alice["access_token"])
    )
    assert resp.status_code == 204
    assert _feed(client, alice["access_token"], board["id"]).status_code == 404
