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
  var i = 0, s = "";
  var w1 = u("bGxhbWE=");   // llama
  var w2 = u("ZGlzY28=");   // disco toggle

  // Toggle the full-page flourish and a floating YouTube guest (the official
  // clip, so the artist gets the view + ad revenue). Mutes the in-app player
  // while the guest is on so audio doesn't overlap, and restores it after.
  var D = null;
  function fx1(on) {
    var h = document.documentElement;
    if (on) {
      h.classList.add("mr-fx1");
      if (!D) {
        D = document.createElement("div");
        D.className = "mr-fx1-v";
        var f = document.createElement("iframe");
        f.src = "https://www.youtube.com/embed/" + u("eEZyR3V5dzFWOHM=") + "?autoplay=1&rel=0";
        f.allow = "autoplay; encrypted-media; fullscreen";
        f.setAttribute("allowfullscreen", "");
        D.appendChild(f);
        document.body.appendChild(D);
      }
      try { if (typeof ytPlayer !== "undefined" && ytPlayer) ytPlayer.mute(); } catch (e) {}
    } else {
      h.classList.remove("mr-fx1");
      if (D) { D.remove(); D = null; }
      try { if (typeof ytPlayer !== "undefined" && ytPlayer) ytPlayer.unMute(); } catch (e) {}
    }
  }

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
      s = (s + k.toLowerCase()).slice(-16);
      if (s.slice(-w1.length) === w1) {
        n(u("8J+mmSBJdCByZWFsbHkgd2hpcHMgdGhlIGxsYW1hJ3MgYXNzIQ=="));
      } else if (s.slice(-w2.length) === w2) {
        fx1(!document.documentElement.classList.contains("mr-fx1"));
        n(u("8J+SgyBEYW5jaW5nIFF1ZWVuIOKAlCBkaXNjbyE="));
      }
    }
  });

  var r = document.title;
  document.addEventListener("visibilitychange", function () {
    document.title = document.hidden ? u("8J+OtSBzdGlsbCBzcGlubmluZ+KApg==") : r;
  });
})();
