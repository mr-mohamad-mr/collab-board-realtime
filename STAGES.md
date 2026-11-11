# Real-time Collab App — Stage Log

WebSocket collaborative boards: live multi-user editing, presence,
conflict handling, roles/permissions, frontend, tests, Docker. Built
one stage per run; nothing is published to GitHub until ALL stages are
complete and verified.

## Stages

### Stage 1 — Architecture + FastAPI backend core ✅ DONE (2026-11-08)

JWT auth, boards with role-based membership, canvas items, per-board
revision counter + snapshot endpoint (the sync foundation for stage 2).

**Built:**
- `app/database.py` — SQLite schema: `users`, `boards`,
  `board_members` (role owner/editor/viewer, one owner per board),
  `items` (note/rect/text/circle with x/y/w/h, text, color, z);
  board-first indexes; WAL + foreign keys; `bump_revision()`
  atomically increments `boards.revision` on every mutation.
- `app/security.py` — PBKDF2-HMAC-SHA256 password hashing (stdlib) +
  HS256 JWT (stdlib hmac) carrying only the user identity. Board roles
  are NOT in the token — they are looked up per request from
  `board_members`, so a role change takes effect immediately.
- `app/schemas.py` — Pydantic models; canvas geometry clamped to
  0–10,000 with positive sizes, `#rrggbb` color validation, text caps.
- `app/main.py` — `GET /health`, `POST /auth/register` (409 on taken
  username), `POST /auth/login`, `GET /auth/me`; board CRUD
  (`POST/GET /boards`, `GET/PATCH/DELETE /boards/{id}`); member
  management (`GET/POST /boards/{id}/members`,
  `PATCH/DELETE /boards/{id}/members/{user_id}`); item CRUD
  (`POST/GET /boards/{id}/items`, `PATCH/DELETE
  /boards/{id}/items/{item_id}`); `GET /boards/{id}/snapshot` —
  full board state (board + members + items) at one revision, the
  resync payload stage 2 will use after reconnects.
- Access rules: reads need membership (any role); edits need editor+;
  membership changes and deletion need owner; the owner can never be
  demoted or removed; a non-member gets 404 (existence never leaked).
  One connection per request (generator dependency, closed after the
  request) — a SQLite connection is never shared across threads.
- `scripts/seed_demo.py` — seeds alice + bob, a shared demo board,
  bob as editor, 4 demo items, via the public API (`trust_env=False`
  for the localhost httpx proxy quirk seen in flagship 1).
- `tests/` — 47 pytest tests (11 auth + 10 boards + 10 members +
  13 items + 3 snapshot), per-test throwaway DB.

**Skills applied:** `fastapi` (Annotated deps, return-type response
models, one HTTP operation per function, `def` path operations for
blocking SQLite), `fastapi_templates` (project layout, DI, TestClient
patterns), `websocket_realtime_builder` (validate access before any
board access — the rule stage 2 applies before a socket joins a board
room; snapshot/resync design), `pytest_patterns`.

**Verification (2026-11-08):**
- `pytest tests/ -q` → **47 passed**.
- Live smoke test (uvicorn on 127.0.0.1:8141, throwaway DB via
  `COLLAB_DB_PATH`): `/health` ok; register alice + bob, login ok;
  `seed_demo.py` created the demo board, added bob as editor, 4 items;
  `GET /boards/{id}/snapshot` → revision 4, 2 members, 4 items;
  viewer role check: carol (viewer) creating an item → 403;
  non-member board read → 404; tampered token → 401. Original
  `collab.db` state untouched (smoke test used a throwaway DB file).

### Stage 2 — WebSocket live sync + presence + conflict handling ✅ DONE (2026-11-09)

Op broadcast per board room, revision-based stale-op rejection,
presence list with join/leave events, reconnect resync via snapshot,
plus live notifications for REST mutations, role changes, kicks and
board deletion.

**Built:**
- `app/rooms.py` — in-memory `RoomManager`: one room per board
  (sockets + presence with per-user connection counts, so a second
  tab does not re-announce a user); `join`/`leave` broadcast
  presence events with the full presence list; `broadcast` is
  best-effort per socket (a dead socket is dropped, never breaks
  delivery to the rest). All room state mutates only on the app's
  event loop (installed by the lifespan), so no lock is needed —
  sync REST handlers use `*_sync` wrappers that schedule work via
  `loop.call_soon_threadsafe`. Single-process by design; a
  multi-worker deploy would move this to Redis pub/sub.
- `app/sync.py` — the wire protocol (pydantic `ClientMessage` /
  `OpPayload`: `op` / `cursor` / `ping`); `apply_op()` re-reads the
  caller's role from `board_members` on every op, so role changes
  take effect immediately, and applies an op only when
  `base_revision` equals the board's current revision — the first op
  at a revision wins and bumps it, every concurrent op at the same
  base revision is rejected as `stale` with the current revision
  (never silently overwritten); `RateLimiter` sliding-window
  limiter (30 ops / 10 s, 20 cursors / s) + 64 KB message cap.
