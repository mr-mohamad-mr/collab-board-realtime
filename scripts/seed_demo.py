"""Seed demo data through the public API: two users, one shared board
with a few items. Run while the server is up:

    python scripts/seed_demo.py [--base-url http://127.0.0.1:8000]

Uses ``trust_env=False``: this VM's ``no_proxy`` contains bracketed
IPv6 entries that break httpx's proxy-pattern parser for localhost.
"""

from __future__ import annotations

import argparse
import sys

import httpx

USERS = [
    ("alice", "password123", "Alice"),
    ("bob", "password123", "Bob"),
    ("carol", "password123", "Carol"),
]

ITEMS = [
    {"type": "note", "x": 120, "y": 100, "w": 240, "h": 160,
     "text": "Welcome to Collab Board! Drag me around.", "color": "#ffd166"},
    {"type": "note", "x": 420, "y": 100, "w": 240, "h": 160,
     "text": "Q3 launch plan — what ships first?", "color": "#8ecae6"},
    {"type": "rect", "x": 120, "y": 320, "w": 300, "h": 200,
     "text": "", "color": "#90be6d"},
    {"type": "text", "x": 140, "y": 340, "w": 260, "h": 60,
     "text": "Milestone 1: beta", "color": "#ffffff"},
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, trust_env=False, timeout=10)
    tokens: dict[str, str] = {}
    for username, password, display in USERS:
        resp = client.post(
            "/auth/register",
            json={"username": username, "password": password, "display_name": display},
        )
        if resp.status_code == 409:  # already seeded — log in instead
            resp = client.post(
                "/auth/login", json={"username": username, "password": password}
            )
        resp.raise_for_status()
        tokens[username] = resp.json()["access_token"]
        print(f"user {username} ok")

    headers = {"Authorization": f"Bearer {tokens['alice']}"}
    resp = client.post(
        "/boards", json={"title": "Demo board", "description": "Seeded demo board"},
        headers=headers,
    )
    resp.raise_for_status()
    board = resp.json()
    print(f"board {board['id']} ({board['title']}) ok")

    resp = client.post(
        f"/boards/{board['id']}/members",
        json={"username": "bob", "role": "editor"}, headers=headers,
    )
    resp.raise_for_status()
    print("bob added as editor")

    resp = client.post(
        f"/boards/{board['id']}/members",
        json={"username": "carol", "role": "viewer"}, headers=headers,
    )
    resp.raise_for_status()
    print("carol added as viewer")

    for item in ITEMS:
        resp = client.post(
            f"/boards/{board['id']}/items", json=item, headers=headers
        )
        resp.raise_for_status()
    print(f"{len(ITEMS)} items created; board revision now {board['revision'] + len(ITEMS)}")

    # A couple of follow-up edits so the stage-4 activity feed has
    # something realistic to show (rename + an item update).
    resp = client.patch(
        f"/boards/{board['id']}",
        json={"title": "Demo board — Q3 launch plan"}, headers=headers,
    )
    resp.raise_for_status()
    print("board renamed")
    snap = client.get(f"/boards/{board['id']}/snapshot", headers=headers).json()
    first = snap["items"][0]
    resp = client.patch(
        f"/boards/{board['id']}/items/{first['id']}",
        json={"text": "Welcome to Collab Board! Drag me around — live."},
        headers=headers,
    )
    resp.raise_for_status()
    print("first item updated")

    feed = client.get(
        f"/boards/{board['id']}/activity", headers=headers
    ).json()
    print(f"activity feed: {len(feed)} entries")
    for entry in feed[:3]:
        print(f"  #{entry['id']} {entry['kind']} by {entry['actor']['username']}")

    snap = client.get(f"/boards/{board['id']}/snapshot", headers=headers).json()
    print(f"snapshot: {len(snap['items'])} items, revision {snap['board']['revision']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
