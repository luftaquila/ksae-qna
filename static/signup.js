const consent = document.getElementById("privacy-consent");
const submit = document.getElementById("signup-submit");
const cancel = document.getElementById("signup-cancel");
const error = document.getElementById("signup-error");
const userHost = document.getElementById("signup-user");

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value || "";
  return div.innerHTML;
}

function escapeAttr(value) {
  return String(value || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function loadPendingSignup() {
  try {
    const response = await fetch("/api/auth/signup-pending");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "가입 정보를 불러오지 못했습니다.");
    const picture = data.picture
      ? `<img src="${escapeAttr(data.picture)}" alt="" referrerpolicy="no-referrer">`
      : "";
    userHost.innerHTML = `${picture}<div class="account-user-copy"><strong>${escapeHtml(data.name)}</strong><span>${escapeHtml(data.email)}</span></div>`;
  } catch (cause) {
    error.textContent = cause.message;
    submit.disabled = true;
  }
}

consent.addEventListener("change", () => {
  submit.disabled = !consent.checked;
  error.textContent = "";
});

submit.addEventListener("click", async () => {
  if (!consent.checked) return;
  submit.disabled = true;
  error.textContent = "";
  try {
    const response = await fetch("/api/auth/signup-consent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ privacy_consent: true }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "가입을 완료하지 못했습니다.");
    window.location.replace("/");
  } catch (cause) {
    error.textContent = cause.message;
    submit.disabled = !consent.checked;
  }
});

cancel.addEventListener("click", async () => {
  await fetch("/api/auth/signup-cancel", { method: "POST" });
  window.location.replace("/");
});

loadPendingSignup();
