"""Stage 2 tests: WebSocket live sync, presence, conflict handling.

Covers the full protocol: join/welcome + presence, op broadcast
(including the sender echo), revision-based stale-op rejection,
viewer read-only enforcement, auth/close codes, cursor relay,
REST->WS broadcast, reconnect resync, role-change/kick/delete
notifications, and malformed-message resilience.
"""

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager

import pytest
from fastapi.websockets import WebSocketDisconnect

from tests.conftest import auth_headers, create_item, make_board, register


def _ws_url(board_id: int, token: str) -> str:
    return f"/ws/boards/{board_id}?token={token}"


class _Socket:
    """A test WebSocket with ONE background reader thread.

    Every received message goes through a single pump into a queue,
    so ``recv`` and ``assert_no_message`` can never steal each
    other's messages (a naive per-call receiver thread would).
    """

    def __init__(self, ws):
        self._ws = ws
        self._q: queue.Queue = queue.Queue()
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        while True:
            try:
                self._q.put(("ok", self._ws.receive_json()))
            except Exception as exc:  # noqa: BLE001 - re-raised on recv()
                self._q.put(("err", exc))
                return

    def send_json(self, data):
        self._ws.send_json(data)

    def send_text(self, text):
        self._ws.send_text(text)

    def recv(self, timeout: float = 5.0) -> dict:
        """Receive one JSON message, failing instead of hanging forever."""
        kind, value = self._q.get(timeout=timeout)
        if kind == "err":
            raise value
        return value

    def assert_no_message(self, timeout: float = 1.0) -> None:
        """Fail if any message arrives within ``timeout`` seconds."""
        try:
            kind, value = self._q.get(timeout=timeout)
        except queue.Empty:
            return
        raise AssertionError(f"unexpected message arrived: {kind} {value!r}")


@contextmanager
def _connect(client, board_id, token):
    """Open a socket and yield (socket, welcome message)."""
    with client.websocket_connect(_ws_url(board_id, token)) as ws:
        sock = _Socket(ws)
        yield sock, sock.recv()


def _setup_board(client):
    """alice (owner) + bob (editor) + carol (viewer) on one board."""
    alice = register(client, "alice")
    bob = register(client, "bob")
    carol = register(client, "carol")
    board = make_board(client, alice["access_token"])
    bid = board["id"]
    for username, role in (("bob", "editor"), ("carol", "viewer")):
        resp = client.post(
            f"/boards/{bid}/members",
            json={"username": username, "role": role},
            headers=auth_headers(alice["access_token"]),
        )
        assert resp.status_code == 201, resp.text
    return alice, bob, carol, bid


def _op(op_id, base_revision, kind, **kwargs):
    payload = {"kind": kind, **kwargs}
    return {"type": "op", "op_id": op_id, "base_revision": base_revision, "op": payload}


_CREATE = {
    "type": "note", "x": 100, "y": 200, "w": 240, "h": 160,
    "text": "Hello", "color": "#ffd166", "z": 0,
}


def _expect_activity(sock, kind):
    """Consume the live ``activity`` broadcast that follows every
    committed op (stage 4) and assert its kind."""
    msg = sock.recv()
    assert msg["type"] == "activity", msg
    assert msg["activity"]["kind"] == kind, msg
    return msg["activity"]


# --- Join / presence --------------------------------------------------------

def test_join_welcome_and_presence(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, welcome):
        assert welcome["type"] == "welcome"
        assert welcome["board_id"] == bid
        assert welcome["revision"] == 0
        assert welcome["role"] == "owner"
        assert [p["username"] for p in welcome["presence"]] == ["alice"]
        with _connect(client, bid, bob["access_token"]) as (bws, bwelcome):
            assert bwelcome["role"] == "editor"
            assert {p["username"] for p in bwelcome["presence"]} == {"alice", "bob"}
            join = aws.recv()
            assert join["type"] == "presence"
            assert join["event"] == "join"
            assert join["user"]["username"] == "bob"
            assert {p["username"] for p in join["presence"]} == {"alice", "bob"}
        # bob's disconnect announces a leave to alice
        leave = aws.recv()
        assert leave["type"] == "presence"
        assert leave["event"] == "leave"
        assert leave["user"]["username"] == "bob"
        assert [p["username"] for p in leave["presence"]] == ["alice"]


