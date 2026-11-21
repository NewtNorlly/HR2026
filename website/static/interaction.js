document.addEventListener("submit", (event) => {
  const button = event.submitter;
  if (!button || event.defaultPrevented) return;
  const original = button.textContent;
  window.setTimeout(() => {
    button.textContent = "正在保存…";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }, 0);
  window.addEventListener("pageshow", () => {
    button.textContent = original;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }, { once: true });
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const navLink = document.querySelector(".navigation a:is(:hover, :focus-visible)");
  navLink?.classList.add("label-dismissed");
  const disclosure = document.activeElement.closest("details.action-panel[open]");
  if (disclosure) {
    disclosure.open = false;
    disclosure.querySelector("summary").focus();
  }
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-open-dialog]");
  if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const dialog = document.getElementById(link.dataset.openDialog);
  if (!dialog || typeof dialog.showModal !== "function") return;
  event.preventDefault();
  if (!dialog.open) dialog.showModal();
});

const topbar = document.querySelector(".topbar");
const mouseNavigation = window.matchMedia("(hover: hover) and (pointer: fine)");
let navigationTimer;
let pointerNearTop = false;

function scheduleNavigationFade() {
  window.clearTimeout(navigationTimer);
  if (mouseNavigation.matches && !pointerNearTop) {
    navigationTimer = window.setTimeout(() => topbar.classList.add("is-idle"), 3200);
  }
}

if (topbar) {
  scheduleNavigationFade();

  for (const navLink of topbar.querySelectorAll(".navigation a")) {
    const resetLabel = () => {
      if (!navLink.matches(":hover, :focus-visible")) navLink.classList.remove("label-dismissed");
    };
    navLink.addEventListener("pointerleave", resetLabel);
    navLink.addEventListener("blur", resetLabel);
  }

  document.addEventListener("pointermove", (event) => {
    if (!mouseNavigation.matches || event.pointerType !== "mouse") return;
    const nearTop = event.clientY <= 64 || Boolean(event.target.closest(".nav-label"));
    if (nearTop === pointerNearTop) return;
    pointerNearTop = nearTop;
    if (nearTop) topbar.classList.remove("is-idle");
    scheduleNavigationFade();
  }, { passive: true });

  document.documentElement.addEventListener("pointerleave", (event) => {
    if (event.pointerType !== "mouse") return;
    pointerNearTop = false;
    scheduleNavigationFade();
  });

  mouseNavigation.addEventListener("change", () => {
    topbar.classList.remove("is-idle");
    pointerNearTop = false;
    scheduleNavigationFade();
  });
}
