const accountEntry = document.querySelector("[data-open-dialog='account-dialog']");
const accountDialog = document.getElementById("account-dialog");

if (accountEntry && typeof accountDialog?.showModal === "function") {
  accountEntry.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (!accountDialog.open) accountDialog.showModal();
  });
}
