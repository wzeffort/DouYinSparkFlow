(() => {
  const opener = document.querySelector("[data-mobile-nav-open]");
  const closer = document.querySelector("[data-mobile-nav-close]");
  const scrim = document.querySelector("[data-mobile-nav-scrim]");
  const drawer = document.querySelector("#app-navigation");
  if (!opener || !closer || !scrim || !drawer) return;

  const close = (returnFocus = true) => {
    document.body.classList.remove("mobile-nav-open");
    opener.setAttribute("aria-expanded", "false");
    if (returnFocus) opener.focus();
  };

  const open = () => {
    document.body.classList.add("mobile-nav-open");
    opener.setAttribute("aria-expanded", "true");
    closer.focus();
  };

  opener.addEventListener("click", open);
  closer.addEventListener("click", () => close());
  scrim.addEventListener("click", () => close());
  drawer.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => close(false));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("mobile-nav-open")) {
      event.preventDefault();
      close();
    }
  });
})();
