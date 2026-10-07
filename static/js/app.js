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

  // ---------------------------------------------------------------------------
  // Phase 27: scroll-reveal animation system.
  //
  // Every element with class `reveal` fades in (and optionally slides) when it
  // enters the viewport. The observer fires once per element, so the animation
  // is a one-shot entrance — revisiting an element after scrolling past it does
  // not replay.
  //
  // Respects prefers-reduced-motion: if the user has reduced motion enabled,
  // elements are already visible via CSS (see tailwind.css) and the observer
  // simply adds the class immediately without waiting for intersection.
  // ---------------------------------------------------------------------------
  const prefersReducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  const reveals = document.querySelectorAll(".reveal");

  if (prefersReducedMotion) {
    // Immediately make everything visible — no animation.
    reveals.forEach((el) => el.classList.add("is-visible"));
  } else if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
    );
    reveals.forEach((el) => observer.observe(el));
  } else {
    // Fallback: no IntersectionObserver, show everything immediately.
    reveals.forEach((el) => el.classList.add("is-visible"));
  }
});
