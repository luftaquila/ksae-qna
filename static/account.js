const userHost = document.getElementById("account-user");
const confirmation = document.getElementById("delete-confirmation");
const deleteButton = document.getElementById("delete-account");
const error = document.getElementById("account-error");
const statsHost = document.getElementById("usage-stats");

const statTargets = {
  conversation_count: document.getElementById("stat-conversations"),
  question_count: document.getElementById("stat-questions"),
  credits_used: document.getElementById("stat-credits-used"),
  credits_refunded: document.getElementById("stat-credits-refunded"),
  input_tokens: document.getElementById("stat-input-tokens"),
  output_tokens: document.getElementById("stat-output-tokens"),
  thinking_tokens: document.getElementById("stat-thinking-tokens"),
};

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value || "";
  return div.innerHTML;
}

function escapeAttr(value) {
  return String(value || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function loadAccount() {
  try {
    const response = await fetch("/api/me");
    const data = await response.json();
    if (!data.user) {
      window.location.replace("/");
      return;
    }
    const user = data.user;
    const picture = user.picture
      ? `<img src="${escapeAttr(user.picture)}" alt="" referrerpolicy="no-referrer">`
      : "";
    userHost.innerHTML = `${picture}<div class="account-user-copy"><strong>${escapeHtml(user.name)}</strong><span>${escapeHtml(user.email)}</span></div>`;
  } catch {
    error.textContent = "계정 정보를 불러오지 못했습니다.";
  }
}

async function loadUsageStats() {
  try {
    const response = await fetch("/api/account/stats");
    if (!response.ok) throw new Error();
    const data = await response.json();
    Object.entries(statTargets).forEach(([key, target]) => {
      target.textContent = Number(data.stats?.[key] || 0).toLocaleString("ko-KR");
    });
    statsHost.setAttribute("aria-busy", "false");
  } catch {
    statsHost.setAttribute("aria-busy", "false");
    statsHost.querySelectorAll("strong").forEach((target) => {
      target.textContent = "확인 불가";
    });
  }
}

confirmation.addEventListener("input", () => {
  deleteButton.disabled = confirmation.value !== "회원탈퇴";
  error.textContent = "";
});

deleteButton.addEventListener("click", async () => {
  if (confirmation.value !== "회원탈퇴") return;
  if (!window.confirm("계정과 모든 대화 기록을 영구 삭제하시겠습니까?")) return;
  deleteButton.disabled = true;
  error.textContent = "";
  try {
    const response = await fetch("/api/account", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation: confirmation.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "회원 탈퇴에 실패했습니다.");
    window.location.replace("/");
  } catch (cause) {
    error.textContent = cause.message;
    deleteButton.disabled = confirmation.value !== "회원탈퇴";
  }
});

loadAccount();
loadUsageStats();
