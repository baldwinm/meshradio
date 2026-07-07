// Help dialog: the header "?" opens it; the titlebar ✕ (a method="dialog"
// submit) and Esc close it natively. This only wires the open action and a
// backdrop click-to-close. The button and dialog live outside the htmx-swapped
// containers, so no rebinding is needed after partial updates.
(function () {
  const dlg = document.getElementById("help");
  const openBtn = document.getElementById("help-btn");
  if (!dlg || !openBtn) return;

  openBtn.addEventListener("click", () => dlg.showModal());

  // Clicking the backdrop (the dialog element itself, outside its content box)
  // closes it. Clicks on the inner content don't reach here as dlg's target.
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) dlg.close();
  });
})();