- `app/websocket.py` — `WS /ws/boards/{board_id}?token=<jwt>`:
  token + membership validated BEFORE accept (close 4401 bad
  token, 4404 not a member — same code for a missing board, so
  existence is never leaked); on join sends `welcome` (revision +
  role + presence); `ping`→`pong`; cursors relayed to other members
  only; ops applied in a worker thread with a fresh SQLite
  connection per op (never shared across threads) and broadcast to
  the whole room including the sender (matched on `op_id`); stale
  ops get a `stale` reply pointing at the snapshot endpoint.
- `app/main.py` — REST mutations now broadcast too, so REST and WS
  clients stay in sync: item create/update/delete emit the same
  `op` message (`op_id` tagged `rest-*`); board PATCH emits
  `board_updated`; board DELETE notifies + closes all sockets
  (`board_deleted`); member role change sends `role_changed` to
  that user's sockets; member removal kicks their sockets
  (`member_removed`, close 4404).
- `tests/test_websocket.py` — 16 new tests; `tests/conftest.py`
  now points `app.state.db_path` at the throwaway test DB so the
  WS handlers (own connections per op) hit the same DB as REST.
- `scripts/ws_smoke.py` — live smoke test with real `websockets`
  clients against a real uvicorn server (throwaway DB).

**Skills applied:** `websocket_realtime_builder` (auth before join,
rooms, presence, reconnect/resync), `websocket_security` (rate
limits, message-size cap, close codes), `fastapi`, `pytest_patterns`
(single background-reader pump per test socket — a naive per-call
receiver thread steals later messages).

**Verification (2026-11-09):**
- `pytest tests/ -q` → **63 passed** (47 stage-1 + 16 new).
- Live smoke test (uvicorn on 127.0.0.1:8142, throwaway DB):
  register + board + member; welcome + presence join; WS op
  broadcast + sender echo (revision 1); stale op rejected with
  current_revision 1; cursor relayed; REST create broadcast to live
  WS clients (revision 2); reconnect presence shows only alice
  after bob left; snapshot revision 2 with 2 items — **SMOKE OK**.
  Original `collab.db` untouched (smoke test used a throwaway DB).

### Stage 3 — Frontend (canvas board UI) ✅ DONE (2026-11-10)

Auth + boards list + canvas board UI, vanilla JS (no build step, no
external assets) served by the same FastAPI app — the flagship-1
stage-3 pattern. Viewers get a read-only canvas.

**Built:**
- `app/main.py` — `GET /` serves `app/static/index.html`,
  `/static/*` serves the assets; version bumped to 0.3.0. API unchanged.
- `app/static/index.html` — app shell: auth view (login/register),
  boards list view, board view (toolbar, canvas, presence bar,
  members panel, inspector).
- `app/static/css/app.css` — one stylesheet (same palette as
  flagship 1), visible keyboard focus, responsive layout (side panel
  stacks below 900 px).
- `app/static/js/api.js` — REST client: token in localStorage (memory
  fallback), `ApiError` with status, all endpoints used by the UI.
- `app/static/js/sync.js` — WebSocket sync client for the stage-2
  protocol: op send with `base_revision`, resolves on the broadcast
  echo (matched on `op_id`); on `stale` resyncs via snapshot and
  retries once; reconnect with exponential backoff (1 s → 10 s cap)
  + snapshot resync on reconnect; 25 s heartbeat ping; queued ops
  while offline flushed after resync; 4401/4404 close codes do NOT
  reconnect (auth/membership failure).
- `app/static/js/board.js` — canvas view (world 0–10,000, pan/zoom
  with wheel + buttons + fit, devicePixelRatio-aware): renders
  note/rect/text/circle with word-wrapped text; drag-to-move and
  bottom-right resize handle (local preview, one op committed on
  pointer-up); double-click empty space adds a note; click-to-place
  tools for all four item types; Delete/Escape keys; live cursors of
  other members with name labels; dotted grid + canvas bounds.
- `app/static/js/app.js` — view glue: auth, boards list (create /
  open / owner delete), board open/close lifecycle, presence chips,
  member management (owner: add by username, change role, remove),
  board rename (owner), inspector (text + colour + delete for the
  selected item), connection/revision status pill, toasts; handles
  `role_changed` (toolbar re-permissioned live), `member_removed`
  and `board_deleted` (back to the boards list).
- `tests/test_frontend.py` — 10 new tests: shell served, every
  referenced asset resolves, assets exist on disk, JS namespaces
  present, API still requires auth, static path cannot escape,
  no Node imports in browser JS.
- `scripts/frontend_smoke.py` — live smoke: real uvicorn on a
  throwaway DB; shell + 5 assets, `node --check` on all 4 JS files,
  register/board/item via the live API, WS op round-trip
  (revision 1 → 2).

**Skills applied:** `frontend_ui_ux_design` (user-centred flow,
WCAG focus visibility + semantic HTML/ARIA labels, responsive
layout, component states), `websocket_realtime_builder` (client-side
reconnect/resync flow from the README), `fastapi` (StaticFiles mount
+ FileResponse shell, same as flagship 1 stage 3).

