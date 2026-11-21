const fileTree = document.querySelector(".repo-navigation");
if (fileTree) {
  const query = matchMedia("(max-width: 700px)");
  fileTree.open = sessionStorage.getItem("hr2026-file-tree") === null
    ? !query.matches
    : sessionStorage.getItem("hr2026-file-tree") === "open";
  if (query.matches) document.querySelector(".repo-toc details")?.removeAttribute("open");
  fileTree.addEventListener("toggle", () => {
    sessionStorage.setItem("hr2026-file-tree", fileTree.open ? "open" : "closed");
  });
}
