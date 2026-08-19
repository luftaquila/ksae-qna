// 결제 판정은 이미 서버(returnUrl 핸들러)에서 끝났다. 이 화면은 그 결과를
// 사람이 읽을 수 있게 옮겨 적기만 한다.

const params = new URLSearchParams(window.location.search);
const result = params.get("result");
const orderId = params.get("order");

const titleEl = document.getElementById("result-title");
const messageEl = document.getElementById("result-message");
const detailEl = document.getElementById("result-detail");
const balanceEl = document.getElementById("result-balance");

const OUTCOMES = {
  paid: {
    title: "결제가 완료되었습니다",
    message: "이용권이 계정에 반영되었습니다.",
  },
  failed: {
    title: "결제가 완료되지 않았습니다",
    message: "결제가 취소되었거나 승인이 거절되었습니다. 요금은 청구되지 않습니다.",
  },
  invalid: {
    title: "결제 정보를 확인할 수 없습니다",
    message: "주문을 찾지 못했습니다. 결제가 진행되었는데 이 화면이 보인다면 문의해 주세요.",
  },
};

function formatLocal(value) {
  if (!value) return "—";
  const date = new Date(value.includes("Z") || value.includes("+") ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const outcome = OUTCOMES[result] || OUTCOMES.invalid;
titleEl.textContent = outcome.title;
messageEl.textContent = outcome.message;

async function loadOrder() {
  if (!orderId) return;
  try {
    const res = await fetch("/api/payments");
    if (!res.ok) return; // 로그인이 풀린 경우. 위의 안내만으로 충분하다.
    const data = await res.json();
    const order = (data.payments || []).find((p) => p.order_id === orderId);
    if (!order) return;

    document.getElementById("detail-goods").textContent = order.goods_name;
    document.getElementById("detail-quantity").textContent = `${order.quantity}장`;
    document.getElementById("detail-amount").textContent =
      `${Number(order.amount).toLocaleString("ko-KR")}원`;
    document.getElementById("detail-order").textContent = order.order_id;
    detailEl.hidden = false;

    if (order.status === "failed" && order.fail_reason) {
      messageEl.textContent = `${order.fail_reason} 요금은 청구되지 않습니다.`;
    }
    if (order.status === "paid" && order.approved_at) {
      messageEl.textContent = `${formatLocal(order.approved_at)}에 결제가 완료되었습니다.`;
    }
  } catch {
    // 상세를 못 불러와도 결과 자체는 이미 표시했다.
  }
}

async function loadBalance() {
  if (result !== "paid") return;
  try {
    const res = await fetch("/api/me");
    const data = await res.json();
    if (!data.user) return;
    balanceEl.textContent = `현재 이용권 ${Number(data.user.credits).toLocaleString("ko-KR")}장`;
  } catch {
    // 잔액 표시는 부가 정보다.
  }
}

loadOrder();
loadBalance();
