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

// Settings: each slider shows its value as it moves, a switch says On or
// Off, quiet hours' times are greyed out while they're off, and a group's
// Save says when there's something to save. All of it is display: the form
// posts the same fields with or without the script.
function sliderText(input) {
  const unit = input.dataset.unit || "";
  if (unit === "%") return `${input.value}%`;
  return unit ? `${input.value} ${unit}` : input.value;
}

function syncQuiet(box) {
  const on = box.querySelector("[data-quiet-toggle]").checked;
  box.querySelectorAll("[data-quiet-time]").forEach((t) => { t.disabled = !on; });
}

document.querySelectorAll("[data-quiet]").forEach(syncQuiet);

document.querySelectorAll("[data-settings-form]").forEach((form) => {
  const initial = new URLSearchParams(new FormData(form)).toString();
  const note = form.querySelector("[data-dirty-note]");
  form.addEventListener("input", (event) => {
    const target = event.target;
    if (target.type === "range") {
      const out = form.querySelector(`output[for="${CSS.escape(target.id)}"]`);
      if (out) out.textContent = sliderText(target);
    }
    if (target.type === "checkbox") {
      const text = target.closest(".switch")?.querySelector("[data-switch-text]");
      if (text) text.textContent = target.checked ? "On" : "Off";
    }
    const quiet = target.closest("[data-quiet]");
    if (quiet) syncQuiet(quiet);
    const dirty = new URLSearchParams(new FormData(form)).toString() !== initial;
    if (note) note.hidden = !dirty;
  });
});