def test_second_tab_does_not_reannounce(client):
    alice, _bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, alice["access_token"]) as (_aws2, welcome2):
            assert [p["username"] for p in welcome2["presence"]] == ["alice"]
        # no second join event for the same user
        aws.assert_no_message()


# --- Ops: broadcast, echo, stale --------------------------------------------

def test_op_broadcast_and_echo(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, bob["access_token"]) as (bws, _w2):
            join = aws.recv()  # bob's join event lands before any op
            assert join["type"] == "presence" and join["event"] == "join"
            aws.send_json(_op("op-1", 0, "create", item=_CREATE))
            # sender gets the echo, the other member gets the broadcast
            echo = aws.recv()
            broadcast = bws.recv()
            assert echo == broadcast
            assert echo["type"] == "op"
            assert echo["op_id"] == "op-1"
            assert echo["revision"] == 1
            assert echo["by"]["username"] == "alice"
            assert echo["op"]["kind"] == "create"
            assert echo["op"]["item"]["text"] == "Hello"
            assert echo["op"]["item"]["id"] == 1
            # stage 4: the committed op also broadcasts a feed entry
            _expect_activity(aws, "item_created")
            _expect_activity(bws, "item_created")
            # the item is really in the DB
            snap = client.get(
                f"/boards/{bid}/snapshot", headers=auth_headers(alice["access_token"])
            )
            assert snap.status_code == 200
            assert snap.json()["board"]["revision"] == 1
            assert len(snap.json()["items"]) == 1


def test_op_update_and_delete(client):
    alice, _bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        aws.send_json(_op("op-1", 0, "create", item=_CREATE))
        assert aws.recv()["revision"] == 1
        _expect_activity(aws, "item_created")
        aws.send_json(_op("op-2", 1, "update", item_id=1, patch={"x": 300, "text": "Moved"}))
        upd = aws.recv()
        assert upd["revision"] == 2
        assert upd["op"]["kind"] == "update"
        assert upd["op"]["item"]["x"] == 300
        assert upd["op"]["item"]["text"] == "Moved"
        _expect_activity(aws, "item_updated")
        aws.send_json(_op("op-3", 2, "delete", item_id=1))
        dele = aws.recv()
        assert dele["revision"] == 3
        assert dele["op"] == {"kind": "delete", "item_id": 1}
        _expect_activity(aws, "item_deleted")


def test_stale_op_rejected_and_nothing_broadcast(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, bob["access_token"]) as (bws, _w2):
            join = aws.recv()  # bob's join event lands before any op
            assert join["type"] == "presence" and join["event"] == "join"
            aws.send_json(_op("op-1", 0, "create", item=_CREATE))
            assert aws.recv()["revision"] == 1
            assert bws.recv()["revision"] == 1
            _expect_activity(aws, "item_created")
            _expect_activity(bws, "item_created")
            # bob's op is based on the old revision 0 -> stale
            bws.send_json(_op("op-2", 0, "create", item={**_CREATE, "text": "Late"}))
            stale = bws.recv()
            assert stale["type"] == "stale"
            assert stale["op_id"] == "op-2"
            assert stale["current_revision"] == 1
            # nobody else heard about it, and the DB is unchanged
            aws.assert_no_message()
            snap = client.get(
                f"/boards/{bid}/snapshot", headers=auth_headers(alice["access_token"])
            )
            assert len(snap.json()["items"]) == 1
            assert snap.json()["items"][0]["text"] == "Hello"
            # retry on the fresh revision works
            bws.send_json(_op("op-3", 1, "create", item={**_CREATE, "text": "Fresh"}))
            ok = bws.recv()
            assert ok["type"] == "op" and ok["revision"] == 2
            assert aws.recv()["op"]["item"]["text"] == "Fresh"


