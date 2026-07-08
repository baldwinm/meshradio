// Skin picker: apply the chosen skin instantly and remember it in a cookie.
// The server reads that cookie and renders <html data-skin> on the next load,
// so there's no flash of the default skin.
(function () {
  const sel = document.getElementById("skin-select");
  if (!sel) return;
  sel.addEventListener("change", () => {
    const skin = sel.value;
    document.documentElement.dataset.skin = skin;
    document.cookie = "skin=" + skin + ";path=/;max-age=31536000;samesite=lax";
  });
})();
