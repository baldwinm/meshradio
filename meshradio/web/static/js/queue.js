// Queue selection: click (or focus + Enter/Space) a track to select it, then
// act on it from the buttons up top next to "Clear queue". The queue re-renders
// on every state push, so the selected id lives here and is re-applied after
// each swap; a selection that's no longer in the queue quietly clears.
(function () {
  var selectedId = null;

  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll("#queue .queue > li"));
  }

  function apply() {
    var found = false;
    rows().forEach(function (li) {
      var on = li.dataset.trackId === selectedId;
      li.classList.toggle("selected", on);
      li.setAttribute("aria-pressed", on ? "true" : "false");
      if (on) found = true;
    });
    if (!found) selectedId = null;
    document.querySelectorAll("#queue .q-sel-action").forEach(function (b) {
      b.hidden = !selectedId;
    });
  }

  function select(li) {
    selectedId = li.dataset.trackId === selectedId ? null : li.dataset.trackId;
    apply();
  }

  async function act(kind) {
    var li = rows().filter(function (r) { return r.dataset.trackId === selectedId; })[0];
    if (!li) return;
    var index = rows().indexOf(li);          // live position, in case it shifted
    var id = li.dataset.trackId;
    selectedId = null;                        // consumed; drop the highlight
    try {
      var res = await fetch("/api/queue/" + kind + "/" + index + "/" + id, { method: "POST" });
      var q = document.getElementById("queue");
      if (res.ok && q) { q.innerHTML = await res.text(); }  // immediate; state push also refreshes
    } catch (e) {}
    apply();
  }

  document.addEventListener("click", function (e) {
    if (e.target.closest("#queue .q-play-next")) return act("top");
    if (e.target.closest("#queue .q-remove")) return act("remove");
    var li = e.target.closest("#queue .queue > li");
    if (li) select(li);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var li = e.target.closest && e.target.closest("#queue .queue > li");
    if (li) { e.preventDefault(); select(li); }
  });

  // Re-apply the highlight after htmx swaps the queue (state pushes, clear).
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "queue") apply();
  });
})();
