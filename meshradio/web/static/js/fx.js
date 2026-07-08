// Misc client-side flourishes.
(function () {
  var u = function (b) { return decodeURIComponent(escape(atob(b))); };

  function n(m) {
    var t = document.createElement("div");
    t.className = "mr-toast";
    t.textContent = m;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 400);
    }, 2600);
  }

  var q = [38, 38, 40, 40, 37, 39, 37, 39, 66, 65];
  var i = 0, s = "", w = u("bGxhbWE=");

  document.addEventListener("keydown", function (e) {
    var a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
    var c = e.keyCode || e.which;

    i = c === q[i] ? i + 1 : c === q[0] ? 1 : 0;
    if (i === q.length) {
      i = 0;
      if (window.__mrx) { window.__mrx(); n(u("4pyoIHNlY3JldCBza2luIHVubG9ja2VkOiBWYXBvcndhdmU=")); }
    }

    var k = e.key;
    if (k && k.length === 1) {
      s = (s + k.toLowerCase()).slice(-w.length);
      if (s === w) n(u("8J+mmSBJdCByZWFsbHkgd2hpcHMgdGhlIGxsYW1hJ3MgYXNzIQ=="));
    }
  });

  var r = document.title;
  document.addEventListener("visibilitychange", function () {
    document.title = document.hidden ? u("8J+OtSBzdGlsbCBzcGlubmluZ+KApg==") : r;
  });
})();