def test_concurrent_same_revision_op_loses(client):
    """Two ops at the same base revision: first wins, second is stale."""
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, bob["access_token"]) as (bws, _w2):
            join = aws.recv()  # bob's join event lands before any op
            assert join["type"] == "presence" and join["event"] == "join"
            aws.send_json(_op("op-1", 0, "create", item=_CREATE))
            assert aws.recv()["revision"] == 1
            assert bws.recv()["revision"] == 1
            _expect_activity(aws, "item_created")
            _expect_activity(bws, "item_created")
            # both now race from revision 1; alice's lands first
            aws.send_json(_op("op-2", 1, "update", item_id=1, patch={"x": 111}))
            assert aws.recv()["revision"] == 2
            assert bws.recv()["revision"] == 2
            _expect_activity(aws, "item_updated")
            _expect_activity(bws, "item_updated")
            bws.send_json(_op("op-3", 1, "update", item_id=1, patch={"x": 222}))
            stale = bws.recv()
            assert stale["type"] == "stale"
            assert stale["current_revision"] == 2
            snap = client.get(
                f"/boards/{bid}/snapshot", headers=auth_headers(alice["access_token"])
            )
            assert snap.json()["items"][0]["x"] == 111


# --- Permissions ------------------------------------------------------------

def test_viewer_can_join_but_not_edit(client):
    alice, _bob, carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, carol["access_token"]) as (cws, welcome):
            assert welcome["role"] == "viewer"
            join = aws.recv()  # carol's join event lands before any op
            assert join["type"] == "presence" and join["event"] == "join"
            cws.send_json(_op("op-1", 0, "create", item=_CREATE))
            err = cws.recv()
            assert err["type"] == "error"
            assert "viewer" in err["detail"]
            aws.assert_no_message()
            # the viewer still receives other members' ops
            aws.send_json(_op("op-2", 0, "create", item=_CREATE))
            assert cws.recv()["type"] == "op"


def test_nonmember_bad_token_and_missing_token_rejected(client):
    alice, _bob, _carol, bid = _setup_board(client)
    outsider = register(client, "outsider")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_ws_url(bid, outsider["access_token"])):
            pass
    assert exc.value.code == 4404
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_ws_url(bid, "garbage.token.here")):
            pass
    assert exc.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/boards/{bid}"):
            pass
    assert exc.value.code == 4401
    # and a board that does not exist at all is the same 4404 (no leak)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_ws_url(9999, alice["access_token"])):
            pass
    assert exc.value.code == 4404


# --- Cursor / ping / malformed ----------------------------------------------

def test_cursor_relayed_to_others_only(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, bob["access_token"]) as (bws, _w2):
            join = aws.recv()  # bob's join event lands before any cursor
            assert join["type"] == "presence" and join["event"] == "join"
            aws.send_json({"type": "cursor", "x": 123.5, "y": 456.0})
            cur = bws.recv()
            assert cur["type"] == "cursor"
            assert cur["user_id"] == 1
            assert cur["username"] == "alice"
            assert cur["x"] == 123.5 and cur["y"] == 456.0
            aws.assert_no_message()  # never echoed back to the sender


def test_ping_pong_and_malformed_message(client):
    alice, _bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        aws.send_text("this is not json")
        err = aws.recv()
        assert err["type"] == "error"
        aws.send_json({"type": "nonsense"})
        err = aws.recv()
        assert err["type"] == "error"
        # the socket is still alive after malformed frames
        aws.send_json({"type": "ping"})
        assert aws.recv() == {"type": "pong"}


# --- REST <-> WS interop ------------------------------------------------------

