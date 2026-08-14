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
let lowCreditThreshold = 5;
let unlimitedCredits = false;

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
    unlimitedCredits = !!data.unlimited_credits;
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

    const avatar = currentUser.picture
      ? `<img src="${escapeAttr(currentUser.picture)}" class="profile-img" alt="" referrerpolicy="no-referrer">`
      : `<span class="profile-avatar" aria-hidden="true">${escapeHtml(Array.from(currentUser.name || "?")[0])}</span>`;
    const adminLink = currentUser.is_admin
      ? `<a href="/admin" class="profile-admin-link">관리자</a>`
      : "";
    const creditText = unlimitedCredits ? "∞ 이용권" : `${currentUser.credits} 이용권`;
    const lowClass = !unlimitedCredits && currentUser.credits <= lowCreditThreshold ? " low" : "";

    authArea.innerHTML = `
      <div class="profile-info">
        <a href="/account" class="profile-account-link" aria-label="${escapeAttr(currentUser.name)} 마이페이지" title="마이페이지">
          ${avatar}
          <span class="profile-name">${escapeHtml(currentUser.name)}</span>
        </a>
        ${adminLink}
      </div>
      <div class="token-wrapper">
        <span class="credit-badge${lowClass}" id="credit-badge">${creditText}</span>
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
  if (unlimitedCredits) {
    badge.textContent = "∞ 이용권";
    badge.classList.remove("low");
  } else {
    badge.textContent = `${credits} 이용권`;
    badge.classList.toggle("low", credits <= lowCreditThreshold);
  }
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
      <span>이용권 사용 내역</span>
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
      let memo = t.memo || t.type;
      if (t.type === "usage") memo = "질문";
      if (t.type === "refund" && /^(오류 환불|요청 저장 실패 환불) \(/.test(memo)) {
        memo = memo.split(" (", 1)[0];
      }
      return `<div class="token-tx ${cls}">
        <div class="token-tx-info">
          <span class="token-tx-memo">${escapeHtml(memo)}</span>
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
  alert("이용권 구매 기능은 준비 중입니다.");
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
          } catch (e) { console.warn("Failed to parse sources JSON:", e); }
        }
        answerEl.innerHTML = marked.parse(msg.content || "");
      }
    }
    scrollToBottom();
  } catch (e) {
    console.warn("Failed to load session messages:", e);
    showWelcome();
  }

}

function startNewChat() {
  currentSessionId = null;
  renderSessionList();
  chat.innerHTML = "";
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
  renderThemeToggle(theme);
}

function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("theme", theme);
  renderThemeToggle(theme);
}

function renderThemeToggle(theme) {
  const dark = theme === "dark";
  themeToggle.setAttribute("aria-label", dark ? "라이트 모드로 전환" : "다크 모드로 전환");
  themeToggle.innerHTML = dark
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 15.2A8.5 8.5 0 0 1 8.8 4 8.5 8.5 0 1 0 20 15.2Z"/></svg>';
}

themeToggle.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  setTheme(current === "dark" ? "light" : "dark");
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  if (!localStorage.getItem("theme")) {
    const theme = e.matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    renderThemeToggle(theme);
  }
});

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  chat.querySelector(".welcome")?.remove();
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
      answerEl.textContent = "이용권이 부족합니다. 구매 후 다시 시도해주세요.";
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

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop();

        for (const block of blocks) {
          let eventType = null;
          const dataLines = [];
          for (const line of block.split(/\r?\n/)) {
            if (line.startsWith("event: ")) eventType = line.slice(7);
            if (line.startsWith("data: ")) dataLines.push(line.slice(6));
          }
          if (!eventType || !dataLines.length) continue;
          const data = dataLines.join("\n");

          if (eventType === "session") {
            try {
              const payload = JSON.parse(data);
              if (payload.session_id) {
                currentSessionId = payload.session_id;
                loadSessions();
              }
            } catch (e) { console.warn("Failed to parse session event:", e); }
          } else {
            handleEvent(eventType, data, sourcesContainer, answerEl, { fullText });
            if (eventType === "token") {
              try { fullText += JSON.parse(data); } catch (e) { console.warn("Failed to parse token data:", e); }
            }
          }
        }
      }
    } catch (streamErr) {
      // Network error during streaming (e.g. app switch on mobile)
      // Preserve partial text if available
      if (!fullText) throw streamErr;
      fullText += "\n\n---\n*연결이 끊어져 응답이 중단되었습니다.*";
    }

    // Final render
    renderAnswerContent(answerEl, fullText);
  } catch (err) {
    answerEl.textContent = `오류가 발생했습니다: ${err.message}`;
  }

  setLoading(false);
  scrollToBottom();
});

function handleEvent(type, data, sourcesContainer, answerEl, state) {
  if (type === "rewrite") {
    try {
      const rewritten = JSON.parse(data);
      const dots = answerEl.querySelector(".loading-dots");
      if (dots) dots.textContent = `검색 중: ${rewritten}`;
    } catch (e) { console.warn("Failed to parse rewrite event:", e); }
  } else if (type === "sources") {
    try {
      const sources = JSON.parse(data);
      renderSources(sourcesContainer, sources);
      // Update loading text to indicate answer generation
      const dots = answerEl.querySelector(".loading-dots");
      if (dots) dots.textContent = "답변 생성 중";
    } catch (e) { console.warn("Failed to parse sources event:", e); }
  } else if (type === "retrieval") {
    try {
      const retrieval = JSON.parse(data);
      if (retrieval.status === "partial") {
        addAnswerNotice(answerEl, "일부 검색 경로가 실패해 사용 가능한 결과만으로 답변했습니다.");
      }
    } catch (e) { console.warn("Failed to parse retrieval event:", e); }
  } else if (type === "credits") {
    try {
      const credits = JSON.parse(data);
      if (Number.isInteger(credits.remaining)) updateCreditDisplay(credits.remaining);
    } catch (e) { console.warn("Failed to parse credits event:", e); }
  } else if (type === "token") {
    try {
      const token = JSON.parse(data);
      state.fullText = (state.fullText || "") + token;
      // Incremental markdown render
      renderAnswerContent(answerEl, state.fullText);
      scrollToBottom();
    } catch (e) { console.warn("Failed to parse token event:", e); }
  }
}

