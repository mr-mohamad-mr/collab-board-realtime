"""Live smoke test for stage 2 (WebSocket live sync).

Starts the real server on 127.0.0.1:8142 with a throwaway DB, then
drives it with real ``websockets`` clients (not TestClient):

1. alice + bob register, alice creates a board, adds bob as editor
2. both connect; welcome + presence join verified
3. alice creates an item over WS -> bob receives the op broadcast
   (stage 4: each also receives the live ``activity`` entry)
4. bob sends a stale op (old base_revision) -> gets ``stale``
5. bob sends a cursor -> alice receives it
6. alice creates an item over REST -> bob receives the op broadcast
7. bob disconnects -> alice receives a presence leave
8. snapshot revision matches the op count

Usage: .venv/bin/python scripts/ws_smoke.py   (exit 0 = all checks pass)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx
from websockets.asyncio.client import connect as ws_connect

PORT = 8142
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent.parent


def _wait_health(timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("server did not become healthy")


async def _recv(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def main() -> int:
    db_path = Path(tempfile.mkdtemp(prefix="collab-smoke-")) / "smoke.db"
    env = dict(os.environ, COLLAB_DB_PATH=str(db_path))
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(PORT),
        ],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_health()
        # trust_env=False: the sandbox proxy env vars break localhost
        # URLs (same quirk scripts/seed_demo.py works around).
        async with httpx.AsyncClient(base_url=BASE, trust_env=False) as http:
            async def _reg(username):
                r = await http.post(
                    "/auth/register",
                    json={"username": username, "password": "password123"},
                )
                assert r.status_code == 201, r.text
                return r.json()

            alice = await _reg("alice")
            bob = await _reg("bob")
            print("PASS register alice + bob")

            r = await http.post(
                "/boards", json={"title": "Smoke board"},
                headers={"Authorization": f"Bearer {alice['access_token']}"},
            )
            assert r.status_code == 201, r.text
            board_id = r.json()["id"]
            r = await http.post(
                f"/boards/{board_id}/members",
                json={"username": "bob", "role": "editor"},
                headers={"Authorization": f"Bearer {alice['access_token']}"},
            )
            assert r.status_code == 201, r.text
            print("PASS board created, bob added as editor")

            item = {
                "type": "note", "x": 100, "y": 200, "w": 240, "h": 160,
                "text": "Smoke note", "color": "#ffd166", "z": 0,
            }
            async with ws_connect(
                f"ws://127.0.0.1:{PORT}/ws/boards/{board_id}"
                f"?token={alice['access_token']}"
            ) as aws, ws_connect(
                f"ws://127.0.0.1:{PORT}/ws/boards/{board_id}"
                f"?token={bob['access_token']}"
            ) as bws:
                welcome_a = await _recv(aws)
                assert welcome_a["type"] == "welcome" and welcome_a["revision"] == 0
                welcome_b = await _recv(bws)
                assert {p["username"] for p in welcome_b["presence"]} == {"alice", "bob"}
                join = await _recv(aws)
                assert join["type"] == "presence" and join["event"] == "join"
                assert join["user"]["username"] == "bob"
                print("PASS welcome + presence join")

                await aws.send(json.dumps({
                    "type": "op", "op_id": "smoke-1", "base_revision": 0,
                    "op": {"kind": "create", "item": item},
                }))
                echo = await _recv(aws)
                broadcast = await _recv(bws)
                assert echo == broadcast and echo["revision"] == 1
                assert echo["op"]["item"]["text"] == "Smoke note"
                print("PASS WS op broadcast + sender echo (revision 1)")
                # stage 4: the committed op also broadcasts a feed entry
                act_a = await _recv(aws)
                act_b = await _recv(bws)
                assert act_a["type"] == "activity"
                assert act_a["activity"]["kind"] == "item_created"
                assert act_a["activity"]["id"] == act_b["activity"]["id"]
                print("PASS live activity broadcast after WS op")

                await bws.send(json.dumps({
                    "type": "op", "op_id": "smoke-2", "base_revision": 0,
                    "op": {"kind": "create", "item": {**item, "text": "Late"}},
                }))
                stale = await _recv(bws)
                assert stale["type"] == "stale" and stale["current_revision"] == 1
                print("PASS stale op rejected with current_revision 1")

                await bws.send(json.dumps({"type": "cursor", "x": 50, "y": 60}))
                cur = await _recv(aws)
                assert cur["type"] == "cursor" and cur["username"] == "bob"
                print("PASS cursor relayed to the other member")

                r = await http.post(
                    f"/boards/{board_id}/items", json=item,
                    headers={"Authorization": f"Bearer {alice['access_token']}"},
                )
                assert r.status_code == 201, r.text
                rest_op = await _recv(bws)
                assert rest_op["type"] == "op" and rest_op["revision"] == 2
                assert rest_op["op_id"].startswith("rest-")
                print("PASS REST create broadcast to live WS clients (revision 2)")

            # bob's socket closed on context exit -> alice should see a leave
            async with ws_connect(
                f"ws://127.0.0.1:{PORT}/ws/boards/{board_id}"
                f"?token={alice['access_token']}"
            ) as aws2:
                welcome = await _recv(aws2)
                assert [p["username"] for p in welcome["presence"]] == ["alice"]
                print("PASS reconnect: presence shows only alice after bob left")

            r = await http.get(
                f"/boards/{board_id}/snapshot",
                headers={"Authorization": f"Bearer {alice['access_token']}"},
            )
            snap = r.json()
            assert snap["board"]["revision"] == 2, snap
            assert len(snap["items"]) == 2, snap
            print("PASS snapshot revision 2, 2 items — resync contract holds")
        print("SMOKE OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
