"""Frontend serving tests (stage 3).

The app shell is a static single-page app: ``GET /`` serves the
HTML shell and ``/static/*`` its CSS/JS. These tests check the shell
is served, every asset it references resolves, and that adding the
static routes did not change API auth behaviour (no login is needed
to load the shell, but the API still requires a token).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"


def test_root_serves_app_shell(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Collab Board" in resp.text
    assert 'id="board-canvas"' in resp.text
    assert 'id="view-auth"' in resp.text
    assert 'id="view-boards"' in resp.text
    assert 'id="view-board"' in resp.text
    # Stage 4: activity feed panel in the board view.
    assert 'id="activity-list"' in resp.text
    assert 'id="activity-more"' in resp.text


def test_referenced_assets_all_resolve(client):
    """Every local <script>/<link> in index.html must return 200."""
    html = client.get("/").text
    refs = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html))
    assert refs, "index.html references no /static assets"
    for ref in sorted(refs):
        resp = client.get(ref)
        assert resp.status_code == 200, ref
        assert len(resp.content) > 0, ref


def test_static_assets_exist_on_disk():
    expected = [
        "index.html",
        "css/app.css",
        "js/api.js",
        "js/sync.js",
        "js/board.js",
        "js/app.js",
    ]
    for rel in expected:
        path = STATIC_DIR / rel
        assert path.is_file(), rel
        assert path.stat().st_size > 0, rel


def test_static_js_defines_expected_globals(client):
    """Each JS module must expose the namespace the shell relies on."""
    checks = {
        "/static/js/api.js": "CollabApi",
        "/static/js/sync.js": "CollabSync",
        "/static/js/board.js": "CollabBoardView",
    }
    for path, name in checks.items():
        assert name in client.get(path).text, path
    # Stage 4: the API client and sync client know the activity feed.
    assert "listActivity" in client.get("/static/js/api.js").text
    assert "activity" in client.get("/static/js/sync.js").text
    assert "activity-list" in client.get("/static/js/app.js").text
    assert "activity-list" in client.get("/static/css/app.css").text


def test_api_still_requires_auth(client):
    # Serving the shell must not have opened the API up.
    assert client.get("/boards").status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_static_cannot_escape_directory(client):
    resp = client.get("/static/../main.py")
    assert resp.status_code in (400, 403, 404)


@pytest.mark.parametrize(
    "path",
    ["/static/js/api.js", "/static/js/sync.js", "/static/js/board.js", "/static/js/app.js"],
)
def test_js_files_have_no_node_requires(client, path):
    """Vanilla browser JS: no CommonJS/Node imports that would break in a browser."""
    text = client.get(path).text
    assert "require(" not in text
    assert "module.exports" not in text
