const chat = document.getElementById("chat");
const form = document.getElementById("form");
const queryInput = document.getElementById("query");
const sendBtn = document.getElementById("send");
const authArea = document.getElementById("auth-area");
const loginOverlay = document.getElementById("login-overlay");

let currentUser = null;

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
async function checkAuth() {
  try {
    const res = await fetch("/api/me");
    const data = await res.json();
    currentUser = data.user;
  } catch {
    currentUser = null;
  }
  renderAuthUI();
}

function renderAuthUI() {
  if (currentUser) {
    loginOverlay.classList.add("hidden");
    queryInput.disabled = false;
    sendBtn.disabled = false;

    const img = currentUser.picture
      ? `<img src="${escapeAttr(currentUser.picture)}" class="profile-img" alt="" referrerpolicy="no-referrer">`
      : "";
    const lowClass = currentUser.credits <= 5 ? " low" : "";

    authArea.innerHTML = `
      <div class="profile-info">
        ${img}
        <span class="profile-name">${escapeHtml(currentUser.name)}</span>
      </div>
      <span class="credit-badge${lowClass}" id="credit-badge">${currentUser.credits} 크레딧</span>
      <button class="topup-btn" id="topup-btn">충전</button>
      <button class="logout-btn" id="logout-btn">로그아웃</button>
    `;

    document.getElementById("logout-btn").addEventListener("click", handleLogout);
    document.getElementById("topup-btn").addEventListener("click", showTopupModal);
  } else {
    loginOverlay.classList.remove("hidden");
    queryInput.disabled = true;
    sendBtn.disabled = true;

    authArea.innerHTML = `<a href="/api/auth/login" class="login-btn google-login">Google 로그인</a>`;
  }
}

function updateCreditDisplay(credits) {
  if (currentUser) currentUser.credits = credits;
  const badge = document.getElementById("credit-badge");
  if (!badge) return;
  badge.textContent = `${credits} 크레딧`;
  badge.classList.toggle("low", credits <= 5);
}

async function handleLogout() {
  await fetch("/api/auth/logout", { method: "POST" });
  currentUser = null;
  renderAuthUI();
}

// ---------------------------------------------------------------------------
// Topup modal
// ---------------------------------------------------------------------------
function showTopupModal() {
  const modal = document.createElement("div");
  modal.className = "topup-modal";
  modal.innerHTML = `
    <div class="topup-box">
      <h3>크레딧 충전</h3>
      <input type="number" id="topup-amount" min="1" max="1000" value="30" placeholder="충전량 (1~1000)">
      <div class="topup-actions">
        <button class="topup-cancel" id="topup-cancel">취소</button>
        <button class="topup-confirm" id="topup-confirm">충전</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  modal.querySelector("#topup-cancel").addEventListener("click", () => modal.remove());
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });

  modal.querySelector("#topup-confirm").addEventListener("click", async () => {
    const amount = parseInt(modal.querySelector("#topup-amount").value, 10);
    if (!amount || amount < 1 || amount > 1000) return;

    try {
      const res = await fetch("/api/credits/topup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount }),
      });
      const data = await res.json();
      if (res.ok) {
        updateCreditDisplay(data.credits);
        modal.remove();
      } else {
        alert(data.error || "충전에 실패했습니다");
      }
    } catch {
      alert("충전에 실패했습니다");
    }
  });

  modal.querySelector("#topup-amount").focus();
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  appendMessage("user", query);
  queryInput.value = "";
  setLoading(true);

  const msgEl = appendAssistantShell();
  const sourcesContainer = msgEl.querySelector(".sources");
  const answerEl = msgEl.querySelector(".answer");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });

    if (res.status === 401) {
      answerEl.textContent = "로그인이 필요합니다.";
      currentUser = null;
      renderAuthUI();
      setLoading(false);
      return;
    }

    if (res.status === 402) {
      answerEl.textContent = "크레딧이 부족합니다. 충전 후 다시 시도해주세요.";
      updateCreditDisplay(0);
      setLoading(false);
      return;
    }

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    // Update credits from header
    const remaining = res.headers.get("X-Credits-Remaining");
    if (remaining !== null) updateCreditDisplay(parseInt(remaining, 10));

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let fullText = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      let eventType = null;
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7);
        } else if (line.startsWith("data: ")) {
          const data = line.slice(6);
          handleEvent(eventType, data, sourcesContainer, answerEl, { fullText });
          if (eventType === "token") {
            try { fullText += JSON.parse(data); } catch {}
          }
          eventType = null;
        }
      }
    }

    // Final render
    answerEl.innerHTML = marked.parse(fullText);
  } catch (err) {
    answerEl.textContent = `오류가 발생했습니다: ${err.message}`;
  }

  setLoading(false);
  scrollToBottom();
});

function handleEvent(type, data, sourcesContainer, answerEl, state) {
  if (type === "sources") {
    try {
      const sources = JSON.parse(data);
      renderSources(sourcesContainer, sources);
    } catch {}
  } else if (type === "token") {
    try {
      const token = JSON.parse(data);
      state.fullText = (state.fullText || "") + token;
      // Incremental markdown render
      answerEl.innerHTML = marked.parse(state.fullText);
      scrollToBottom();
    } catch {}
  }
}

function appendMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  chat.appendChild(el);
  scrollToBottom();
}

function appendAssistantShell() {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `
    <div class="sources"></div>
    <div class="answer"><span class="loading-dots">답변 생성 중</span></div>
  `;
  chat.appendChild(el);
  scrollToBottom();
  return el;
}

function renderSources(container, sources) {
  if (!sources.length) return;

  const toggle = document.createElement("button");
  toggle.className = "sources-toggle";
  toggle.innerHTML = `<span class="arrow">&#9654;</span> 참고 문서 ${sources.length}건`;

  const list = document.createElement("div");
  list.className = "sources-list";

  sources.forEach((s) => {
    const item = document.createElement("div");
    item.className = "source-item";
    let html = `<div class="source-header">${escapeHtml(s.source)}</div>`;
    html += `<span class="source-score">유사도: ${(s.score * 100).toFixed(1)}%</span>`;
    if (s.url) {
      html += ` <a href="${escapeAttr(s.url)}" target="_blank" rel="noopener">원문 보기</a>`;
    }
    html += `<div class="source-content">${escapeHtml(s.content)}</div>`;
    item.innerHTML = html;
    list.appendChild(item);
  });

  toggle.addEventListener("click", () => {
    toggle.classList.toggle("open");
    list.classList.toggle("open");
  });

  container.appendChild(toggle);
  container.appendChild(list);
}

function setLoading(loading) {
  sendBtn.disabled = loading;
  queryInput.disabled = loading;
  if (!loading) queryInput.focus();
}

function scrollToBottom() {
  chat.scrollTop = chat.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function escapeAttr(str) {
  return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
checkAuth();
