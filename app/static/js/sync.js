/* Collab Board — WebSocket sync client.
   Talks the stage-2 wire protocol (see README): op messages with a
   base_revision, stale-op resync via the snapshot endpoint, presence
   and live cursors. The server is the source of truth: a local op is
   only "done" when its broadcast echo (matched on op_id) comes back.

   Reconnect: on an unexpected close it retries with exponential
   backoff (1s → 10s cap), then resyncs from the snapshot — the
   README's reconnect flow — so a dropped connection can never lose
   an edit or apply one twice. Ops sent while offline are queued and
   flushed after the resync (with the fresh base_revision).

   Callbacks (all optional, set via `on`):
     welcome({revision, role, presence})   op({revision, by, op})
     stale({op_id, current_revision})      error({op_id, detail})
     presence({event, user, presence})     cursor({user_id, username, x, y})
     roleChanged(role)                     memberRemoved()
     boardDeleted()                        boardUpdated({revision, board})
     activity({activity})                  status("live"|"reconnecting"|"offline", detail?)
*/

(function (global) {
  "use strict";

  var MAX_RECONNECT_DELAY_MS = 10000;

  function uid() {
    return "op-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  }

  function createSyncClient(api, boardId, hooks) {
    hooks = hooks || {};
    var ws = null;
    var token = null;
    var revision = 0;
    var role = null;
    var presence = [];
    var connected = false;
    var wantClose = false;          // true when close() was called on purpose
    var reconnectTimer = null;
    var reconnectDelay = 1000;
    var pending = {};               // op_id -> {op, resolve, reject, retries}
    var queued = [];                // ops waiting for a live socket
    var heartbeatTimer = null;

    function emit(name, data) {
      if (typeof hooks[name] === "function") {
        try { hooks[name](data); } catch (e) { /* a hook must never break sync */ }
      }
    }

    function setStatus(state, detail) {
      emit("status", { state: state, detail: detail || "" });
    }

    function wsUrl() {
      var proto = global.location.protocol === "https:" ? "wss:" : "ws:";
      return proto + "//" + global.location.host + "/ws/boards/" + boardId +
        "?token=" + encodeURIComponent(token);
    }

    function startHeartbeat() {
      stopHeartbeat();
      heartbeatTimer = global.setInterval(function () {
        if (ws && ws.readyState === 1) {
          try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) { /* socket dying */ }
        }
      }, 25000);
    }

    function stopHeartbeat() {
      if (heartbeatTimer) { global.clearInterval(heartbeatTimer); heartbeatTimer = null; }
    }

    /** Fetch the snapshot and hand the fresh state to the app. */
    function resync() {
      return api.snapshot(boardId).then(function (snap) {
        revision = snap.board.revision;
        role = snap.board.role;
        emit("resync", snap);
        return snap;
      });
    }

    function flushQueue() {
      var items = queued;
      queued = [];
      items.forEach(function (entry) { sendOpNow(entry); });
    }

    function sendOpNow(entry) {
      if (!ws || ws.readyState !== 1) { queued.push(entry); return; }
      pending[entry.op_id] = entry;
      ws.send(JSON.stringify({
        type: "op", op_id: entry.op_id, base_revision: revision, op: entry.op,
      }));
    }

    /** Send an op; resolves when the broadcast echo confirms it.
        On `stale` the client resyncs and retries once automatically. */
    function sendOp(op) {
      return new Promise(function (resolve, reject) {
        sendOpNow({ op_id: uid(), op: op, resolve: resolve, reject: reject, retries: 0 });
      });
    }

    function sendCursor(x, y) {
      if (!ws || ws.readyState !== 1) { return; }
      try { ws.send(JSON.stringify({ type: "cursor", x: x, y: y })); } catch (e) { /* ignore */ }
    }

    function handleMessage(raw) {
      var msg;
      try { msg = JSON.parse(raw); } catch (e) { return; }
      switch (msg.type) {
        case "welcome":
          revision = msg.revision;
          role = msg.role;
          presence = msg.presence || [];
          connected = true;
          reconnectDelay = 1000;
          setStatus("live");
          startHeartbeat();
          emit("welcome", { revision: revision, role: role, presence: presence });
          flushQueue();
          break;
        case "op":
          // The sender matches on op_id; everyone (incl. the sender)
          // applies the op and moves to the new revision.
          revision = msg.revision;
          var entry = msg.op_id && pending[msg.op_id];
          if (entry) { delete pending[msg.op_id]; entry.resolve(msg); }
          emit("op", msg);
          break;
        case "stale": {
          var stale = msg.op_id && pending[msg.op_id];
          if (stale) {
            delete pending[msg.op_id];
            if (stale.retries < 1) {
              stale.retries += 1;
              resync().then(function () { sendOpNow(stale); }, function () {
                stale.reject(new Error("resync failed after stale op"));
              });
            } else {
              stale.reject(new Error("op rejected as stale (revision " + msg.current_revision + ")"));
            }
          }
          emit("stale", msg);
          break;
        }
        case "error": {
          var failed = msg.op_id && pending[msg.op_id];
          if (failed) { delete pending[msg.op_id]; failed.reject(new Error(msg.detail || "op failed")); }
          emit("error", msg);
          break;
        }
        case "presence":
          presence = msg.presence || presence;
          emit("presence", msg);
          break;
        case "cursor":
          emit("cursor", msg);
          break;
        case "role_changed":
          role = msg.role;
          emit("roleChanged", msg.role);
          break;
        case "member_removed":
          emit("memberRemoved", msg);
          break;
        case "board_deleted":
          emit("boardDeleted", msg);
          break;
        case "board_updated":
          revision = msg.revision;
          emit("boardUpdated", msg);
          break;
        case "activity":
          emit("activity", msg);
          break;
        case "pong":
          break;
        default:
          break;
      }
    }

    function scheduleReconnect() {
      if (wantClose) { return; }
      setStatus("reconnecting", "retrying in " + Math.round(reconnectDelay / 1000) + "s");
      reconnectTimer = global.setTimeout(function () {
        reconnectTimer = null;
        connect();
      }, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
    }

    function connect() {
      if (!token) { return; }
      try { ws = new WebSocket(wsUrl()); } catch (e) { scheduleReconnect(); return; }
      ws.onopen = function () { /* welcome confirms the join */ };
      ws.onmessage = function (ev) { handleMessage(ev.data); };
      ws.onerror = function () { /* onclose carries the recovery */ };
      ws.onclose = function (ev) {
        stopHeartbeat();
        connected = false;
        if (wantClose) { setStatus("offline"); return; }
        if (ev.code === 4401 || ev.code === 4404) {
          // Auth / membership failure: reconnecting will not help.
          setStatus("offline", ev.code === 4401 ? "not signed in" : "no access to this board");
          emit("authFailed", { code: ev.code });
          return;
        }
        scheduleReconnect();
      };
    }

    return {
      /** Open the socket (token from the API client). */
      open: function (jwt) {
        token = jwt;
        wantClose = false;
        // Fresh state first, then the socket (README reconnect flow).
        return resync().then(function () { connect(); });
      },
      close: function () {
        wantClose = true;
        if (reconnectTimer) { global.clearTimeout(reconnectTimer); reconnectTimer = null; }
        stopHeartbeat();
        if (ws) { try { ws.close(); } catch (e) { /* already closed */ } ws = null; }
        connected = false;
        pending = {};
        queued = [];
      },
      sendOp: sendOp,
      sendCursor: sendCursor,
      resync: resync,
      get revision() { return revision; },
      get role() { return role; },
      get presence() { return presence; },
      get connected() { return connected; },
    };
  }

  global.CollabSync = { createSyncClient: createSyncClient };
})(typeof window !== "undefined" ? window : globalThis);
