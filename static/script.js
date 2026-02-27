const chat = document.getElementById("chat");
const form = document.getElementById("form");
const queryInput = document.getElementById("query");
const sendBtn = document.getElementById("send");
const authArea = document.getElementById("auth-area");
const sessionListEl = document.getElementById("session-list");
const newChatBtn = document.getElementById("new-chat-btn");
const themeToggle = document.getElementById("theme-toggle");
const sidebar = document.getElementById("sidebar");
const sidebarToggle = document.getElementById("sidebar-toggle");
const sidebarOverlay = document.getElementById("sidebar-overlay");

let currentUser = null;
let currentSessionId = null;
let availableModels = [];
let lowCreditThreshold = 5;

// ---------------------------------------------------------------------------
// Mobile sidebar
// ---------------------------------------------------------------------------
function openSidebar() {
  sidebar.classList.add("open");
  sidebarOverlay.classList.add("open");
}

function closeSidebar() {
  sidebar.classList.remove("open");
  sidebarOverlay.classList.remove("open");
}

sidebarToggle.addEventListener("click", () => {
  sidebar.classList.contains("open") ? closeSidebar() : openSidebar();
});

sidebarOverlay.addEventListener("click", closeSidebar);
let sessions = [];

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
async function checkAuth() {
  try {
    const res = await fetch("/api/me");
    const data = await res.json();
    currentUser = data.user;
    if (data.low_credit_threshold !== undefined) lowCreditThreshold = data.low_credit_threshold;
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
    queryInput.disabled = false;
    sendBtn.disabled = false;

    const imgTag = currentUser.picture
      ? `<img src="${escapeAttr(currentUser.picture)}" class="profile-img" alt="" referrerpolicy="no-referrer">`
      : "";
    const img = currentUser.is_admin
      ? `<a href="/admin" class="profile-admin-link" title="관리자 페이지">${imgTag}</a>`
      : imgTag;
    const lowClass = currentUser.credits <= lowCreditThreshold ? " low" : "";

    authArea.innerHTML = `
      <div class="profile-info">
        ${img}
        <span class="profile-name">${escapeHtml(currentUser.name)}</span>
      </div>
      <div class="token-wrapper">
        <span class="credit-badge${lowClass}" id="credit-badge">${currentUser.credits} 크레딧</span>
      </div>
      <button class="logout-btn" id="logout-btn" title="로그아웃">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M18.36 6.64a9 9 0 1 1-12.73 0"></path>
          <line x1="12" y1="2" x2="12" y2="12"></line>
        </svg>
      </button>
    `;

    document.getElementById("logout-btn").addEventListener("click", handleLogout);
    document.getElementById("credit-badge").addEventListener("click", toggleTokenPopover);
  } else {
    queryInput.disabled = true;
    sendBtn.disabled = true;

    authArea.innerHTML = `<a href="/api/auth/login" class="login-btn google-login">Google 로그인</a>`;
  }
  showWelcome();
}

function updateCreditDisplay(credits) {
  if (currentUser) currentUser.credits = credits;
  const badge = document.getElementById("credit-badge");
  if (!badge) return;
  badge.textContent = `${credits} 크레딧`;
  badge.classList.toggle("low", credits <= lowCreditThreshold);
}

async function handleLogout() {
  await fetch("/api/auth/logout", { method: "POST" });
  currentUser = null;
  currentSessionId = null;
  sessions = [];
  renderSessionList();
  renderAuthUI();
  showWelcome();
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
      <span>크레딧 사용 내역</span>
    </div>
    <div class="token-history"><div class="token-history-loading">불러오는 중...</div></div>
    <div class="token-popover-footer">
      <div class="token-purchase-row">
        <input type="number" class="token-purchase-input" min="1" max="1000" value="5" placeholder="수량">
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
      const date = formatLocal(t.created_at);
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
  alert("크레딧 구매 기능은 준비 중입니다.");
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
  closeSidebar();
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
    showWelcome();
  }

}

function startNewChat() {
  currentSessionId = null;
  renderSessionList();
  showWelcome();
  queryInput.focus();
}

