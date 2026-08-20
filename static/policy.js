// 판매자 정보와 판매 조건은 관리자 설정에서 온다. 값이 비어 있으면 하드코딩된
// 자리표시자 대신 "미등록"이라고 솔직하게 적는다.

const FIELDS = {
  biz_name: "biz-name",
  biz_owner: "biz-owner",
  biz_reg_no: "biz-reg-no",
  biz_mail_order_no: "biz-mail-order-no",
  biz_address: "biz-address",
  biz_tel: "biz-tel",
  biz_email: "biz-email",
};

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

async function loadPolicy() {
  let data;
  try {
    const res = await fetch("/api/policy");
    data = await res.json();
  } catch {
    Object.values(FIELDS).forEach((id) => setText(id, "불러오지 못했습니다"));
    return;
  }

  const business = data.business || {};
  Object.entries(FIELDS).forEach(([key, id]) => {
    setText(id, business[key] || "미등록");
  });

  const email = business.biz_email;
  const contact = document.getElementById("refund-contact");
  if (contact) {
    if (email) {
      contact.innerHTML = "";
      const link = document.createElement("a");
      link.href = `mailto:${email}`;
      link.textContent = email;
      contact.appendChild(link);
    } else {
      contact.textContent = "미등록";
    }
  }

  // 이용약관과 개인정보처리방침 안에도 상호가 있어야 한다 (심사 요구사항).
  const label = business.biz_name
    ? `${business.biz_name}${business.biz_owner ? ` (대표 ${business.biz_owner})` : ""}`
    : "미등록";
  setText("terms-biz", label);
  setText("privacy-biz", label);
  setText("privacy-contact", business.biz_email || "미등록");

  const payment = data.payment || {};
  if (payment.unit_price) {
    setText("product-price", `${Number(payment.unit_price).toLocaleString("ko-KR")}원 / 1장`);
    setText("product-range", `${payment.min_quantity}장 ~ ${payment.max_quantity}장`);
    setText(
      "product-min-amount",
      `${Number(payment.min_amount).toLocaleString("ko-KR")}원 (카드사 최소 승인금액)`,
    );
  }
}

loadPolicy();
