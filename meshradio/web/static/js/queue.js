// Queue Edit mode. Listening, the queue is just songs; ✎ Edit shows a
// checkbox and a drag handle on every row and a bar of actions that apply to
// the checked songs (Play next, Remove, Select all, Clear queue). Rows move
// by dragging the handle, or with Alt+↑ / Alt+↓ on a focused row. Every
// change answers with a notice carrying Undo, so none asks "are you sure?".
//
// The queue partial is re-rendered on every state push, inside the same
// #queue container, so Edit mode is a class on that container and the
// selection lives here, re-applied after each swap. A swap is held off while
// a row is being dragged: the drop's own answer brings the queue up to date.
(function () {
  var editing = false;
  var selected = new Set();       // track ids (strings)
  var anchor = null;              // last row clicked, for Shift-click ranges
  var drag = null;

  function box() { return document.getElementById("queue"); }
  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll("#queue .queue > li"));
  }
  function idOf(li) { return li.dataset.trackId; }
  function titleOf(li) {
    var t = li && li.querySelector(".title");
    return t ? t.textContent.trim() : "Song";
  }
  function songs(n) { return n + (n === 1 ? " song" : " songs"); }
  function toast(m) { if (window.mrToast) window.mrToast(m); }

  // -- state onto the page ----------------------------------------------------

  function apply() {
    var q = box();
    if (!q) return;
    var list = rows();
    var present = new Set(list.map(idOf));
    selected.forEach(function (id) { if (!present.has(id)) selected.delete(id); });
    if (!list.length) editing = false;
    q.classList.toggle("editing", editing);
    list.forEach(function (li) {
      var on = selected.has(idOf(li));
      li.classList.toggle("selected", on);
      var cb = li.querySelector(".q-check");
      if (cb) cb.checked = on;
    });
    var n = selected.size;
    var count = q.querySelector(".q-count");
    if (count) count.textContent = n ? n + " selected" : "None selected";
    q.querySelectorAll(".q-next, .q-remove").forEach(function (b) { b.disabled = !n; });
    var rn = q.querySelector(".q-remove .q-n");
    if (rn) rn.textContent = n ? " (" + n + ")" : "";
    var all = q.querySelector(".q-all");
    if (all) {
      var every = n > 0 && n === list.length;
      all.innerHTML = every ? "☑<span class=\"q-l\"> Select none</span>"
                            : "☐<span class=\"q-l\"> Select all</span>";
    }
    var edit = q.querySelector(".q-edit");
    if (edit) edit.setAttribute("aria-pressed", editing ? "true" : "false");
  }

  function setEditing(on) {
    editing = on;
    if (!on) { selected.clear(); anchor = null; }
    apply();
    var q = box();
    if (!q) return;
    var focus = q.querySelector(on ? ".queue .q-check" : ".q-edit");
    if (focus) focus.focus({ preventScroll: true });
  }

  function select(li, shift) {
    var id = idOf(li);
    if (shift && anchor) {
      var list = rows().map(idOf);
      var a = list.indexOf(anchor), b = list.indexOf(id);
      if (a >= 0 && b >= 0) {
        var lo = Math.min(a, b), hi = Math.max(a, b);
        for (var i = lo; i <= hi; i++) selected.add(list[i]);
        apply();
        return;
      }
    }
    if (selected.has(id)) selected.delete(id); else selected.add(id);
    anchor = id;
    apply();
  }

  // -- talking to the server ----------------------------------------------------

  async function edit(url, message, focusId) {
    try {
      var res = await fetch(url, { method: "POST" });
      if (!res.ok) {
        toast(res.status === 429 ? "Too many clicks. Wait a moment and try again."
                                 : "That didn't go through. Try again in a moment.");
        return;
      }
      var q = box();
      if (q) q.innerHTML = await res.text();   // now; the state push also refreshes
      apply();
      if (focusId) {
        var cb = document.querySelector('#queue li[data-track-id="' + focusId + '"] .q-check');
        if (cb) cb.focus({ preventScroll: true });
      }
      var token = res.headers.get("X-Queue-Undo");
      var count = parseInt(res.headers.get("X-Queue-Count") || "0", 10);
      if (token) undoToast(message(count), token);
    } catch (e) {
      toast("Can't reach MeshRadio right now. Check your connection.");
    }
  }

  function picked() {
    // In the queue's order, which is the order Play next keeps them in.
    return rows().map(idOf).filter(function (id) { return selected.has(id); });
  }

  function removeSelected() {
    var ids = picked();
    if (!ids.length) return;
    ids.forEach(function (id) { selected.delete(id); });
    edit("/api/queue/edit/remove?ids=" + ids.join(","), function (n) {
      return "Removed " + songs(n);
    });
  }

  function playNextSelected() {
    var ids = picked();
    if (!ids.length) return;
    var only = ids.length === 1
      ? titleOf(document.querySelector('#queue li[data-track-id="' + ids[0] + '"]')) : null;
    selected.clear();
    edit("/api/queue/edit/next?ids=" + ids.join(","), function (n) {
      return only ? "Up next: " + only : songs(n) + " up next";
    });
  }

  function clearAll() {
    selected.clear();
    edit("/api/queue/edit/clear", function (n) { return "Cleared " + songs(n); });
  }

  function move(li, to) {
    var name = titleOf(li);
    edit("/api/queue/edit/move/" + idOf(li) + "/" + to, function () {
      return "Moved " + name;
    }, idOf(li));
  }

  // A notice with an Undo button, which stays a little longer than a plain one
  // so there's time to reach it. A newer notice replaces it.
  var undoEl = null, undoTimer = null;
  function undoToast(message, token) {
    if (undoEl) undoEl.remove();
    clearTimeout(undoTimer);
    var t = document.createElement("div");
    t.className = "mr-toast has-action";
    t.setAttribute("role", "status");
    var span = document.createElement("span");
    span.textContent = message;
    var b = document.createElement("button");
    b.type = "button";
    b.className = "toast-undo";
    b.textContent = "Undo";
    b.addEventListener("click", function () {
      hide();
      edit("/api/queue/edit/undo/" + token, function () { return ""; });
      toast("Undone");
    });
    t.appendChild(span);
    t.appendChild(b);
    document.body.appendChild(t);
    undoEl = t;
    requestAnimationFrame(function () { t.classList.add("show"); });
    function hide() {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 400);
      if (undoEl === t) undoEl = null;
    }
    undoTimer = setTimeout(hide, 6000);
  }

  // -- dragging -------------------------------------------------------------------

  function startDrag(e, li) {
    if (drag || e.button > 0) return;
    e.preventDefault();
    var list = li.parentElement;
    var all = rows();
    drag = {
      li: li,
      list: list,
      rows: all,
      from: all.indexOf(li),
      to: all.indexOf(li),
      mids: all.map(function (r) {
        var b = r.getBoundingClientRect();
        return b.top + b.height / 2;
      }),
      height: li.getBoundingClientRect().height,
      startY: e.clientY,
      startScroll: list.scrollTop,
      pointer: e.pointerId,
    };
    e.target.setPointerCapture(e.pointerId);
    li.classList.add("dragging");
    list.classList.add("drag-active");
  }

  function dragMove(e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    var list = drag.list;
    var lb = list.getBoundingClientRect();
    // Keep the list scrolling when the row is held near its top or bottom edge.
    if (e.clientY < lb.top + 28) list.scrollTop -= 8;
    else if (e.clientY > lb.bottom - 28) list.scrollTop += 8;
    var scrolled = list.scrollTop - drag.startScroll;
    var dy = e.clientY - drag.startY + scrolled;
    drag.li.style.transform = "translateY(" + dy + "px)";
    var centre = drag.mids[drag.from] + dy - scrolled;
    var to = drag.from;
    for (var i = 0; i < drag.mids.length; i++) {
      if (i === drag.from) continue;
      var mid = drag.mids[i] - scrolled;
      if (i < drag.from && centre < mid) { to = Math.min(to, i); }
      if (i > drag.from && centre > mid) { to = Math.max(to, i); }
    }
    drag.to = to;
    drag.rows.forEach(function (r, i) {
      if (r === drag.li) return;
      var shift = 0;
      if (drag.from < to && i > drag.from && i <= to) shift = -drag.height;
      if (to < drag.from && i >= to && i < drag.from) shift = drag.height;
      r.style.transform = shift ? "translateY(" + shift + "px)" : "";
    });
  }

  function endDrag(e, cancelled) {
    if (!drag || (e && e.pointerId !== drag.pointer)) return;
    var d = drag;
    drag = null;
    d.rows.forEach(function (r) { r.style.transform = ""; });
    d.li.classList.remove("dragging");
    d.list.classList.remove("drag-active");
    if (!cancelled && d.to !== d.from) move(d.li, d.to);
  }

  // -- wiring -----------------------------------------------------------------

  document.addEventListener("click", function (e) {
    var q = e.target.closest && e.target.closest("#queue");
    if (!q) return;
    if (e.target.closest(".q-edit")) return setEditing(!editing);
    if (e.target.closest(".q-done")) return setEditing(false);
    if (e.target.closest(".q-next")) return playNextSelected();
    if (e.target.closest(".q-remove")) return removeSelected();
    if (e.target.closest(".q-clear")) return clearAll();
    if (e.target.closest(".q-all")) {
      var list = rows();
      if (selected.size === list.length) selected.clear();
      else list.forEach(function (li) { selected.add(idOf(li)); });
      return apply();
    }
    if (!editing) return;
    var li = e.target.closest(".queue > li");
    if (!li || e.target.closest(".q-handle")) return;
    // A click on the box itself has already ticked it; select() lands on the
    // same state (and apply() sets every box in a Shift-range).
    select(li, e.shiftKey);
  });

  document.addEventListener("keydown", function (e) {
    if (!editing) return;
    if (e.key === "Escape" && !document.querySelector("dialog[open]")) {
      return setEditing(false);
    }
    if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    var li = e.target.closest && e.target.closest("#queue .queue > li");
    if (!li) return;
    e.preventDefault();
    var list = rows();
    var to = list.indexOf(li) + (e.key === "ArrowUp" ? -1 : 1);
    if (to < 0 || to >= list.length) return;
    move(li, to);
  });

  document.addEventListener("pointerdown", function (e) {
    if (!editing) return;
    var handle = e.target.closest && e.target.closest("#queue .q-handle");
    if (handle) startDrag(e, handle.closest("li"));
  });
  document.addEventListener("pointermove", dragMove);
  document.addEventListener("pointerup", function (e) { endDrag(e, false); });
  document.addEventListener("pointercancel", function (e) { endDrag(e, true); });

  // Hold off re-renders of the queue mid-drag (a state push, a shuffle).
  function holdWhileDragging(e) {
    if (drag && e.detail && e.detail.target && e.detail.target.id === "queue") {
      e.detail.shouldSwap = false;
    }
  }
  document.body.addEventListener("htmx:oobBeforeSwap", holdWhileDragging);
  document.body.addEventListener("htmx:beforeSwap", holdWhileDragging);

  // A re-render replaces the row that had focus; put focus back on the same
  // song so a keyboard user can keep nudging it with Alt+↑ / Alt+↓.
  var focusedId = null;
  document.addEventListener("focusin", function (e) {
    var li = e.target.closest && e.target.closest("#queue .queue > li");
    focusedId = li ? idOf(li) : null;
  });

  function onSwap(e) {
    if (!e.target || e.target.id !== "queue") return;
    apply();
    if (editing && focusedId && !box().contains(document.activeElement)) {
      var cb = box().querySelector('li[data-track-id="' + focusedId + '"] .q-check');
      if (cb) cb.focus({ preventScroll: true });
    }
  }
  document.body.addEventListener("htmx:afterSwap", onSwap);
  document.body.addEventListener("htmx:oobAfterSwap", onSwap);
  // Back to Now Playing from another page: a fresh #queue, still listening.
  document.body.addEventListener("htmx:afterSettle", function () {
    if (!box()) { editing = false; selected.clear(); }
    apply();
  });
})();
