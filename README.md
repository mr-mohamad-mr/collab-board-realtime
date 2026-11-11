# Collab Board — Real-time Collaborative Boards

Multi-user whiteboard app: live multi-user editing over WebSockets,
presence, conflict handling, roles/permissions, activity feed.
Flagship project 2 in Mamad's flagship programme (see
`~/workspace/products/PROJECT_LOG.md`).

> **Status: ✅ COMPLETE — all 4 stages done and verified (2026-11-11).**
> Published to GitHub: https://github.com/mr-mohamad-mr/collab-board-realtime

## Architecture

```
app/
  main.py      FastAPI app: auth, boards, members, items, snapshot,
               activity feed (+ REST→WS broadcast of every mutation)
               + GET / app shell and /static/* assets (stage 3)
  database.py  SQLite schema + per-board revision counter + activity table
  security.py  PBKDF2 passwords + stdlib HS256 JWTs (no extra deps)
  schemas.py   Pydantic models incl. canvas geometry validation
  activity.py  feed recording (same transaction as the mutation),
               cursor pagination, 500-per-board retention (stage 4)
  rooms.py     in-memory board rooms: sockets + presence (stage 2)
  sync.py      WS op protocol, stale-op conflict rule, rate limits
  websocket.py WS /ws/boards/{id}?token=<jwt> (stage 2)
  static/      vanilla-JS frontend (stage 3): index.html, css/app.css,
               js/api.js (REST client), js/sync.js (WS client),
               js/board.js (canvas view), js/app.js (view glue)
tests/         85 pytest tests (TestClient, throwaway DB per test)
scripts/
  seed_demo.py  Seeds 3 users + a shared demo board via the public API
  ws_smoke.py   Live smoke test with real WS clients (stage 2)
  frontend_smoke.py  Live smoke test: shell + assets + WS round-trip
  stage4_smoke.py    Live smoke test: activity feed + Docker files
Dockerfile, docker-compose.yml, .dockerignore  (stage 4)
```

**Domain model**

- `users` — username (unique), display name, password hash.
- `boards` — title, description, owner, monotonic `revision`.
- `board_members` — `(board_id, user_id)` with a role:
  `owner` (exactly one, the creator), `editor`, `viewer`.
- `items` — canvas objects: `note` / `rect` / `text` / `circle` with
  `x, y, w, h` (clamped to a 0–10,000 canvas), text, `#rrggbb` color,
  `z` order, creator.
- `activity` — one row per board mutation: actor, kind
  (`board_created` / `board_updated` / `member_added` /
  `member_role_changed` / `member_removed` / `item_created` /
  `item_updated` / `item_deleted`), a small detail object (ids,
  usernames, item type, ≤80-char text snippet — never secrets),
  the board `revision` at the time. Kept to the newest 500 rows
  per board; deleting a board deletes its feed with it.

**Access rules** (enforced on every endpoint; a non-member gets 404,
never 403, so board existence is not leaked):

- Reads need membership (any role); edits need `editor` or `owner`.
- Membership changes and board deletion need `owner`.
- The owner can never be demoted or removed.

**The `revision` counter** is the foundation of stage 2: it bumps on
every board/item mutation, and `GET /boards/{id}/snapshot` returns the
full board state at one revision — a client that reconnects fetches the
snapshot and then applies only newer ops, so it can never miss an edit
or apply one twice.

## API

| Method | Endpoint | Role |
| --- | --- | --- |
| GET | `/health` | — |
| POST | `/auth/register`, `/auth/login` | — |
| GET | `/auth/me` | auth |
| POST / GET | `/boards` | auth / member |
| GET / PATCH / DELETE | `/boards/{id}` | member / owner / owner |
| GET / POST | `/boards/{id}/members` | member / owner |
| PATCH / DELETE | `/boards/{id}/members/{user_id}` | owner |
| POST / GET | `/boards/{id}/items` | editor+ / member |
| PATCH / DELETE | `/boards/{id}/items/{item_id}` | editor+ |
| GET | `/boards/{id}/snapshot` | member |
| GET | `/boards/{id}/activity?limit=&before_id=` | member |

