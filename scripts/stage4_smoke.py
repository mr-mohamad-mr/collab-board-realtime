"""Live stage-4 smoke test.

Boots the real server on a throwaway DB and checks, end to end:
- activity feed: REST mutations record entries; the paginated
  endpoint serves them newest-first; a live socket receives the
  ``activity`` broadcast after a WS op
- Docker files: docker-compose.yml parses and names one service with
  a named volume; the Dockerfile pins python:3.12-slim, runs as a
  non-root user, and has a /health HEALTHCHECK
- the seed demo script runs against the live server and leaves a
  non-empty activity feed

Usage: ``.venv/bin/python scripts/stage4_smoke.py`` (from the project
root). Exits 0 on SMOKE OK, 1 on any failure. The real collab.db is
untouched. Docker itself is NOT required (and is not present in this
VM): the compose file is validated by parsing, not by running.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import yaml

PORT = 8144
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
    db_fd, db_path = tempfile.mkstemp(prefix="collab-stage4-smoke-", suffix=".db")
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

        # 1. Register two users, one board, one member add.
        status, alice = post("/auth/register", {
            "username": "smoke", "password": "password123", "display_name": "Smoke",
        })
        check(status == 201, "register alice via live server")
        status, bob = post("/auth/register", {
            "username": "smoke-bob", "password": "password123", "display_name": "Bob",
        })
        check(status == 201, "register bob via live server")
        auth = {"Authorization": f"Bearer {alice['access_token']}"}
        status, board = post("/boards", {"title": "Stage 4 board"}, auth)
        check(status == 201, "create board via live server")
        bid = board["id"]
        status, _ = post(f"/boards/{bid}/members",
                         {"username": "smoke-bob", "role": "editor"}, auth)
        check(status == 201, "add bob as editor")

        # 2. Feed after board create + member add.
        status, feed_raw = get(f"/boards/{bid}/activity", auth)
        feed = json.loads(feed_raw) if status == 200 else []
        check(status == 200 and [e["kind"] for e in feed] == [
            "member_added", "board_created",
        ], "feed lists member_added + board_created, newest first")
        check(feed[0]["actor"]["username"] == "smoke", "feed entry carries actor")
        status, page = get(f"/boards/{bid}/activity?limit=1", auth)
        check(status == 200 and len(json.loads(page)) == 1,
              "feed ?limit=1 returns one entry")
        status, _ = get("/boards/999999/activity", auth)
        check(status == 404, "feed for missing board is 404")

        # 3. Item mutations extend the feed.
        status, item = post(f"/boards/{bid}/items", {
            "type": "note", "x": 100, "y": 100, "w": 240, "h": 160, "text": "hi",
        }, auth)
        check(status == 201, "create item via live server")
        status, feed_raw = get(f"/boards/{bid}/activity?limit=1", auth)
        check(json.loads(feed_raw)[0]["kind"] == "item_created",
              "item create recorded in feed")

        # 4. Live WS op -> op echo, then an activity broadcast.
        import asyncio
        import websockets

        async def ws_activity():
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT}/ws/boards/{bid}?token={alice['access_token']}"
            ) as ws:
                welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert welcome["type"] == "welcome" and welcome["revision"] == 1
                await ws.send(json.dumps({
                    "type": "op", "op_id": "smoke-4", "base_revision": 1,
                    "op": {"kind": "create", "item": {
                        "type": "rect", "x": 400, "y": 100, "w": 200, "h": 120,
                        "text": "", "color": "#8ecae6", "z": 0,
                    }},
                }))
                op_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert op_msg["type"] == "op" and op_msg["revision"] == 2, op_msg
                act = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert act["type"] == "activity", act
                assert act["activity"]["kind"] == "item_created", act
                assert act["activity"]["revision"] == 2, act
                return True

        check(asyncio.run(ws_activity()),
              "WS op broadcasts activity entry live (revision 2)")

        # 5. Docker files: compose parses, Dockerfile is sane.
        with open(os.path.join(here, "docker-compose.yml")) as f:
            compose = yaml.safe_load(f)
        services = compose.get("services", {})
        check(list(services) == ["api"], "compose defines exactly one service: api")
        api_svc = services["api"]
        check("collab-data:/data" in api_svc.get("volumes", []),
              "compose mounts named volume collab-data:/data")
        check(api_svc.get("environment", []) and any(
            "COLLAB_DB_PATH=/data/collab.db" in str(e)
            for e in api_svc["environment"]
        ), "compose sets COLLAB_DB_PATH=/data/collab.db")
        check("collab-data" in compose.get("volumes", {}),
              "compose declares the collab-data volume")
        with open(os.path.join(here, "Dockerfile")) as f:
            dockerfile = f.read()
        check("FROM python:3.12-slim" in dockerfile,
              "Dockerfile pins python:3.12-slim")
        check("USER app" in dockerfile, "Dockerfile runs as non-root user")
        check("HEALTHCHECK" in dockerfile and "/health" in dockerfile,
              "Dockerfile healthchecks /health")

        # 6. Seed demo runs against the live server, feed non-empty.
        seed = subprocess.run(
            [venv_python, "scripts/seed_demo.py", "--base-url", BASE],
            cwd=here, env=env, capture_output=True, text=True, timeout=60,
        )
        check(seed.returncode == 0, "seed_demo.py runs against the live server")
        check("activity feed:" in seed.stdout, "seed demo prints the feed count")
        print(seed.stdout.strip())
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
