"""Live frontend smoke test (stage 3).

Boots the real server on a throwaway DB and checks, end to end:
- ``GET /`` serves the app shell and every asset it references
- every JS file parses cleanly (``node --check``)
- the API + WebSocket still work behind the shell: register, board,
  item, and a live WS op round-trip

Usage: ``.venv/bin/python scripts/frontend_smoke.py`` (from the project root).
Exits 0 on SMOKE OK, 1 on any failure. The real collab.db is untouched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 8143
BASE = f"http://127.0.0.1:{PORT}"


def check(condition: bool, label: str) -> None:
    print(("  ok  " if condition else " FAIL ") + label)
    if not condition:
        raise SystemExit(1)


def get(path: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post(path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_fd, db_path = tempfile.mkstemp(prefix="collab-frontend-smoke-", suffix=".db")
    os.close(db_fd)
    os.unlink(db_path)
    env = dict(os.environ, COLLAB_DB_PATH=db_path)
    venv_python = os.path.join(here, ".venv", "bin", "python")
    proc = subprocess.Popen(
        [venv_python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=here, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the server.
        for _ in range(50):
            try:
                status, _ = get("/health")
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            print(" FAIL server did not start")
            return 1

        # 1. App shell + referenced assets.
        status, html = get("/")
        check(status == 200 and "board-canvas" in html, "GET / serves the app shell")
        refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html)))
        check(bool(refs), f"shell references {len(refs)} static assets")
        for ref in refs:
            status, body = get(ref)
            check(status == 200 and len(body) > 0, f"GET {ref} -> 200")

        # 2. JS parses cleanly (node --check on each file).
        for rel in ["js/api.js", "js/sync.js", "js/board.js", "js/app.js"]:
            path = os.path.join(here, "app", "static", rel)
            result = subprocess.run(
                ["node", "--check", path], capture_output=True, text=True
            )
            check(result.returncode == 0, f"node --check {rel}")

        # 3. API still works behind the shell: register + board + item.
        status, reg = post("/auth/register", {
            "username": "smoke", "password": "password123", "display_name": "Smoke",
        })
        check(status == 201, "register via live server")
        token = reg["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        status, board = post("/boards", {"title": "Smoke board"}, auth)
        check(status == 201, "create board via live server")
        status, item = post(f"/boards/{board['id']}/items", {
            "type": "note", "x": 100, "y": 100, "w": 240, "h": 160, "text": "hi",
        }, auth)
        check(status == 201 and item["text"] == "hi", "create item via live server")
        status, snap = get(f"/boards/{board['id']}/snapshot", auth)
        snap_obj = json.loads(snap) if status == 200 else {}
        check(status == 200 and snap_obj.get("board", {}).get("revision") == 1,
              "snapshot revision 1")

        # 4. Live WS op round-trip against the same server.
        import asyncio
        import websockets

        async def ws_roundtrip():
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT}/ws/boards/{board['id']}?token={token}"
            ) as ws:
                welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert welcome["type"] == "welcome" and welcome["revision"] == 1
                await ws.send(json.dumps({
                    "type": "op", "op_id": "smoke-1", "base_revision": 1,
                    "op": {"kind": "create", "item": {
                        "type": "rect", "x": 400, "y": 100, "w": 200, "h": 120,
                        "text": "", "color": "#8ecae6", "z": 0,
                    }},
                }))
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert msg["type"] == "op" and msg["revision"] == 2, msg
                return True

        check(asyncio.run(ws_roundtrip()), "WS op round-trip (revision 1 -> 2)")
        print("SMOKE OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    sys.exit(main())
