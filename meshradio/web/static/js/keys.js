// Keyboard shortcuts for the player.
//
// Everything here drives the *existing* controls rather than a private path to
// the server: play/pause and skip click the htmx buttons (so the swap and the
// button state stay htmx's job), volume and mute call playbar.js, and seeking
// goes through the scrub bar, which already knows this tab's real position.
// That means a shortcut can never disagree with the buttons on screen.
//
// The controls live in an htmx-swapped partial, so nothing is bound to them
// up front — each press looks the elements up fresh.
(function () {
  var SEEK_S = 10;
  var VOL_STEP = 5;

  function typing(el) {
    if (!el) return false;
    return /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable;
  }

  // Space and Enter *activate* whatever is focused. Someone who just clicked
  // ⏭ still has it focused, so handling Space here too would skip and toggle
  // playback from one press — let the browser have those keys.
  function activatable(el) {
    if (!el) return false;
    return /^(BUTTON|A|SUMMARY)$/.test(el.tagName) || el.getAttribute("role") === "button";
  }

  function click(selector) {
    var el = document.querySelector(selector);
    if (el) el.click();
    return !!el;
  }

  function seekBy(delta) {
    var scrub = document.getElementById("pb-scrub");
    if (!scrub || scrub.disabled) return false;   // nothing playing, or no duration
    var next = Math.max(0, Math.min(+scrub.max || 0, +scrub.value + delta));
    scrub.value = next;
    scrubCommit(scrub);                            // playbar.js: seeks and tells the server
    return true;
  }

  function nudgeVolume(delta) {
    var range = document.getElementById("vol-range");
    if (!range) return false;
    var next = Math.max(0, Math.min(100, +range.value + delta));
    range.value = next;
    setVolume(next);                               // playbar.js: fetch + icon
    return true;
  }

  document.addEventListener("keydown", function (e) {
    // Let the browser's own shortcuts through, and don't hijack typing — the
    // search box is a text field a space belongs in.
    if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) return;

    var help = document.getElementById("help");
    var helpOpen = help && help.open;
    var key = e.key;

    // "?" works everywhere, including to close the sheet it opens.
    if (key === "?") {
      if (help) helpOpen ? help.close() : help.showModal();
      e.preventDefault();
      return;
    }
    // Esc closes the dialog natively; otherwise keep hands off while it's up.
    if (helpOpen) return;

    var handled = false;
    switch (key) {
      case " ":
        if (activatable(e.target)) return;
        // fall through
      case "k":
      case "K":
        // Whichever the current state offers: pause when something's playing,
        // otherwise the "play today" button.
        handled = click('[hx-post="/api/pause"], [hx-post="/api/play-today"]');
        break;
      case "n":
      case "N":
        handled = click('[hx-post="/api/skip"]');
        break;
      case "ArrowRight":
        handled = seekBy(SEEK_S);
        break;
      case "ArrowLeft":
        handled = seekBy(-SEEK_S);
        break;
      case "ArrowUp":
        handled = nudgeVolume(VOL_STEP);
        break;
      case "ArrowDown":
        handled = nudgeVolume(-VOL_STEP);
        break;
      case "m":
      case "M":
        toggleMute();
        handled = true;
        break;
    }
    // Only swallow the key when it actually did something — an unhandled arrow
    // should still scroll the page.
    if (handled) e.preventDefault();
  });
})();
