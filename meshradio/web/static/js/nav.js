// Keep the header's current-section marker honest.
//
// The nav is hx-boosted: a click swaps <main> and leaves the <header> alone, so
// the aria-current the server rendered would still point at the page you came
// from. The URL is the truth after a boost, so recompute from it — on htmx
// settles and on back/forward, which restore <main> the same way.
(function () {
  function mark() {
    var path = location.pathname;
    var links = document.querySelectorAll("header nav a[href]");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href");
      // Same rule as base.html: exact match, or a section prefix so
      // /archive/themes and /archive/2026-08-01 both light up "Archive".
      var current = path === href || (href !== "/" && path.indexOf(href) === 0);
      if (current) links[i].setAttribute("aria-current", "page");
      else links[i].removeAttribute("aria-current");
    }
  }

  document.body.addEventListener("htmx:afterSettle", mark);
  window.addEventListener("popstate", mark);
})();
