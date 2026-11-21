const officePreview = document.querySelector("[data-office-preview]");

if (officePreview) {
  let pollDelay = 2000;
  let pollTimer;
  let checking = false;

  async function checkOfficePreview() {
    if (document.hidden || checking) return;
    if (!officePreview.isConnected) {
      stopPolling();
      return;
    }

    checking = true;
    try {
      const response = await fetch(officePreview.dataset.officePreview, {cache: "no-store"});
      if (response.redirected) {
        const target = new URL(response.url);
        if (target.origin === location.origin && /^\/files\/\d+\/preview-status$/.test(target.pathname)) {
          // Apply the same latest-version redirect to the open preview page.
          stopPolling();
          location.reload();
          return;
        }
      }
      if (response.redirected || response.status === 403 || response.status === 404) {
        const message = officePreview.querySelector(".repo-empty");
        message.textContent = "当前无法访问此预览，请刷新页面检查权限。";
        officePreview.replaceChildren(message);
        stopPolling();
        return;
      }
      if (response.ok) {
        const {status} = await response.json();
        const template = {
          ready: "[data-office-ready]",
          failed: "[data-office-error]",
        }[status];
        if (template) {
          officePreview.replaceChildren(officePreview.querySelector(template).content.cloneNode(true));
          stopPolling();
          return;
        }
      }
    } catch {
      // Keep waiting when the connection drops temporarily.
    } finally {
      checking = false;
    }

    pollDelay = Math.min(pollDelay + 1000, 8000);
    if (!document.hidden) pollTimer = setTimeout(checkOfficePreview, pollDelay);
  }

  function handleVisibilityChange() {
    clearTimeout(pollTimer);
    if (!document.hidden) checkOfficePreview();
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    document.removeEventListener("visibilitychange", handleVisibilityChange);
  }

  document.addEventListener("visibilitychange", handleVisibilityChange);
  if (!document.hidden) pollTimer = setTimeout(checkOfficePreview, pollDelay);
}
