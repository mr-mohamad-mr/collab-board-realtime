/* Collab Board — canvas board view.
   Renders items on an HTML5 canvas (world coordinates 0–10,000, the
   same space the API validates), with pan/zoom, drag-to-move,
   resize, double-click to add a note, and live cursors of the other
   members. The server is the source of truth: local edits are sent
   as ops through the callbacks and only applied when the broadcast
   echo comes back — except a drag in progress, which is previewed
   locally and committed once on pointer-up.

   Viewers get a read-only canvas: no drag, no create, no resize.
*/

(function (global) {
  "use strict";

  var CANVAS_MAX = 10000;
  var HANDLE_SIZE = 14;      // screen px, resize handle of the selection
  var CURSOR_COLORS = ["#e5484d", "#2f6feb", "#1e9e62", "#8b5cf6", "#d97706", "#0e7490", "#db2777"];

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  function colorFor(username) {
    var h = 0;
    for (var i = 0; i < username.length; i++) { h = (h * 31 + username.charCodeAt(i)) >>> 0; }
    return CURSOR_COLORS[h % CURSOR_COLORS.length];
  }

  function roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /** Word-wrap `text` to fit `maxWidth` (canvas units, world space). */
  function wrapLines(ctx, text, maxWidth) {
    var lines = [];
    (text || "").split("\n").forEach(function (para) {
      var words = para.split(/\s+/).filter(Boolean);
      if (!words.length) { lines.push(""); return; }
      var line = words[0];
      for (var i = 1; i < words.length; i++) {
        var test = line + " " + words[i];
        if (ctx.measureText(test).width <= maxWidth) { line = test; }
        else { lines.push(line); line = words[i]; }
      }
      lines.push(line);
    });
    return lines;
  }

  function createBoardView(canvas, opts) {
    opts = opts || {};
    var ctx = canvas.getContext("2d");
    var items = [];                 // world-space items, sorted by z
    var cursors = {};               // userId -> {username, x, y}
    var view = { scale: 0.5, ox: 40, oy: 40 };
    var tool = "select";            // select | pan | note | rect | text | circle
    var selectedId = null;
    var drag = null;                // {mode, item, startX, startY, orig, moved}
    var lastCursorSent = 0;
    var destroyed = false;

    function canEdit() {
      return typeof opts.canEdit === "function" ? opts.canEdit() : true;
    }

    // --- coordinate transforms ------------------------------------------
    function toWorld(sx, sy) {
      var r = canvas.getBoundingClientRect();
      return {
        x: (sx - r.left - view.ox) / view.scale,
        y: (sy - r.top - view.oy) / view.scale,
      };
    }
    function toScreen(wx, wy) {
      return { x: wx * view.scale + view.ox, y: wy * view.scale + view.oy };
    }

    // --- rendering -------------------------------------------------------
    function resizeCanvas() {
      var dpr = global.devicePixelRatio || 1;
      var r = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(r.width * dpr));
      canvas.height = Math.max(1, Math.round(r.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function drawGrid() {
      var r = canvas.getBoundingClientRect();
      ctx.fillStyle = "#f4f6f8";
      ctx.fillRect(0, 0, r.width, r.height);
      var step = 100;
      var startX = Math.floor((-view.ox / view.scale) / step) * step;
      var startY = Math.floor((-view.oy / view.scale) / step) * step;
      ctx.fillStyle = "#dbe1e8";
      for (var x = startX; x < CANVAS_MAX + step; x += step) {
        for (var y = startY; y < CANVAS_MAX + step; y += step) {
          var p = toScreen(x, y);
          if (p.x < -5 || p.y < -5 || p.x > r.width + 5 || p.y > r.height + 5) { continue; }
          ctx.fillRect(p.x, p.y, 2, 2);
        }
      }
      // Canvas bounds.
      var tl = toScreen(0, 0);
      var br = toScreen(CANVAS_MAX, CANVAS_MAX);
      ctx.strokeStyle = "#c3ccd6";
      ctx.lineWidth = 1;
      ctx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);
    }

    function drawItemText(item, px, py, pw, ph) {
      var pad = 12 * view.scale;
      var fontPx = Math.max(11, 15 * view.scale);
      ctx.font = fontPx + "px system-ui, sans-serif";
      ctx.fillStyle = "#1c2333";
      ctx.textBaseline = "top";
      var lines = wrapLines(ctx, item.text || "", pw - pad * 2);
      var lineH = fontPx * 1.35;
      var maxLines = Math.max(1, Math.floor((ph - pad * 2) / lineH));
      lines.slice(0, maxLines).forEach(function (line, i) {
        ctx.fillText(line, px + pad, py + pad + i * lineH);
      });
    }

    function drawItem(item) {
      var tl = toScreen(item.x, item.y);
      var pw = item.w * view.scale;
      var ph = item.h * view.scale;
      if (item.type === "text") {
        ctx.font = "600 " + Math.max(12, 20 * view.scale) + "px system-ui, sans-serif";
        ctx.fillStyle = item.color || "#1c2333";
        ctx.textBaseline = "top";
        var lines = wrapLines(ctx, item.text || "", pw);
        var lineH = Math.max(12, 20 * view.scale) * 1.3;
        lines.forEach(function (line, i) { ctx.fillText(line, tl.x, tl.y + i * lineH); });
        return;
      }
      ctx.fillStyle = item.color || "#ffd166";
      ctx.strokeStyle = "rgba(28,35,51,0.35)";
      ctx.lineWidth = 1;
      if (item.type === "circle") {
        ctx.beginPath();
        ctx.ellipse(tl.x + pw / 2, tl.y + ph / 2, pw / 2, ph / 2, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      } else if (item.type === "rect") {
        ctx.globalAlpha = 0.85;
        ctx.fillRect(tl.x, tl.y, pw, ph);
        ctx.globalAlpha = 1;
        ctx.strokeRect(tl.x, tl.y, pw, ph);
      } else { // note
        roundRect(ctx, tl.x, tl.y, pw, ph, 8 * view.scale);
        ctx.fill();
        ctx.stroke();
      }
      if (item.type !== "circle") { drawItemText(item, tl.x, tl.y, pw, ph); }
      else if (item.text) { drawItemText(item, tl.x, tl.y, pw, ph); }
    }

    function drawSelection() {
      if (selectedId == null) { return; }
      var item = items.find(function (it) { return it.id === selectedId; });
      if (!item) { return; }
      var tl = toScreen(item.x, item.y);
      var pw = item.w * view.scale;
      var ph = item.h * view.scale;
      ctx.strokeStyle = "#1f3a5f";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(tl.x - 3, tl.y - 3, pw + 6, ph + 6);
      ctx.setLineDash([]);
      if (canEdit()) {
        ctx.fillStyle = "#1f3a5f";
        ctx.fillRect(tl.x + pw - HANDLE_SIZE / 2, tl.y + ph - HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE);
      }
    }

    function drawCursors() {
      Object.keys(cursors).forEach(function (userId) {
        var c = cursors[userId];
        var p = toScreen(c.x, c.y);
        var color = colorFor(c.username || "?");
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(p.x + 14, p.y + 14);
        ctx.lineTo(p.x + 8, p.y + 15);
        ctx.lineTo(p.x + 5, p.y + 21);
        ctx.lineTo(p.x + 2, p.y + 20);
        ctx.lineTo(p.x + 5, p.y + 14);
        ctx.lineTo(p.x, p.y + 14);
        ctx.closePath();
        ctx.fill();
        ctx.font = "600 12px system-ui, sans-serif";
        var label = c.username || "?";
        var w = ctx.measureText(label).width + 12;
        ctx.fillStyle = color;
        roundRect(ctx, p.x + 14, p.y + 16, w, 20, 10);
        ctx.fill();
        ctx.fillStyle = "#ffffff";
        ctx.textBaseline = "middle";
        ctx.fillText(label, p.x + 20, p.y + 26);
        ctx.textBaseline = "alphabetic";
      });
    }

    function draw() {
      if (destroyed) { return; }
      drawGrid();
      items.forEach(drawItem);
      drawSelection();
      drawCursors();
    }

    // --- hit testing ------------------------------------------------------
    function hitItem(wx, wy) {
      for (var i = items.length - 1; i >= 0; i--) {
        var it = items[i];
        if (wx >= it.x && wx <= it.x + it.w && wy >= it.y && wy <= it.y + it.h) { return it; }
      }
      return null;
    }

    function onResizeHandle(wx, wy) {
      if (selectedId == null || !canEdit()) { return false; }
      var item = items.find(function (it) { return it.id === selectedId; });
      if (!item) { return false; }
      var p = toScreen(item.x + item.w, item.y + item.h);
      var q = toScreen(wx, wy);
      return Math.abs(q.x - p.x) <= HANDLE_SIZE && Math.abs(q.y - p.y) <= HANDLE_SIZE;
    }

    function select(id) {
      selectedId = id;
      draw();
      if (typeof opts.onSelect === "function") {
        var item = id == null ? null : items.find(function (it) { return it.id === id; }) || null;
        opts.onSelect(item);
      }
    }

    // --- pointer interaction ----------------------------------------------
    function pointerPos(ev) {
      var r = canvas.getBoundingClientRect();
      return { sx: ev.clientX - r.left, sy: ev.clientY - r.top };
    }

    function onPointerDown(ev) {
      canvas.focus();
      var pos = pointerPos(ev);
      var w = toWorld(ev.clientX, ev.clientY);
      if (tool === "pan") {
        drag = { mode: "pan", startSX: pos.sx, startSY: pos.sy, ox: view.ox, oy: view.oy, moved: false };
        canvas.setPointerCapture(ev.pointerId);
        return;
      }
      if (tool !== "select") {
        // Click-to-place tools: create the item at the click point.
        if (!canEdit()) { return; }
        var defaults = {
          note: { w: 240, h: 160, text: "New note", color: "#ffd166" },
          rect: { w: 240, h: 160, text: "", color: "#8ecae6" },
          text: { w: 320, h: 60, text: "Text", color: "#1c2333" },
          circle: { w: 160, h: 160, text: "", color: "#b7e4c7" },
        }[tool];
        var item = {
          type: tool,
          x: clamp(Math.round(w.x - defaults.w / 2), 0, CANVAS_MAX - defaults.w),
          y: clamp(Math.round(w.y - defaults.h / 2), 0, CANVAS_MAX - defaults.h),
          w: defaults.w, h: defaults.h, text: defaults.text, color: defaults.color, z: 0,
        };
        if (typeof opts.onCreateItem === "function") {
          opts.onCreateItem(item).then(function (created) { if (created) { select(created.id); } });
        }
        setTool("select");
        return;
      }
      if (onResizeHandle(w.x, w.y)) {
        var it = items.find(function (x) { return x.id === selectedId; });
        drag = { mode: "resize", item: it, startX: w.x, startY: w.y, orig: { w: it.w, h: it.h }, moved: false };
        canvas.setPointerCapture(ev.pointerId);
        return;
      }
      var hit = hitItem(w.x, w.y);
      if (hit) {
        select(hit.id);
        if (canEdit()) {
          drag = {
            mode: "move", item: hit, startX: w.x, startY: w.y,
            orig: { x: hit.x, y: hit.y }, moved: false,
          };
          canvas.setPointerCapture(ev.pointerId);
        }
      } else {
        select(null);
        drag = { mode: "pan", startSX: pos.sx, startSY: pos.sy, ox: view.ox, oy: view.oy, moved: false };
        canvas.setPointerCapture(ev.pointerId);
      }
    }

    function onPointerMove(ev) {
      var pos = pointerPos(ev);
      var w = toWorld(ev.clientX, ev.clientY);
      // Live cursor relay (throttled; the server caps at 20/s).
      var now = Date.now();
      if (now - lastCursorSent > 50 && typeof opts.onCursor === "function") {
        lastCursorSent = now;
        opts.onCursor(clamp(w.x, 0, CANVAS_MAX), clamp(w.y, 0, CANVAS_MAX));
      }
      if (!drag) {
        canvas.style.cursor = tool === "pan" ? "grab" : (hitItem(w.x, w.y) ? "move" : "default");
        return;
      }
      if (drag.mode === "pan") {
        view.ox = drag.ox + (pos.sx - drag.startSX);
        view.oy = drag.oy + (pos.sy - drag.startSY);
        drag.moved = true;
        draw();
        return;
      }
      if (drag.mode === "move") {
        var dx = w.x - drag.startX;
        var dy = w.y - drag.startY;
        if (Math.abs(dx) + Math.abs(dy) > 2) { drag.moved = true; }
        drag.item.x = clamp(Math.round(drag.orig.x + dx), 0, CANVAS_MAX - drag.item.w);
        drag.item.y = clamp(Math.round(drag.orig.y + dy), 0, CANVAS_MAX - drag.item.h);
        draw();
        return;
      }
      if (drag.mode === "resize") {
        drag.item.w = clamp(Math.round(drag.orig.w + (w.x - drag.startX)), 40, CANVAS_MAX - drag.item.x);
        drag.item.h = clamp(Math.round(drag.orig.h + (w.y - drag.startY)), 40, CANVAS_MAX - drag.item.y);
        drag.moved = true;
        draw();
      }
    }

    function onPointerUp(ev) {
      if (!drag) { return; }
      var d = drag;
      drag = null;
      if (d.mode === "move" && d.moved && typeof opts.onUpdateItem === "function") {
        opts.onUpdateItem(d.item.id, { x: d.item.x, y: d.item.y });
      } else if (d.mode === "resize" && d.moved && typeof opts.onUpdateItem === "function") {
        opts.onUpdateItem(d.item.id, { w: d.item.w, h: d.item.h });
      }
      draw();
    }

    function onWheel(ev) {
      ev.preventDefault();
      var factor = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
      var pos = pointerPos(ev);
      var before = toWorld(ev.clientX, ev.clientY);
      view.scale = clamp(view.scale * factor, 0.05, 3);
      view.ox = pos.sx - before.x * view.scale;
      view.oy = pos.sy - before.y * view.scale;
      draw();
    }

    function onDblClick(ev) {
      if (!canEdit() || tool !== "select") { return; }
      var w = toWorld(ev.clientX, ev.clientY);
      if (hitItem(w.x, w.y)) { return; } // double-click on an item: no-op
      var item = {
        type: "note",
        x: clamp(Math.round(w.x - 120), 0, CANVAS_MAX - 240),
        y: clamp(Math.round(w.y - 80), 0, CANVAS_MAX - 160),
        w: 240, h: 160, text: "New note", color: "#ffd166", z: 0,
      };
      if (typeof opts.onCreateItem === "function") {
        opts.onCreateItem(item).then(function (created) { if (created) { select(created.id); } });
      }
    }

    function onKeyDown(ev) {
      if (ev.key === "Delete" || ev.key === "Backspace") {
        if (selectedId != null && canEdit() && typeof opts.onDeleteItem === "function") {
          var id = selectedId;
          select(null);
          opts.onDeleteItem(id);
          ev.preventDefault();
        }
      } else if (ev.key === "Escape") {
        select(null);
        setTool("select");
      }
    }

    // --- public API -------------------------------------------------------
    function setTool(next) {
      tool = next;
      canvas.style.cursor = next === "pan" ? "grab" : "default";
      if (typeof opts.onToolChange === "function") { opts.onToolChange(next); }
    }

    function fit() {
      var r = canvas.getBoundingClientRect();
      if (!items.length) {
        view.scale = 0.5;
        view.ox = (r.width - 1000 * view.scale) / 2;
        view.oy = (r.height - 1000 * view.scale) / 2;
        draw();
        return;
      }
      var minX = Math.min.apply(null, items.map(function (i) { return i.x; }));
      var minY = Math.min.apply(null, items.map(function (i) { return i.y; }));
      var maxX = Math.max.apply(null, items.map(function (i) { return i.x + i.w; }));
      var maxY = Math.max.apply(null, items.map(function (i) { return i.y + i.h; }));
      var bw = Math.max(200, maxX - minX);
      var bh = Math.max(200, maxY - minY);
      view.scale = clamp(Math.min((r.width - 80) / bw, (r.height - 80) / bh), 0.05, 1.5);
      view.ox = (r.width - bw * view.scale) / 2 - minX * view.scale;
      view.oy = (r.height - bh * view.scale) / 2 - minY * view.scale;
      draw();
    }

    function zoom(factor) {
      var r = canvas.getBoundingClientRect();
      var cx = r.width / 2;
      var cy = r.height / 2;
      var before = { x: (cx - view.ox) / view.scale, y: (cy - view.oy) / view.scale };
      view.scale = clamp(view.scale * factor, 0.05, 3);
      view.ox = cx - before.x * view.scale;
      view.oy = cy - before.y * view.scale;
      draw();
    }

    function setItems(next) {
      items = (next || []).slice().sort(function (a, b) { return (a.z - b.z) || (a.id - b.id); });
      if (selectedId != null && !items.some(function (i) { return i.id === selectedId; })) {
        selectedId = null;
        if (typeof opts.onSelect === "function") { opts.onSelect(null); }
      }
      draw();
    }

    /** Apply one broadcast op (create/update/delete) to local state. */
    function applyOp(op) {
      if (!op) { return; }
      if (op.kind === "create" && op.item) {
        if (!items.some(function (i) { return i.id === op.item.id; })) { items.push(op.item); }
      } else if (op.kind === "update" && op.item) {
        var idx = items.findIndex(function (i) { return i.id === op.item.id; });
        if (idx >= 0) { items[idx] = op.item; }
        else { items.push(op.item); } // missed the create; take the update
      } else if (op.kind === "delete" && op.item_id != null) {
        items = items.filter(function (i) { return i.id !== op.item_id; });
        if (selectedId === op.item_id) { selectedId = null; }
      }
      setItems(items);
    }

    function setCursors(next) {
      cursors = next || {};
      draw();
    }

    function setCursor(userId, username, x, y) {
      cursors[userId] = { username: username, x: x, y: y };
      draw();
    }

    function removeCursor(userId) {
      delete cursors[userId];
      draw();
    }

    var resizeObserver = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(function () { resizeCanvas(); });
      resizeObserver.observe(canvas);
    }

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDblClick);
    canvas.addEventListener("keydown", onKeyDown);
    resizeCanvas();

    return {
      setItems: setItems,
      applyOp: applyOp,
      setCursors: setCursors,
      setCursor: setCursor,
      removeCursor: removeCursor,
      setTool: setTool,
      getTool: function () { return tool; },
      fit: fit,
      zoomIn: function () { zoom(1.25); },
      zoomOut: function () { zoom(1 / 1.25); },
      getZoom: function () { return Math.round(view.scale * 100); },
      getSelected: function () {
        return selectedId == null ? null : items.find(function (i) { return i.id === selectedId; }) || null;
      },
      select: select,
      draw: draw,
      destroy: function () {
        destroyed = true;
        if (resizeObserver) { resizeObserver.disconnect(); }
        canvas.removeEventListener("pointerdown", onPointerDown);
        canvas.removeEventListener("pointermove", onPointerMove);
        canvas.removeEventListener("pointerup", onPointerUp);
        canvas.removeEventListener("pointercancel", onPointerUp);
        canvas.removeEventListener("wheel", onWheel);
        canvas.removeEventListener("dblclick", onDblClick);
        canvas.removeEventListener("keydown", onKeyDown);
      },
    };
  }

  global.CollabBoardView = { createBoardView: createBoardView };
})(typeof window !== "undefined" ? window : globalThis);