async function deleteSession(id) {
  if (!confirm("이 대화를 삭제하시겠습니까?")) return;
  try {
    await fetch(`/api/sessions/${id}`, { method: "DELETE" });
    sessions = sessions.filter((s) => s.id !== id);
    if (currentSessionId === id) {
      currentSessionId = null;
      showWelcome();
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
    const category = document.getElementById("category-select").value || null;
    const model = document.getElementById("model-select").value;
    const body = { query, collections, category, model };
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
      answerEl.textContent = "크레딧이 부족합니다. 구매 후 다시 시도해주세요.";
      updateCreditDisplay(0);
      setLoading(false);
      return;
    }

    if (res.status === 503) {
      const data = await res.json();
      answerEl.textContent = data.error || "모델을 사용할 수 없습니다.";
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

function formatLocal(utcStr) {
  const d = new Date(utcStr + (utcStr.endsWith("Z") ? "" : "Z"));
  const yy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${yy}-${mm}-${dd} ${hh}:${mi}:${ss}`;
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------
async function loadModels() {
  try {
    const res = await fetch("/api/models");
    const data = await res.json();
    availableModels = data.models || [];
  } catch {
    availableModels = [];
  }
  renderModelSelect();
}

function renderModelSelect() {
  const select = document.getElementById("model-select");
  const prev = select.value;
  select.innerHTML = "";
  for (const m of availableModels) {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = `${m.label} (${m.credits})`;
    select.appendChild(opt);
  }
  // Restore previous selection if still available
  if (prev && availableModels.some((m) => m.id === prev)) {
    select.value = prev;
  }
}

// ---------------------------------------------------------------------------
// Welcome screen
// ---------------------------------------------------------------------------
function buildWelcomeModelRows() {
  if (!availableModels.length) return "";
  // Group models into rows of 2
  const rows = [];
  for (let i = 0; i < availableModels.length; i += 2) {
    const pair = availableModels.slice(i, i + 2);
    const spans = pair.map((m) => `<span class="welcome-model">${escapeHtml(m.label)} (${m.credits})</span>`).join("");
    rows.push(`<div class="welcome-models-row">${spans}</div>`);
  }
  return rows.join("");
}

function showWelcome() {
  // Don't overwrite if there are actual messages displayed
  if (chat.querySelector(".msg")) return;

  const loginHtml = currentUser
    ? ""
    : `<div class="welcome-login">
        <p>질문하려면 로그인하세요</p>
        <a href="/api/auth/login" class="login-btn google-login">Google 로그인</a>
      </div>`;

  chat.innerHTML = `
    <div class="welcome">
      <div>
        <div class="welcome-title">PitBot</div>
        <p class="welcome-subtitle">자작자동차 규정 및 Q&A 챗봇</p>
      </div>
      <div class="welcome-models">
        <div class="welcome-models-title">사용 가능 모델 (소모 크레딧)</div>
        <div class="welcome-models-grid">
          ${buildWelcomeModelRows()}
        </div>
      </div>
      <div class="welcome-items">
        <div class="welcome-item">
          <span class="welcome-icon">&#9889;</span>
          <span>질문 1회당 선택한 모델에 따라 크레딧이 차감됩니다</span>
        </div>
        <div class="welcome-item">
          <span class="welcome-icon">&#128218;</span>
          <span>입력창 상단에서 AI가 검색에 사용할 데이터를 선택할 수 있습니다.
            <ul class="welcome-chip-list">
              <li><b>카테고리</b> &mdash; Q&A 카테고리 필터</li>
              <li><b>Q&A</b> &mdash; QnA 게시판 데이터</li>
              <li><b>규정</b> &mdash; 대회 규정집 (2026 Formula)</li>
            </ul>
          </span>
        </div>
      </div>
      <div class="welcome-warn">LLM은 실수하거나 잘못된 정보를 제공할 수 있으며, AI 답변은 차량검차 시 근거자료로 사용할 수 없습니다.</div>
      <div class="welcome-contact">문의: <a href="mailto:mail@luftaquila.io">mail@luftaquila.io</a></div>
      ${loginHtml}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
initTheme();
loadModels().then(() => showWelcome());
checkAuth();
