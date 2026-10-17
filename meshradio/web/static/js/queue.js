// Queue row actions: ⤒ (play next) and ✕ (remove) on each row. The queue
// re-renders on every state push, so the listener sits on the document and
// the row's live position is read at click time, in case it shifted.
(function () {
  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll("#queue .queue > li"));
  }

  async function act(kind, li) {
    var index = rows().indexOf(li);
    var id = li.dataset.trackId;
    if (index < 0) return;
    var title = (li.querySelector(".title") || {}).textContent || "Song";
    try {
      var res = await fetch("/api/queue/" + kind + "/" + index + "/" + id, { method: "POST" });
      var q = document.getElementById("queue");
      if (res.ok && q) {
        q.innerHTML = await res.text();      // immediate; the state push also refreshes
        if (window.mrToast)
          window.mrToast(kind === "top" ? "Up next: " + title : "Removed: " + title);
      } else if (window.mrToast) {
        window.mrToast("That didn't go through. Try again in a moment.");
      }
    } catch (e) {
      if (window.mrToast) window.mrToast("Can't reach MeshRadio right now. Check your connection.");
    }
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("#queue .q-btn");
    if (!btn) return;
    var li = btn.closest("li");
    if (li) act(btn.classList.contains("q-remove") ? "remove" : "top", li);
  });
})();
