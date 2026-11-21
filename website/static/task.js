function revealTaskAnchor() {
  if (!/^#task-\d+$/.test(location.hash)) return;
  const target = document.getElementById(location.hash.slice(1));
  if (!target) return;

  for (let section = target; section; section = section.parentElement.closest("details")) {
    section.open = true;
  }
  target.scrollIntoView({ block: "nearest" });
}

revealTaskAnchor();
window.addEventListener("hashchange", revealTaskAnchor);
