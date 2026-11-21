document.querySelectorAll(".wiki-form").forEach((form) => {
  const input = form.elements.url;
  const check = form.querySelector(".wiki-check");
  const update = () => {
    let destination;
    try {
      const parsed = new URL(input.value.trim());
      if (["https:", "http:"].includes(parsed.protocol) && !parsed.username && !parsed.password) {
        destination = parsed.href;
      }
    } catch {
      // A partial address is normal while the user is typing.
    }
    check.hidden = !destination;
    if (destination) check.href = destination;
    else check.removeAttribute("href");
  };
  input.addEventListener("input", update);
  update();
});
