// Admin page: the typed confirmation on a removal. The server checks the
// typed date either way; this only keeps the button disabled until it
// matches, so the destructive press can't be what a stray Enter does. Without
// the script the button works and the server's check is the whole guard.
function syncConfirm(box) {
  const button = box.form && box.form.querySelector("[data-confirm-button]");
  if (button) button.disabled = box.value.trim() !== box.dataset.confirmValue;
}

document.querySelectorAll("[data-confirm-value]").forEach(syncConfirm);
document.addEventListener("input", (event) => {
  const box = event.target.closest("[data-confirm-value]");
  if (box) syncConfirm(box);
});
