async function showMarkdownSource() {
  const reader = document.querySelector(".repo-document");
  const activeTab = reader.querySelector(".repo-views [aria-current]");
  const sourceTab = reader.querySelector('.repo-views a[href="?view=source"]');
  const download = reader.querySelector(".repo-download");
  const preview = reader.querySelector(".repo-preview");
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  pre.className = "repo-source";
  pre.append(code);

  activeTab?.removeAttribute("aria-current");
  sourceTab.setAttribute("aria-current", "page");
  reader.querySelector(".repo-document-body").classList.remove("repo-has-toc");
  reader.querySelector(".repo-toc")?.remove();
  preview.replaceChildren(pre);

  try {
    const response = await fetch(download.href, { credentials: "omit" });
    if (!response.ok) throw new Error("Cannot load Markdown source");
    code.textContent = await response.text();
  } catch {
    code.textContent = "暂时无法加载源码。";
  }
}

if (new URLSearchParams(location.search).get("view") === "source") {
  showMarkdownSource();
}
