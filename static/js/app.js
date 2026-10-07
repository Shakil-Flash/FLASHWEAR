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
  // Phase 27: Reliable, fail-safe scroll-reveal system
  //
  // Every element with class `reveal` starts 100% visible by default in CSS.
  // When this script initializes, `js-reveal-active` is added to <html>,
  // arming the animation.
  //
  // Elements already in or near the viewport trigger immediately without waiting.
  // A 1.5s fail-safe guarantees all content is revealed even on edge-case browsers.
  // Respects prefers-reduced-motion completely.
  // ---------------------------------------------------------------------------
  const prefersReducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;

  const reveals = document.querySelectorAll(".reveal");

  if (!prefersReducedMotion && "IntersectionObserver" in window && reveals.length > 0) {
    // Arm reveal animations now that JS is confirmed running
    document.documentElement.classList.add("js-reveal-active");

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.05, rootMargin: "0px 0px 80px 0px" }
    );

    reveals.forEach((el) => {
      // Check if already in viewport
      const rect = el.getBoundingClientRect();
      if (rect.top < window.innerHeight) {
        el.classList.add("is-visible");
      } else {
        observer.observe(el);
      }
    });

    // 1.5s fail-safe guarantee
    setTimeout(() => {
      reveals.forEach((el) => el.classList.add("is-visible"));
    }, 1500);
  } else {
    // Immediate fallback: ensure all elements are visible
    reveals.forEach((el) => el.classList.add("is-visible"));
  }
});
