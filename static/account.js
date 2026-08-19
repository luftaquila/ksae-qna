const userHost = document.getElementById("account-user");
const confirmation = document.getElementById("delete-confirmation");
const deleteButton = document.getElementById("delete-account");
const error = document.getElementById("account-error");
const statsHost = document.getElementById("usage-stats");
const paymentListHost = document.getElementById("payment-list");

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

const PAYMENT_STATUS = {
  pending: "결제 진행 중",
  paid: "결제 완료",
  failed: "결제 실패",
  cancelled: "결제 취소",
};

function formatLocal(value) {
  if (!value) return "";
  const date = new Date(value.includes("Z") || value.includes("+") ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function loadPayments() {
  if (!paymentListHost) return;
  try {
    const response = await fetch("/api/payments");
    if (!response.ok) throw new Error();
    const data = await response.json();
    const payments = data.payments || [];

    if (!payments.length) {
      paymentListHost.innerHTML = `<div class="payment-empty">결제 내역이 없습니다</div>`;
      return;
    }

    paymentListHost.innerHTML = payments.map((payment) => {
      const status = PAYMENT_STATUS[payment.status] || payment.status;
      const when = formatLocal(payment.approved_at || payment.cancelled_at || payment.created_at);
      const note = payment.status === "failed" && payment.fail_reason ? payment.fail_reason : "";
      return `<div class="payment-row payment-${escapeAttr(payment.status)}">
        <div class="payment-row-info">
          <span class="payment-goods">${escapeHtml(payment.goods_name)}</span>
          <span class="payment-meta">${escapeHtml(when)} · ${escapeHtml(status)}</span>
          ${note ? `<span class="payment-note">${escapeHtml(note)}</span>` : ""}
          <span class="payment-order">${escapeHtml(payment.order_id)}</span>
        </div>
        <span class="payment-amount">${Number(payment.amount).toLocaleString("ko-KR")}원</span>
      </div>`;
    }).join("");
  } catch {
    paymentListHost.innerHTML = `<div class="payment-empty">불러오지 못했습니다</div>`;
  }
}

loadAccount();
loadUsageStats();
loadPayments();
