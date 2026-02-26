const chat = document.getElementById("chat");
const form = document.getElementById("form");
const queryInput = document.getElementById("query");
const sendBtn = document.getElementById("send");
const authArea = document.getElementById("auth-area");
const loginOverlay = document.getElementById("login-overlay");
const sessionListEl = document.getElementById("session-list");
const newChatBtn = document.getElementById("new-chat-btn");
const themeToggle = document.getElementById("theme-toggle");

let currentUser = null;
let currentSessionId = null;
let sessions = [];

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
  if (currentUser) {
    loadSessions();
  }
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
      <div class="token-wrapper">
        <span class="credit-badge${lowClass}" id="credit-badge">${currentUser.credits} 토큰</span>
      </div>
      <button class="logout-btn" id="logout-btn">로그아웃</button>
    `;

    document.getElementById("logout-btn").addEventListener("click", handleLogout);
    document.getElementById("credit-badge").addEventListener("click", toggleTokenPopover);
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
  badge.textContent = `${credits} 토큰`;
  badge.classList.toggle("low", credits <= 5);
}

async function handleLogout() {
  await fetch("/api/auth/logout", { method: "POST" });
  currentUser = null;
  currentSessionId = null;
  sessions = [];
  renderSessionList();
  renderAuthUI();
  chat.innerHTML = "";
}

// ---------------------------------------------------------------------------
// Token popover
// ---------------------------------------------------------------------------
let tokenPopover = null;

function toggleTokenPopover() {
  if (tokenPopover) {
    closeTokenPopover();
    return;
  }

  const wrapper = document.querySelector(".token-wrapper");
  tokenPopover = document.createElement("div");
  tokenPopover.className = "token-popover";
  tokenPopover.innerHTML = `
    <div class="token-popover-header">
      <span>토큰 내역</span>
    </div>
    <div class="token-history"><div class="token-history-loading">불러오는 중...</div></div>
    <div class="token-popover-footer">
      <div class="token-purchase-row">
        <input type="number" class="token-purchase-input" min="1" max="1000" value="30" placeholder="수량">
        <button class="token-purchase-btn">구매</button>
      </div>
    </div>
  `;
  wrapper.appendChild(tokenPopover);

  loadTransactions();

  tokenPopover.querySelector(".token-purchase-btn").addEventListener("click", handleTokenPurchase);
  tokenPopover.querySelector(".token-purchase-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleTokenPurchase();
  });

  setTimeout(() => document.addEventListener("click", onClickOutsidePopover), 0);
}

function closeTokenPopover() {
  if (tokenPopover) {
    tokenPopover.remove();
    tokenPopover = null;
  }
  document.removeEventListener("click", onClickOutsidePopover);
}

function onClickOutsidePopover(e) {
  if (tokenPopover && !tokenPopover.contains(e.target) && e.target.id !== "credit-badge") {
    closeTokenPopover();
  }
}

async function loadTransactions() {
  const historyEl = tokenPopover?.querySelector(".token-history");
  if (!historyEl) return;

  try {
    const res = await fetch("/api/transactions");
    const data = await res.json();
    const txns = data.transactions || [];

    if (!txns.length) {
      historyEl.innerHTML = `<div class="token-history-empty">내역이 없습니다</div>`;
      return;
    }

    historyEl.innerHTML = txns.map((t) => {
      const isUsage = t.amount < 0;
      const sign = isUsage ? "" : "+";
      const cls = isUsage ? "usage" : "purchase";
      const date = t.created_at.slice(0, 16).replace("T", " ");
      return `<div class="token-tx ${cls}">
        <div class="token-tx-info">
          <span class="token-tx-memo">${escapeHtml(t.memo || t.type)}</span>
          <span class="token-tx-date">${date}</span>
        </div>
        <span class="token-tx-amount">${sign}${t.amount}</span>
      </div>`;
    }).join("");
  } catch {
    historyEl.innerHTML = `<div class="token-history-empty">불러오기 실패</div>`;
  }
}

async function handleTokenPurchase() {
  const input = tokenPopover?.querySelector(".token-purchase-input");
  if (!input) return;
  const amount = parseInt(input.value, 10);
  if (!amount || amount < 1 || amount > 1000) return;

  const btn = tokenPopover.querySelector(".token-purchase-btn");
  btn.disabled = true;
  btn.textContent = "...";

  try {
    const res = await fetch("/api/credits/topup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount }),
    });
    const data = await res.json();
    if (res.ok) {
      updateCreditDisplay(data.credits);
      loadTransactions();
      input.value = "30";
    } else {
      alert(data.error || "구매에 실패했습니다");
    }
  } catch {
    alert("구매에 실패했습니다");
  }

  btn.disabled = false;
  btn.textContent = "구매";
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------
async function loadSessions() {
  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    sessions = data.sessions || [];
  } catch {
    sessions = [];
  }
  renderSessionList();
}

function renderSessionList() {
  sessionListEl.innerHTML = "";
  for (const s of sessions) {
    const item = document.createElement("div");
    item.className = "session-item" + (s.id === currentSessionId ? " active" : "");
    item.innerHTML = `
      <span class="session-item-title">${escapeHtml(s.title)}</span>
      <button class="session-item-delete" title="삭제">&#10005;</button>
    `;
    item.addEventListener("click", () => switchSession(s.id));
    item.querySelector(".session-item-delete").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSession(s.id);
    });
    sessionListEl.appendChild(item);
  }
}

async function switchSession(id) {
  currentSessionId = id;
  renderSessionList();
  chat.innerHTML = "";

  try {
    const res = await fetch(`/api/sessions/${id}/messages`);
    const data = await res.json();
    const messages = data.messages || [];

    for (const msg of messages) {
      if (msg.role === "user") {
        appendMessage("user", msg.content);
      } else if (msg.role === "assistant") {
        const msgEl = appendAssistantShell();
        const sourcesContainer = msgEl.querySelector(".sources");
        const answerEl = msgEl.querySelector(".answer");

        if (msg.sources) {
          try {
            const sources = JSON.parse(msg.sources);
            renderSources(sourcesContainer, sources);
          } catch {}
        }
        answerEl.innerHTML = marked.parse(msg.content || "");
      }
    }
    scrollToBottom();
  } catch {
    chat.innerHTML = "";
  }

}

function startNewChat() {
  currentSessionId = null;
  renderSessionList();
  chat.innerHTML = "";
  queryInput.focus();
}

async function deleteSession(id) {
  if (!confirm("이 대화를 삭제하시겠습니까?")) return;
  try {
    await fetch(`/api/sessions/${id}`, { method: "DELETE" });
    sessions = sessions.filter((s) => s.id !== id);
    if (currentSessionId === id) {
      currentSessionId = null;
      chat.innerHTML = "";
    }
    renderSessionList();
  } catch {}
}

newChatBtn.addEventListener("click", startNewChat);

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
function initTheme() {
  const saved = localStorage.getItem("theme");
  const theme = saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", theme);
  themeToggle.textContent = theme === "dark" ? "\u2600\uFE0F" : "\uD83C\uDF19";
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("theme", theme);
  themeToggle.textContent = theme === "dark" ? "\u2600\uFE0F" : "\uD83C\uDF19";
}

themeToggle.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  setTheme(current === "dark" ? "light" : "dark");
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  if (!localStorage.getItem("theme")) {
    const theme = e.matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    themeToggle.textContent = theme === "dark" ? "\u2600\uFE0F" : "\uD83C\uDF19";
  }
});

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
    const collections = [...form.querySelectorAll('input[name="collections"]:checked')].map((el) => el.value);
    const body = { query, collections };
    if (currentSessionId) body.session_id = currentSessionId;

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (res.status === 401) {
      answerEl.textContent = "로그인이 필요합니다.";
      currentUser = null;
      renderAuthUI();
      setLoading(false);
      return;
    }

    if (res.status === 402) {
      answerEl.textContent = "토큰이 부족합니다. 구매 후 다시 시도해주세요.";
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

          if (eventType === "session") {
            try {
              const payload = JSON.parse(data);
              if (payload.session_id) {
                currentSessionId = payload.session_id;
                loadSessions();
              }
            } catch {}
          } else {
            handleEvent(eventType, data, sourcesContainer, answerEl, { fullText });
            if (eventType === "token") {
              try { fullText += JSON.parse(data); } catch {}
            }
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
initTheme();
checkAuth();
