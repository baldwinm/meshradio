// Every htmx button says what happened. Most of them swap nothing on the page
// they're on ("+ queue" from the archive puts a song in a queue on another
// page), so without this a press looked like it did nothing, and a refused
// one (rate limit, server error) was swallowed silently.
//
// A button opts in with data attributes:
//   data-done="✓ Queued"   its label after it worked (and it stays disabled)
//   data-toast="Queued: x"  a notice at the bottom of the screen
// Failures need no opt-in: any htmx request that fails says so.
(function () {
  function toast(m) { if (window.mrToast) window.mrToast(m); }

  document.body.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail.elt;
    if (!e.detail.successful) return;        // the error handlers below speak
    if (!(el instanceof HTMLElement)) return;
    if (el.dataset.done) {
      el.textContent = el.dataset.done;
      el.disabled = true;
      el.classList.add("done");
    }
    if (el.dataset.toast) toast(el.dataset.toast);
  });

  document.body.addEventListener("htmx:responseError", function (e) {
    var status = e.detail.xhr ? e.detail.xhr.status : 0;
    toast(status === 429 ? "Too many clicks. Wait a moment and try again."
                         : "That didn't go through. Try again in a moment.");
  });
  document.body.addEventListener("htmx:sendError", function () {
    toast("Can't reach MeshRadio right now. Check your connection.");
  });
})();