The activity feed is newest-first; `limit` is clamped to 1–100
(default 50) and `before_id` pages backwards (pass the oldest id
you have). Auth is a `Bearer` JWT (`Authorization: Bearer <token>`),
24 h expiry. The token carries only the user identity; board roles
are looked up per request from `board_members`, so a role change
takes effect immediately without re-issuing tokens.

## WebSocket live sync (stage 2)

Connect with the JWT as a query param (browsers cannot set headers
on a WebSocket handshake):

```
WS /ws/boards/{board_id}?token=<jwt>
```

Auth is checked **before** the socket is accepted: a bad/missing
token closes with `4401`, a non-member (or a board that does not
exist — same code, so existence is never leaked) with `4404`.

**Client → server** (JSON text frames):

| Message | Meaning |
| --- | --- |
| `{"type":"op","op_id":"…","base_revision":N,"op":{…}}` | Apply an op. `op` is `{"kind":"create","item":{…}}`, `{"kind":"update","item_id":7,"patch":{…}}` or `{"kind":"delete","item_id":7}`. |
| `{"type":"cursor","x":123.5,"y":456}` | Ephemeral pointer position, relayed to the other members, never stored. |
| `{"type":"ping"}` | → `{"type":"pong"}` |

**Server → client:**

| Message | Meaning |
| --- | --- |
| `welcome` | On join: `board_id`, current `revision`, your `role`, full `presence` list. |
| `op` | A committed op, broadcast to the whole room **including the sender** (match on `op_id`); carries the new `revision` and `by` (the author). |
| `activity` | (stage 4) The feed entry recorded with the just-committed op or REST mutation — same entry `GET /boards/{id}/activity` returns, so a client that missed it refetches. |
| `stale` | Your `base_revision` is behind the board. Resync via `GET /boards/{id}/snapshot` and retry — nothing was overwritten. |
| `error` | Validation/permission failure (`op_id` echoed when known); the socket stays open. |
| `presence` | `join`/`leave` event + the full presence list. A second tab from the same user does not re-announce them. |
| `cursor` | Another member's pointer (`user_id`, `username`, `x`, `y`). |
| `role_changed` | Your role on this board changed (sent to your sockets). |
| `member_removed` | You were removed from the board; the socket then closes (`4404`). |
| `board_deleted` | The board was deleted; all sockets then close. |
| `board_updated` | The board's title/description changed (new `revision`). |

**Conflict rule:** an op applies only when its `base_revision`
equals the board's current revision. The first op at a revision
wins and bumps it; every concurrent op at the same base revision is
rejected as `stale`. REST mutations (item create/update/delete)
broadcast the same `op` message (`op_id` tagged `rest-*`), so REST
and WS clients stay in sync — and a client that missed any change
is caught by the stale-op check on its next op.

**Reconnect flow:** fetch `GET /boards/{id}/snapshot` (full state at
one revision), open the socket, compare the `welcome` revision with
the snapshot's — if it moved, fetch the snapshot again. Then apply
only ops with a higher revision.

**Limits:** 64 KB max message, 30 ops / 10 s per socket (over that:
`error` + close `1008`), 20 cursors / s (excess dropped silently).
Viewers can join and watch, but their ops are rejected. Rooms are
in-memory and single-process by design — a multi-worker deploy
would move room state to Redis pub/sub.

## Frontend (stage 3)

Open `http://127.0.0.1:8000/` — the same server serves the app shell
and the API (no separate dev server, no build step, no external
assets). Register or log in, create a board, and open it in two
browser windows (two users) to see live editing.

- **Boards list:** create, open, delete (owner only), role badge per board.
- **Canvas:** pan (drag empty space or Pan tool), zoom (wheel, +/−,
  Fit), drag items to move, drag the corner handle to resize,
  double-click empty space to add a note, click-to-place tools for
  note / rectangle / text / circle, Delete removes the selection.
- **Live:** presence chips show who is on the board; other members'
  cursors appear with name labels; every edit broadcasts to all open
  clients. The status pill shows Live / Reconnecting / Offline and
  the current revision.
- **Members:** owner adds members by username, changes roles, or
  removes them — live sockets learn immediately (`role_changed`,
  `member_removed`). Viewers see a read-only canvas.
