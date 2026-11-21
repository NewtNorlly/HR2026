const accountDialog = document.getElementById("account-dialog");
const loginPreview = accountDialog.querySelector("[data-login-preview]");
const loginStatus = accountDialog.querySelector("[data-login-status]");

if (typeof accountDialog.showModal === "function") {
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-open-dialog='account-dialog']");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (!accountDialog.open) accountDialog.showModal();
  });
}

loginPreview.addEventListener("submit", (event) => {
  event.preventDefault();
  loginPreview.reset();
  loginStatus.textContent = "这是登录界面演示。";
  loginStatus.hidden = false;
});

accountDialog.addEventListener("close", () => {
  loginPreview.reset();
  loginStatus.hidden = true;
});
