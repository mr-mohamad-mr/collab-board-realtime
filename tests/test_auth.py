"""Auth: registration, login, token handling."""

from conftest import register


def test_register_returns_token_and_user(client):
    body = register(client, "alice", display_name="Alice")
    assert body["user"]["username"] == "alice"
    assert body["user"]["display_name"] == "Alice"
    assert body["token_type"] == "bearer"
    assert len(body["access_token"].split(".")) == 3


def test_register_defaults_display_name_to_username(client):
    body = register(client, "bob")
    assert body["user"]["display_name"] == "bob"


def test_register_duplicate_username_conflicts(client):
    register(client, "alice")
    resp = client.post(
        "/auth/register", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 409


def test_register_short_password_rejected(client):
    resp = client.post(
        "/auth/register", json={"username": "alice", "password": "short"}
    )
    assert resp.status_code == 422


def test_login_round_trip(client):
    register(client, "alice")
    resp = client.post(
        "/auth/login", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "alice"


def test_login_wrong_password_unauthorized(client):
    register(client, "alice")
    resp = client.post(
        "/auth/login", json={"username": "alice", "password": "wrong-password"}
    )
    assert resp.status_code == 401


def test_login_unknown_user_unauthorized(client):
    resp = client.post(
        "/auth/login", json={"username": "ghost", "password": "password123"}
    )
    assert resp.status_code == 401


def test_me_returns_current_user(client):
    body = register(client, "alice")
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


def test_me_without_token_unauthorized(client):
    assert client.get("/auth/me").status_code == 401


def test_me_with_tampered_token_unauthorized(client):
    body = register(client, "alice")
    token = body["access_token"][:-2] + "xx"
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_health_ok(client):
    assert client.get("/health").json() == {"status": "ok"}
