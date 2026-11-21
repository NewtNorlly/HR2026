const legacyLinks = JSON.parse(document.currentScript.dataset.legacyLinks);
const query = new URLSearchParams(window.location.search);
if (query.has("issue")) query.delete("scope");
query.sort();
const legacyTarget = legacyLinks[query.toString()];
if (legacyTarget) window.location.replace(legacyTarget + window.location.hash);