function addAnswerNotice(answerEl, message) {
  let notices = [];
  try { notices = JSON.parse(answerEl.dataset.notices || "[]"); } catch { notices = []; }
  if (!notices.includes(message)) notices.push(message);
  answerEl.dataset.notices = JSON.stringify(notices);
}

function renderAnswerContent(answerEl, markdown) {
  let notices = [];
  try { notices = JSON.parse(answerEl.dataset.notices || "[]"); } catch { notices = []; }
  const noticeHtml = notices.map((notice) => `<div class="answer-notice">${escapeHtml(notice)}</div>`).join("");
  answerEl.innerHTML = noticeHtml + marked.parse(markdown);
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
    <div class="answer"><span class="loading-dots">검색 중</span></div>
    <div class="sources"></div>
  `;
  chat.appendChild(el);
  scrollToBottom();
  return el;
}

const CONF_CLASS = {
  "합의됨": "conf-ok",
  "다수의견": "conf-major",
  "단일제보": "conf-single",
  "미해결": "conf-open",
};

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

    // 신뢰도는 이 데이터의 핵심 축이므로 문자열에 묻지 않고 배지로 뺀다.
    const conf = s.confidence
      ? `<span class="conf-badge ${CONF_CLASS[s.confidence] || ""}">${escapeHtml(s.confidence)}</span>`
      : "";
    const header = conf
      ? escapeHtml(s.source).replace(/^\[[^\]]*·[^\]]*\]\s*/, "")
      : escapeHtml(s.source);

    let html = `<div class="source-header">${conf}${header}</div>`;
    html += `<span class="source-score">유사도: ${(s.score * 100).toFixed(1)}%</span>`;
    if (s.url) {
      html += ` <a href="${escapeAttr(s.url)}" target="_blank" rel="noopener">원문 보기</a>`;
    } else if (s.dates && s.dates.length) {
      // 익명 채팅 출처는 링크할 원문이 없다. 발언 날짜가 유일한 대조 단서다.
      const shown = s.dates.slice(0, 3).join(", ");
      const more = s.dates.length > 3 ? ` 외 ${s.dates.length - 3}건` : "";
      html += ` <span class="source-dates">발언일: ${escapeHtml(shown)}${more}</span>`;
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
// Collections (검색 소스) — 사용자는 데이터 종류만 고른다.
// 종목, 규정 유형, Q&A 카테고리는 서버가 질문에서 자동 판별한다.
// ---------------------------------------------------------------------------
let availableCollections = [];

async function loadCollections() {
  try {
    const res = await fetch("/api/collections");
    const data = await res.json();
    availableCollections = data.collections || [];
  } catch {
    availableCollections = [];
  }
  renderCollectionChips();
}

function renderCollectionChips() {
  const host = document.getElementById("collection-chips");
  if (!host) return;
  host.innerHTML = "";

  for (const c of availableCollections) {
    host.appendChild(buildCollectionChip(c, "checked"));
  }
}

function buildCollectionChip(collection, checked = "checked") {
  const label = document.createElement("label");
  label.className = "collection-chip";
  label.title = collection.description;
  const checkedAttr = checked ? "checked" : "";
  label.innerHTML =
    `<input type="checkbox" name="collections" value="${escapeAttr(collection.key)}" ${checkedAttr}>` +
    `<span>${escapeHtml(collection.label)}</span>`;
  const checkbox = label.querySelector("input");
  checkbox.addEventListener("change", () => {
    const enabledSources = form.querySelectorAll('input[name="collections"]:checked');
    if (enabledSources.length === 0) checkbox.checked = true;
  });
  return label;
}

// ---------------------------------------------------------------------------
// Welcome screen
// ---------------------------------------------------------------------------
function buildWelcomeSourceRows() {
  return availableCollections.map(
    (collection) => `<li><b>${escapeHtml(collection.label)}</b> &mdash; ${escapeHtml(collection.description)}</li>`
  ).join("");
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
      <img class="welcome-logo" src="/static/logo.svg" alt="">
      <h2 class="welcome-title">PitBot</h2>
      <p class="welcome-subtitle">자작자동차 규정 및 Q&amp;A 챗봇</p>
      <div class="welcome-items">
        <div class="welcome-item">
          <span class="welcome-icon" aria-hidden="true">&#9889;</span>
          <span>질문 1회당 이용권 1장이 차감됩니다.<br>매월 1일마다 이용권이 무료로 다시 충전됩니다.</span>
        </div>
        <div class="welcome-item">
          <span class="welcome-icon" aria-hidden="true">&#128218;</span>
          <span>입력창 상단에서 AI가 검색에 사용할 데이터를 선택할 수 있습니다.
            <ul class="welcome-chip-list">${buildWelcomeSourceRows()}</ul>
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
loadCollections().then(() => showWelcome());
checkAuth();