def test_rest_create_broadcast_to_ws(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, bob["access_token"]) as (bws, _w):
        resp = create_item(client, alice["access_token"], bid)
        assert resp.status_code == 201, resp.text
        msg = bws.recv()
        assert msg["type"] == "op"
        assert msg["op_id"].startswith("rest-")
        assert msg["revision"] == 1
        assert msg["by"]["username"] == "alice"
        assert msg["op"]["kind"] == "create"
        assert msg["op"]["item"]["id"] == resp.json()["id"]


def test_rest_update_and_delete_broadcast(client):
    alice, bob, _carol, bid = _setup_board(client)
    item = create_item(client, alice["access_token"], bid).json()
    with _connect(client, bid, bob["access_token"]) as (bws, _w):
        resp = client.patch(
            f"/boards/{bid}/items/{item['id']}",
            json={"text": "Edited via REST"},
            headers=auth_headers(alice["access_token"]),
        )
        assert resp.status_code == 200
        upd = bws.recv()
        assert upd["op"]["kind"] == "update"
        assert upd["op"]["item"]["text"] == "Edited via REST"
        assert upd["revision"] == 2
        _expect_activity(bws, "item_updated")
        resp = client.delete(
            f"/boards/{bid}/items/{item['id']}",
            headers=auth_headers(alice["access_token"]),
        )
        assert resp.status_code == 204
        dele = bws.recv()
        assert dele["op"] == {"kind": "delete", "item_id": item["id"]}
        assert dele["revision"] == 3


# --- Reconnect / resync ------------------------------------------------------

def test_reconnect_gets_fresh_revision_and_snapshot(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        aws.send_json(_op("op-1", 0, "create", item=_CREATE))
        assert aws.recv()["revision"] == 1
    # bob joins late: welcome already carries revision 1, and the
    # snapshot endpoint agrees — the resync contract holds.
    with _connect(client, bid, bob["access_token"]) as (_bws, welcome):
        assert welcome["revision"] == 1
    snap = client.get(
        f"/boards/{bid}/snapshot", headers=auth_headers(bob["access_token"])
    )
    assert snap.json()["board"]["revision"] == 1
    assert len(snap.json()["items"]) == 1


# --- Membership changes while connected --------------------------------------

def test_role_change_notified_and_enforced(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, bob["access_token"]) as (bws, _w):
        resp = client.patch(
            f"/boards/{bid}/members/2",
            json={"role": "viewer"},
            headers=auth_headers(alice["access_token"]),
        )
        assert resp.status_code == 200, resp.text
        notice = bws.recv()
        assert notice["type"] == "role_changed"
        assert notice["role"] == "viewer"
        _expect_activity(bws, "member_role_changed")
        # the demotion takes effect immediately on the live socket
        bws.send_json(_op("op-1", 0, "create", item=_CREATE))
        err = bws.recv()
        assert err["type"] == "error"
        assert "viewer" in err["detail"]


def test_member_removed_kicks_socket(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, bob["access_token"]) as (bws, _w):
        resp = client.delete(
            f"/boards/{bid}/members/2", headers=auth_headers(alice["access_token"])
        )
        assert resp.status_code == 204, resp.text
        notice = bws.recv()
        assert notice["type"] == "member_removed"
        with pytest.raises(WebSocketDisconnect) as exc:
            bws.recv()
        assert exc.value.code == 4404


def test_board_delete_closes_sockets(client):
    alice, bob, _carol, bid = _setup_board(client)
    with _connect(client, bid, alice["access_token"]) as (aws, _w):
        with _connect(client, bid, bob["access_token"]) as (bws, _w2):
            join = aws.recv()  # bob's join event lands before the delete
            assert join["type"] == "presence" and join["event"] == "join"
            resp = client.delete(
                f"/boards/{bid}", headers=auth_headers(alice["access_token"])
            )
            assert resp.status_code == 204, resp.text
            assert aws.recv()["type"] == "board_deleted"
            assert bws.recv()["type"] == "board_deleted"
            with pytest.raises(WebSocketDisconnect):
                aws.recv()
            with pytest.raises(WebSocketDisconnect):
                bws.recv()
