// Easter eggs — all hidden until found, nothing added to the default UI.
//   · Konami code (↑↑↓↓←→←→ B A) unlocks the secret Vaporwave skin
//   · type "llama" for the classic Winamp homage
//   · leave the tab and the title winks at you
//   · a greeting in the devtools console for the curious
// Key sequences are ignored while typing in a field so search etc. stay normal.
(function () {
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "egg-toast";
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => t.remove(), 400);
    }, 2600);
  }

  const KONAMI = ["arrowup","arrowup","arrowdown","arrowdown",
                  "arrowleft","arrowright","arrowleft","arrowright","b","a"];
  let ki = 0;
  let typed = "";

  document.addEventListener("keydown", (e) => {
    const el = document.activeElement;
    if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
    const key = e.key.toLowerCase();

    // Konami → unlock the secret Vaporwave skin
    ki = key === KONAMI[ki] ? ki + 1 : key === KONAMI[0] ? 1 : 0;
    if (ki === KONAMI.length) {
      ki = 0;
      if (window.unlockSecretSkin) {
        window.unlockSecretSkin();
        toast("✨ secret skin unlocked: Vaporwave");
      }
    }

    // Typing "llama" → Winamp homage
    if (key.length === 1) {
      typed = (typed + key).slice(-5);
      if (typed === "llama") toast("🦙 It really whips the llama's ass!");
    }
  });

  // Wink at the title when the tab loses focus.
  const realTitle = document.title;
  document.addEventListener("visibilitychange", () => {
    document.title = document.hidden ? "🎵 still spinning…" : realTitle;
  });

  // A hello for anyone who opens the console.
  try {
    console.log(
      "%c📻 MeshRadio",
      "font:700 20px 'Lucida Console',monospace;color:#00e800;text-shadow:1px 1px 0 #000"
    );
    console.log(
      "%cIt really whips the llama's ass. (Psst — try the Konami code.)",
      "color:#8b93a5"
    );
  } catch (e) {}
})();