- **Activity (stage 4):** the side panel lists who did what
  ("Alice added a note", "Bob changed Carol's role to editor"),
  newest first, with a "Load older activity" button; new entries
  appear live as they happen.
- **Sync safety:** ops carry the board's `base_revision`; if your op
  races with another member's, the client resyncs from the snapshot
  and retries once automatically. A dropped connection reconnects
  with backoff and resyncs — no edit is lost or applied twice.

## Run

Local (venv):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
.venv/bin/python scripts/seed_demo.py   # in another shell, while running
.venv/bin/python -m pytest tests/ -q   # 85 passed
.venv/bin/python scripts/ws_smoke.py   # live WS smoke test, SMOKE OK
.venv/bin/python scripts/frontend_smoke.py   # live frontend smoke test, SMOKE OK
.venv/bin/python scripts/stage4_smoke.py     # live stage-4 smoke test, SMOKE OK
```

Docker (one service — the app is single-process by design):

```bash
docker compose up --build
# open http://localhost:8000 (or $COLLAB_PORT)
docker compose exec api python scripts/seed_demo.py
docker compose down        # keeps board data in the collab-data volume
```

Environment: `COLLAB_DB_PATH` (default `collab.db` next to the project),
`COLLAB_SECRET` (JWT signing secret; change in production — the default
is dev-only), `COLLAB_PORT` (compose host port, default 8000).

## Scaling

- **Single process, single replica** — rooms live in memory and the
  DB is one SQLite file, so run exactly one `api` container (or one
  uvicorn worker). Scaling past that needs Redis pub/sub for rooms
  and a shared database.
- **SQLite** is the right call at this size: one file, WAL mode,
  board-first indexes; the feed is capped at 500 rows per board.
- **Activity feed** is the audit trail: same-transaction recording
  means it can never disagree with the board, and the 500-row cap
  bounds storage on busy boards.

## Skills applied

- `fastapi` (Annotated deps, return-type response models, one HTTP
  operation per function, `def` path operations for blocking SQLite)
- `fastapi_templates` (project layout, DI, TestClient patterns)
- `websocket_realtime_builder` (access validated before any board
  access — applied before a socket joins a board room; snapshot/
  resync design; presence groundwork)
- `websocket_security` (stage 2: rate limits, message-size cap,
  close codes)
- `pytest_patterns` (throwaway DB per test, behaviour-level API tests;
  stage 2: one background-reader pump per test socket)
- `frontend_ui_ux_design` (stage 3: user-centred flow, WCAG focus
  visibility + semantic HTML/ARIA labels, responsive layout,
  component states)
- `websocket_realtime_builder` (stage 3: client-side reconnect/resync
  flow, op echo matching, stale-op retry)
- `docker_compose` (stage 4: single service, named volume,
  healthcheck, non-root user, env from `.env`)
- `audit_logging` (stage 4: structured audit entries, per-board
  retention enforced, no secrets in entries)

## Roadmap

- **Stage 1** — ✅ DONE 2026-11-08: Architecture + FastAPI backend
  core (tenants→boards, JWT auth, items, revision counter, snapshot).
- **Stage 2** — ✅ DONE 2026-11-09: WebSocket live sync + presence +
  conflict handling (op broadcast per board room, revision-based
  stale-op rejection, presence list, reconnect resync via snapshot,
  REST→WS broadcast, role-change/kick/delete notifications).
- **Stage 3** — ✅ DONE 2026-11-10: Frontend — vanilla-JS app shell
  served by the same FastAPI app: auth, boards list, canvas board UI
  (pan/zoom, drag + resize, click-to-place tools, live cursors,
  presence), member management, inspector; WS client with stale-op
  resync + retry and reconnect-with-backoff; 73 pytest tests green,
  live frontend smoke test SMOKE OK.
- **Stage 4** — ✅ DONE 2026-11-11: Activity feed (same-transaction
  recording, paginated REST endpoint, live WS broadcast, frontend
  panel), Docker + Docker Compose, full test pass (85 pytest tests
  green, all three live smoke tests SMOKE OK), demo data, final
  README. Published to GitHub as `collab-board-realtime`.
