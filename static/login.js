(() => {
  "use strict";
  const form = document.querySelector("#login-form");
  const error = document.querySelector("#error");
  const submit = document.querySelector("#submit");
  const label = submit.querySelector(".label");
  const toggle = document.querySelector("#toggle-pw");

  function showError(text) {
    error.textContent = text;
    error.hidden = !text;
  }

  toggle.addEventListener("click", () => {
    const show = form.password.type === "password";
    form.password.type = show ? "text" : "password";
    toggle.setAttribute("aria-pressed", String(show));
    toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
    toggle.querySelector(".eye-on").hidden = !show;
    toggle.querySelector(".eye-off").hidden = show;
    form.password.focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError("");
    if (!form.username.value.trim() || !form.password.value) {
      showError("Enter your username and password.");
      return;
    }
    submit.disabled = true;
    label.textContent = "Signing in";
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: form.username.value.trim(),
          password: form.password.value,
        }),
      });
      if (res.ok) {
        location.href = "/";
        return;
      }
      const data = await res.json().catch(() => ({}));
      showError(data.error || "Something went wrong. Try again.");
      form.password.value = "";
      form.password.focus();
    } catch {
      showError("Can't reach the server. Check that server.py is running, then try again.");
    }
    submit.disabled = false;
    label.textContent = "Sign In";
  });
})();