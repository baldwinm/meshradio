// Embed mode: where the YouTube player window sits. On Now Playing it lies
// over the card's #yt-slot, so the video is the cover art; on every other
// page it has no slot and the CSS docks it (style.css, "the player window's
// place").
//
// It's laid over the slot rather than moved into it because the card is
// re-rendered on every state push, and moving an iframe reloads it: the song
// would restart. The window stays where it is in the document; only its
// position follows the slot.
(function () {
  var win = document.getElementById("yt-window");
  if (!win) return;
  var queued = false;

  function place() {
    queued = false;
    var slot = document.querySelector("main.page-now #yt-slot");
    if (!slot) {
      if (win.classList.contains("in-slot")) {
        win.classList.remove("in-slot");
        win.style.top = win.style.left = win.style.width = win.style.height = "";
      }
      return;
    }
    var r = slot.getBoundingClientRect();
    win.classList.add("in-slot");
    win.style.top = (r.top + window.scrollY) + "px";
    win.style.left = (r.left + window.scrollX) + "px";
    win.style.width = r.width + "px";
    win.style.height = r.height + "px";
  }

  function soon() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(place);
  }

  place();
  window.addEventListener("resize", soon);
  window.addEventListener("load", soon);
  ["htmx:afterSettle", "htmx:oobAfterSwap", "htmx:afterSwap"].forEach(function (name) {
    document.body.addEventListener(name, soon);
  });
  // Anything above the card changing height (a shows line arriving, the
  // banner wrapping) moves the slot without a swap of its own.
  if (window.ResizeObserver) new ResizeObserver(soon).observe(document.body);
})();
