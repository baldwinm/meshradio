// Skin picker: apply the chosen skin instantly and remember it in a cookie.
// The server reads that cookie and renders <html data-skin> on the next load,
// so there's no flash of the default skin.
//
// "vaporwave" is a secret skin unlocked by the Konami code (easter.js calls
// window.unlockSecretSkin). Once found it's remembered in localStorage so the
// option stays in the picker on future visits.
(function () {
  const SECRET = { value: "vaporwave", label: "✨ Vaporwave" };
  const sel = document.getElementById("skin-select");
  if (!sel) return;

  function setCookie(skin) {
    document.cookie = "skin=" + skin + ";path=/;max-age=31536000;samesite=lax";
  }

  function addSecretOption() {
    if (sel.querySelector('option[value="' + SECRET.value + '"]')) return;
    const opt = document.createElement("option");
    opt.value = SECRET.value;
    opt.textContent = SECRET.label;
    sel.appendChild(opt);
  }

  sel.addEventListener("change", () => {
    document.documentElement.dataset.skin = sel.value;
    setCookie(sel.value);
  });

  // Konami unlock: reveal the skin, apply it, and remember it's unlocked.
  window.unlockSecretSkin = function () {
    localStorage.setItem("skinUnlocked", "1");
    addSecretOption();
    document.documentElement.dataset.skin = SECRET.value;
    setCookie(SECRET.value);
    sel.value = SECRET.value;
  };

  // Keep the option available on later visits once it's been found.
  if (localStorage.getItem("skinUnlocked")) addSecretOption();
})();