**Verification (2026-11-10):**
- `pytest tests/ -q` → **73 passed** (63 stage-1/2 + 10 new).
- Live smoke test (uvicorn on 127.0.0.1:8143, throwaway DB):
  shell + all 5 assets 200, all JS parses, register/board/item OK,
  snapshot revision 1, WS op round-trip revision 1 → 2 — **SMOKE OK**.
  Original `collab.db` untouched.

### Stage 4 — Activity feed, Docker Compose, full test pass, README ✅ DONE (2026-11-11)

Then: CV entry + GitHub publish (repo `collab-board-realtime`) +
profile README featured link.

**Built:**
- `app/activity.py` — the feed: `record()` inserts one entry per
  mutation **in the same transaction** as the mutation (a failed
  edit leaves no entry; a committed edit always has one), carrying
  the board revision at the time; `list_entries()` serves
  newest-first pages with a `before_id` cursor; retention is
  enforced, not just documented — at most 500 entries per board,
  older rows pruned on insert (audit_logging skill). Entries never
  contain secrets: ids, usernames, item types and ≤80-char text
  snippets only. Kinds: `board_created`, `board_updated`,
  `member_added`, `member_role_changed`, `member_removed`,
  `item_created`, `item_updated`, `item_deleted`.
- `app/database.py` — `activity` table (`board_id` FK cascades, so
  deleting a board deletes its feed) + `(board_id, id DESC)` index;
  verified to migrate onto the pre-existing `collab.db` (no table
  before, created on first connect).
- `app/main.py` — every REST mutation records + broadcasts
  (`board_updated`/`role_changed`/`member_removed`/op first, feed
  entry second — the primary message always leads); new endpoint
  `GET /boards/{id}/activity?limit=&before_id=` (limit clamped
  1–100, members only, non-member 404). Version 0.4.0.
- `app/sync.py` + `app/websocket.py` — WS ops record the entry in
  `apply_op`'s transaction and broadcast
  `{"type": "activity", "activity": {...}}` right after the op echo.
- `app/rooms.py` — `close_user_sockets` / `close_board` now drop
  sockets from the room *before* sending: their sends await (and
  yield the loop), so a later-scheduled broadcast could otherwise
  interleave a message to a socket about to close — caught live by
  the member-removed test.
- Frontend — activity panel in the board view (`index.html`,
  `app.css`): `api.js` gains `listActivity`, `sync.js` forwards
  `activity` messages, `app.js` renders human-readable entries
  ("Alice added a note: “Welcome…”", "Bob changed Carol's role to
  editor"), prepends live entries, and loads older pages via
  `before_id`.
- `Dockerfile` — `python:3.12-slim`, non-root `app` user, named
  data dir `/data`, `/health` HEALTHCHECK (docker_compose skill).
- `docker-compose.yml` — one service `api` (single-process by
  design: in-memory rooms + SQLite), named volume `collab-data`,
  `COLLAB_DB_PATH=/data/collab.db`, `COLLAB_PORT` override,
  `COLLAB_SECRET` from `.env` (dev default otherwise),
  `restart: unless-stopped`, healthcheck.
- `.dockerignore` — venv, caches, `*.db`, `.env`, `.git`.
- `scripts/seed_demo.py` — now seeds carol as viewer too, renames
  the board and updates an item, so the demo feed has 9 entries.
- `scripts/stage4_smoke.py` — live smoke: feed REST + pagination,
  live WS activity broadcast, compose YAML parsed and checked,
  Dockerfile checks, seed demo run (9-entry feed).
- `requirements.txt` — added `pyyaml` (stage-4 smoke only).

**Skills applied:** `docker_compose` (single service, named volume,
healthcheck, non-root, env from `.env`), `audit_logging`
(structured audit entries, retention policy enforced per board,
no secrets in entries), `fastapi`, `pytest_patterns`,
`frontend_ui_ux_design` (activity panel in the side panel,
aria-live feed list), `websocket_realtime_builder` (live feed
broadcast alongside the op stream).

**Verification (2026-11-11):**
- `pytest tests/ -q` → **85 passed** (73 stage-1–3 + 12 new:
  11 activity tests + frontend activity assertions). Stage-2 WS
  tests were updated to consume the new `activity` message after
  each op (protocol addition, op behaviour unchanged).
- Live smoke tests (real uvicorn, throwaway DBs): `ws_smoke.py`
  SMOKE OK (incl. new live-activity check), `frontend_smoke.py`
  SMOKE OK, `stage4_smoke.py` SMOKE OK (feed, pagination, WS
  activity broadcast, compose/Dockerfile checks, seed demo with a
  9-entry feed). Original `collab.db` untouched (0 boards; the
  migration was verified on a throwaway copy instead).
- Docker itself is NOT installed in this VM, so `docker compose up`
  could not be run here — the compose file was validated by YAML
  parse + structural checks in the smoke test, and the Dockerfile
  by inspection. First run on a machine with Docker is
  `docker compose up --build` (see README).
