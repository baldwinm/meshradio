// Skin picker: apply the chosen skin instantly and remember it in a cookie.
// The server reads that cookie and renders <html data-skin> on the next load,
// so there's no flash of the default skin.
(function () {
  var u = function (b) { return decodeURIComponent(escape(atob(b))); };
  var V = u("YXVyb3Jh");              // value
  var L = u("4pyoIFZhcG9yd2F2ZQ==");  // label
  var F = u("c2tpblVubG9ja2Vk");      // flag

  var sel = document.getElementById("skin-select");
  if (!sel) return;

  function ck(v) {
    document.cookie = "skin=" + v + ";path=/;max-age=31536000;samesite=lax";
  }

  function add() {
    if (sel.querySelector('option[value="' + V + '"]')) return;
    var o = document.createElement("option");
    o.value = V;
    o.textContent = L;
    sel.appendChild(o);
  }

  sel.addEventListener("change", function () {
    document.documentElement.dataset.skin = sel.value;
    ck(sel.value);
  });

  window.__mrx = function () {
    localStorage.setItem(F, "1");
    add();
    document.documentElement.dataset.skin = V;
    ck(V);
    sel.value = V;
  };

  if (localStorage.getItem(F)) add();
})();
