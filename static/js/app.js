// FLASHWEAR frontend bootstrap (Phase 1)
// Keep this file minimal. HTMX/Alpine handle progressive enhancements.

document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("submit", (event) => {
    const message = event.target.dataset.confirm;
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-submit-on-change]")) {
      event.target.form?.requestSubmit();
    }
  });
});
