/* Collab Board — app shell (view glue).
   Three views: auth → boards list → one board (canvas + members +
   inspector). Owns the sync client lifecycle: opening a board fetches
   the snapshot, opens the socket, and hands ops/presence/cursors to
   the board view; closing a board closes the socket.
*/

(function (global) {
  "use strict";

  var api = CollabApi.createApi("");
  var boardView = null;
  var sync = null;
  var currentUser = null;
  var currentBoard = null;   // {id, title, description, revision, role}
  var members = [];
  var selectedItem = null;
  var cursors = {};           // userId -> {username, x, y}
  var activityOldestId = null; // cursor for "load older activity"
  var activityLoading = false;

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  var toastTimer = null;
  function toast(message, kind) {
    var t = $("toast");
    t.textContent = message;
    t.className = "toast show " + (kind || "info");
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { t.className = "toast"; }, 4000);
  }

  function showView(name) {
    ["view-auth", "view-boards", "view-board"].forEach(function (id) {
      $(id).hidden = id !== "view-" + name;
    });
  }

  function setStatus(state, detail) {
    var pill = $("conn-status");
    pill.textContent = state === "live" ? "● Live"
      : state === "reconnecting" ? "◌ Reconnecting…"
      : "○ Offline" + (detail ? " — " + detail : "");
    pill.className = "conn conn-" + state;
  }

  function setRevision(rev) {
    $("board-revision").textContent = "rev " + rev;
  }

  // --- auth ---------------------------------------------------------------
  function onAuthSuccess(data) {
    api.setToken(data.access_token);
    currentUser = data.user;
    $("user-label").textContent = currentUser.display_name + " (@" + currentUser.username + ")";
    showView("boards");
    loadBoards();
  }

  function authError(err) {
    toast(err.status === 401 ? "Wrong username or password." : err.message, "error");
  }

  function bindAuth() {
    $("login-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      api.login($("login-username").value.trim(), $("login-password").value)
        .then(onAuthSuccess, authError);
    });
    $("register-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var u = $("reg-username").value.trim();
      var p = $("reg-password").value;
      api.register(u, p, $("reg-display").value.trim())
        .then(onAuthSuccess, function (err) {
          toast(err.status === 409 ? "That username is taken." : err.message, "error");
        });
    });
    $("logout-btn").addEventListener("click", function () {
      closeBoard();
      api.setToken(null);
      currentUser = null;
      showView("auth");
    });
  }

  // --- boards list ----------------------------------------------------------
  function roleBadge(role) {
    var b = el("span", "badge badge-" + role, role);
    return b;
  }

  function loadBoards() {
    api.listBoards().then(function (boards) {
      var list = $("boards-list");
      list.innerHTML = "";
      if (!boards.length) {
        list.appendChild(el("p", "empty", "No boards yet — create your first one above."));
      }
      boards.forEach(function (b) {
        var card = el("article", "board-card");
        var title = el("h3", null, b.title);
        var meta = el("p", "muted", "rev " + b.revision + " · " + (b.description || "No description"));
        var row = el("div", "card-row");
        row.appendChild(roleBadge(b.role));
        var openBtn = el("button", "btn btn-primary", "Open");
        openBtn.addEventListener("click", function () { openBoard(b.id); });
        row.appendChild(openBtn);
        if (b.role === "owner") {
          var delBtn = el("button", "btn btn-danger", "Delete");
          delBtn.addEventListener("click", function () {
            if (global.confirm("Delete board “" + b.title + "”? This cannot be undone.")) {
              api.deleteBoard(b.id).then(loadBoards, function (err) { toast(err.message, "error"); });
            }
          });
          row.appendChild(delBtn);
        }
        card.appendChild(title);
        card.appendChild(meta);
        card.appendChild(row);
        list.appendChild(card);
      });
    }, function (err) {
      if (err.status === 401) { showView("auth"); return; }
      toast(err.message, "error");
    });
  }

  function bindBoards() {
    $("create-board-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var title = $("new-board-title").value.trim();
      if (!title) { return; }
      api.createBoard(title, $("new-board-desc").value.trim())
        .then(function (b) {
          $("new-board-title").value = "";
          $("new-board-desc").value = "";
          openBoard(b.id);
        }, function (err) { toast(err.message, "error"); });
    });
  }

  // --- board view -----------------------------------------------------------
  function canEdit() {
    return currentBoard && (currentBoard.role === "owner" || currentBoard.role === "editor");
  }

  // --- activity feed (stage 4) -----------------------------------------
  function activityText(entry) {
    var name = entry.actor.display_name || entry.actor.username;
    var d = entry.detail || {};
    switch (entry.kind) {
      case "board_created": return name + " created this board";
      case "board_updated": return name + " updated the board";
      case "member_added": return name + " added " + (d.display_name || d.username) + " as " + d.role;
      case "member_role_changed": return name + " changed " + (d.display_name || d.username) + "'s role to " + d.to_role;
      case "member_removed": return name + " removed " + (d.display_name || d.username);
      case "item_created": return name + " added a " + d.item_type + (d.text ? ": \u201c" + d.text + "\u201d" : "");
      case "item_updated": return name + " updated a " + d.item_type;
      case "item_deleted": return name + " deleted a " + d.item_type;
      default: return name + " did " + entry.kind;
    }
  }

  function activityEntryEl(entry) {
    var li = el("li", "activity-entry");
    var when = entry.created_at ? new Date(entry.created_at.replace(" ", "T") + "Z") : null;
    li.appendChild(el("span", "activity-text", activityText(entry)));
    li.appendChild(el("span", "activity-time muted small",
      when && !isNaN(when) ? when.toLocaleString() : (entry.created_at || "")));
    return li;
  }

  function loadActivity(beforeId) {
    if (!currentBoard || activityLoading) { return; }
    activityLoading = true;
    api.listActivity(currentBoard.id, 50, beforeId).then(function (entries) {
      var list = $("activity-list");
      entries.forEach(function (entry) {
        list.appendChild(activityEntryEl(entry));
        if (activityOldestId === null || entry.id < activityOldestId) {
          activityOldestId = entry.id;
        }
      });
      $("activity-more").style.display = entries.length < 50 ? "none" : "";
      if (!beforeId && entries.length === 0) {
        list.appendChild(el("li", "muted small", "No activity yet."));
      }
    }, function (err) {
      toast("Could not load activity: " + err.message, "error");
    }).then(function () { activityLoading = false; });
  }

  function prependActivity(entry) {
    var list = $("activity-list");
    var empty = list.querySelector(".muted.small");
    if (empty) { list.removeChild(empty); }
    list.insertBefore(activityEntryEl(entry), list.firstChild);
  }

  function renderPresence(presence) {
    var bar = $("presence-bar");
    bar.innerHTML = "";
    (presence || []).forEach(function (p) {
      var chip = el("span", "presence-chip" + (p.user_id === currentUser.id ? " me" : ""),
        (p.display_name || p.username) + (p.user_id === currentUser.id ? " (you)" : ""));
      chip.title = p.role;
      bar.appendChild(chip);
    });
    bar.appendChild(el("span", "muted", (presence || []).length + " online"));
  }

  function renderMembers() {
    var list = $("members-list");
    list.innerHTML = "";
    members.forEach(function (m) {
      var row = el("li", "member-row");
      row.appendChild(el("span", null, (m.display_name || m.username) + " (@" + m.username + ")"));
      row.appendChild(roleBadge(m.role));
      if (currentBoard.role === "owner" && m.role !== "owner") {
        var sel = document.createElement("select");
        sel.setAttribute("aria-label", "Role for " + m.username);
        ["editor", "viewer"].forEach(function (r) {
          var o = document.createElement("option");
          o.value = r; o.textContent = r; if (m.role === r) { o.selected = true; }
          sel.appendChild(o);
        });
        sel.addEventListener("change", function () {
          api.updateMember(currentBoard.id, m.user_id, sel.value).then(function () {
            toast(m.username + " is now " + sel.value + ".");
            loadMembers();
          }, function (err) { toast(err.message, "error"); loadMembers(); });
        });
        row.appendChild(sel);
        var rm = el("button", "btn btn-small btn-danger", "Remove");
        rm.addEventListener("click", function () {
          if (global.confirm("Remove " + m.username + " from this board?")) {
            api.removeMember(currentBoard.id, m.user_id).then(loadMembers,
              function (err) { toast(err.message, "error"); });
          }
        });
        row.appendChild(rm);
      }
      list.appendChild(row);
    });
    $("add-member-form").style.display = currentBoard.role === "owner" ? "" : "none";
  }

  function loadMembers() {
    api.listMembers(currentBoard.id).then(function (ms) {
      members = ms;
      renderMembers();
    }, function (err) { toast(err.message, "error"); });
  }

  function renderInspector() {
    var panel = $("inspector");
    panel.innerHTML = "";
    if (!selectedItem) {
      panel.appendChild(el("p", "muted", "Select an item on the canvas to edit it. Double-click empty space to add a note."));
      return;
    }
    var item = selectedItem;
    var editable = canEdit();
    panel.appendChild(el("h3", null, item.type + " #" + item.id));

    var textLabel = el("label", null, "Text");
    var text = document.createElement("textarea");
    text.rows = 3;
    text.value = item.text || "";
    text.disabled = !editable;
    text.setAttribute("aria-label", "Item text");
    textLabel.appendChild(text);
    panel.appendChild(textLabel);

    var colorLabel = el("label", null, "Colour");
    var color = document.createElement("input");
    color.type = "color";
    color.value = item.color || "#ffd166";
    color.disabled = !editable;
    color.setAttribute("aria-label", "Item colour");
    colorLabel.appendChild(color);
    panel.appendChild(colorLabel);

    var applyBtn = el("button", "btn btn-primary", "Apply changes");
    applyBtn.disabled = !editable;
    applyBtn.addEventListener("click", function () {
      sendItemUpdate(item.id, { text: text.value, color: color.value });
    });
    panel.appendChild(applyBtn);

    var delBtn = el("button", "btn btn-danger", "Delete item");
    delBtn.disabled = !editable;
    delBtn.addEventListener("click", function () {
      sendItemDelete(item.id);
    });
    panel.appendChild(delBtn);

    panel.appendChild(el("p", "muted",
      "Position " + Math.round(item.x) + ", " + Math.round(item.y) +
      " · size " + Math.round(item.w) + " × " + Math.round(item.h)));
  }

  function sendItemUpdate(itemId, patch) {
    if (!sync) { return Promise.reject(new Error("not connected")); }
    var op = { kind: "update", item_id: itemId, patch: patch };
    return sync.sendOp(op).catch(function (err) {
      toast("Update failed: " + err.message, "error");
      return sync.resync().then(function (snap) { boardView.setItems(snap.items); });
    });
  }

  function sendItemDelete(itemId) {
    if (!sync) { return Promise.reject(new Error("not connected")); }
    return sync.sendOp({ kind: "delete", item_id: itemId }).then(function () {
      if (selectedItem && selectedItem.id === itemId) { selectedItem = null; renderInspector(); }
    }).catch(function (err) {
      toast("Delete failed: " + err.message, "error");
      return sync.resync().then(function (snap) { boardView.setItems(snap.items); });
    });
  }

  function setTool(tool) {
    if (boardView) { boardView.setTool(tool); }
    document.querySelectorAll("[data-tool]").forEach(function (btn) {
      var active = btn.getAttribute("data-tool") === tool;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function bindToolbar() {
    document.querySelectorAll("[data-tool]").forEach(function (btn) {
      btn.addEventListener("click", function () { setTool(btn.getAttribute("data-tool")); });
    });
    $("zoom-in").addEventListener("click", function () { if (boardView) { boardView.zoomIn(); } });
    $("zoom-out").addEventListener("click", function () { if (boardView) { boardView.zoomOut(); } });
    $("zoom-fit").addEventListener("click", function () { if (boardView) { boardView.fit(); } });
    $("activity-more").addEventListener("click", function () {
      if (activityOldestId !== null) { loadActivity(activityOldestId); }
    });
    $("back-to-boards").addEventListener("click", function () {
      closeBoard();
      showView("boards");
      loadBoards();
    });
    $("rename-board").addEventListener("click", function () {
      var title = global.prompt("Board title", currentBoard.title);
      if (title && title.trim() && title.trim() !== currentBoard.title) {
        api.updateBoard(currentBoard.id, { title: title.trim() }).then(function (b) {
          currentBoard = b;
          $("board-title").textContent = b.title;
          setRevision(b.revision);
        }, function (err) { toast(err.message, "error"); });
      }
    });
    $("add-member-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var username = $("add-member-username").value.trim();
      if (!username) { return; }
      api.addMember(currentBoard.id, username, $("add-member-role").value).then(function () {
        $("add-member-username").value = "";
        loadMembers();
        toast(username + " added to the board.");
      }, function (err) {
        toast(err.status === 404 ? "No user with that username." : err.message, "error");
      });
    });
  }

  function applyRemoteOp(msg) {
    if (boardView) { boardView.applyOp(msg.op); }
    setRevision(msg.revision);
  }

  function openBoard(boardId) {
    showView("board");
    setStatus("reconnecting", "connecting…");
    $("board-title").textContent = "Loading…";
    $("presence-bar").innerHTML = "";
    $("members-list").innerHTML = "";
    $("activity-list").innerHTML = "";
    $("activity-more").style.display = "";
    activityOldestId = null;
    selectedItem = null;
    cursors = {};

    if (boardView) { boardView.destroy(); boardView = null; }
    if (sync) { sync.close(); sync = null; }

    boardView = CollabBoardView.createBoardView($("board-canvas"), {
      canEdit: canEdit,
      onCreateItem: function (item) {
        return sync.sendOp({ kind: "create", item: item }).then(function (msg) {
          return msg.op.item; // the server-assigned item (with id)
        }).catch(function (err) {
          toast("Could not create item: " + err.message, "error");
          return sync.resync().then(function (snap) { boardView.setItems(snap.items); return null; });
        });
      },
      onUpdateItem: function (itemId, patch) { return sendItemUpdate(itemId, patch); },
      onDeleteItem: function (itemId) { return sendItemDelete(itemId); },
      onCursor: function (x, y) { if (sync) { sync.sendCursor(x, y); } },
      onSelect: function (item) { selectedItem = item; renderInspector(); },
      onToolChange: function (tool) { setTool(tool); },
    });

    sync = CollabSync.createSyncClient(api, boardId, {
      status: function (s) { setStatus(s.state, s.detail); },
      resync: function (snap) {
        currentBoard = snap.board;
        $("board-title").textContent = snap.board.title;
        $("board-desc").textContent = snap.board.description || "";
        setRevision(snap.board.revision);
        members = snap.members;
        renderMembers();
        boardView.setItems(snap.items);
        boardView.fit();
        renderInspector();
        applyToolPermissions();
      },
      welcome: function (w) {
        setRevision(w.revision);
        renderPresence(w.presence);
      },
      op: applyRemoteOp,
      stale: function () { toast("Your edit raced with another — resynced and retried.", "warn"); },
      error: function (m) { toast(m.detail || "Sync error", "error"); },
      presence: function (m) { renderPresence(m.presence); },
      cursor: function (m) {
        if (m.user_id === currentUser.id) { return; }
        cursors[m.user_id] = { username: m.username, x: m.x, y: m.y };
        boardView.setCursors(cursors);
      },
      roleChanged: function (role) {
        currentBoard.role = role;
        toast("Your role is now " + role + ".", "info");
        applyToolPermissions();
        renderInspector();
      },
      memberRemoved: function () {
        toast("You were removed from this board.", "error");
        closeBoard();
        showView("boards");
        loadBoards();
      },
      boardDeleted: function () {
        toast("This board was deleted.", "error");
        closeBoard();
        showView("boards");
        loadBoards();
      },
      boardUpdated: function (m) {
        currentBoard.title = m.board.title;
        currentBoard.description = m.board.description;
        $("board-title").textContent = m.board.title;
        $("board-desc").textContent = m.board.description || "";
        setRevision(m.revision);
      },
      activity: function (m) { prependActivity(m.activity); },
      authFailed: function () {
        toast("You no longer have access to this board.", "error");
        closeBoard();
        showView("boards");
        loadBoards();
      },
    });

    sync.open(api.getToken()).then(function () {
      setTool("select");
      applyToolPermissions();
      loadActivity();
    }, function (err) {
      toast(err.status === 404 ? "Board not found." : err.message, "error");
      closeBoard();
      showView("boards");
      loadBoards();
    });
  }

  function applyToolPermissions() {
    var editable = canEdit();
    document.querySelectorAll("[data-tool]").forEach(function (btn) {
      var tool = btn.getAttribute("data-tool");
      btn.disabled = !editable && tool !== "select" && tool !== "pan";
    });
    $("rename-board").style.display = currentBoard && currentBoard.role === "owner" ? "" : "none";
    $("viewer-banner").hidden = editable;
  }

  function closeBoard() {
    if (sync) { sync.close(); sync = null; }
    if (boardView) { boardView.destroy(); boardView = null; }
    currentBoard = null;
    members = [];
    selectedItem = null;
    cursors = {};
    activityOldestId = null;
  }

  function init() {
    bindAuth();
    bindBoards();
    bindToolbar();
    if (api.isLoggedIn()) {
      api.me().then(function (user) {
        currentUser = user;
        $("user-label").textContent = user.display_name + " (@" + user.username + ")";
        showView("boards");
        loadBoards();
      }, function () { showView("auth"); });
    } else {
      showView("auth");
    }
  }

  global.addEventListener("DOMContentLoaded", init);
})(typeof window !== "undefined" ? window : globalThis);
