const form = document.getElementById("login-form");
const emailInput = document.getElementById("login-email");
const passwordInput = document.getElementById("login-password");
const errorBox = document.getElementById("login-error");
const submitButton = document.getElementById("login-submit");
const toggleButton = document.getElementById("toggle-password");

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.add("show");
}

function clearError() {
  errorBox.textContent = "";
  errorBox.classList.remove("show");
}

toggleButton.addEventListener("click", () => {
  const showing = passwordInput.type === "text";
  passwordInput.type = showing ? "password" : "text";
  toggleButton.textContent = showing ? "Show" : "Hide";
  toggleButton.setAttribute("aria-label", showing ? "Show password" : "Hide password");
  passwordInput.focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();

  const email = emailInput.value.trim();
  const password = passwordInput.value;
  if (!email || !password) {
    showError("Please enter both your email and password.");
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = "Signing in…";

  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      passwordInput.value = "";
      showError(data.detail || "Sign-in failed. Please try again.");
      return;
    }

    const next = new URLSearchParams(window.location.search).get("next");
    const safeNext = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
    window.location.assign(safeNext);
  } catch (error) {
    showError("Could not reach the server. Check your connection and try again.");
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Sign in";
  }
});
