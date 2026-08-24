(() => {
  "use strict";
  const card = document.querySelector("[data-telegram-status]");
  const button = card?.querySelector("[data-status-refresh]");
  if (!card || !button) return;
  const endpoint = card.dataset.statusUrl;
  let checking = false;

  const checkStatus = async () => {
    if (checking || !endpoint) return;
    checking = true;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Checking connection...";
    try {
      const response = await fetch(endpoint, {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Status request failed");
      const status = await response.json();
      if (status.connected) {
        window.JobMatcher?.toast("Telegram connected. Opening your dashboard...");
        window.setTimeout(() => window.location.assign(status.redirect || "/dashboard"), 450);
        return;
      }
      window.JobMatcher?.toast("Still waiting for Telegram confirmation.");
    } catch (_error) {
      window.JobMatcher?.toast("Connection status could not be checked. Please try again.");
    } finally {
      checking = false;
      button.disabled = false;
      button.textContent = original;
    }
  };
  button.addEventListener("click", checkStatus);
  window.addEventListener("focus", () => window.setTimeout(checkStatus, 500));
})();
