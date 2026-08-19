const themeToggle = document.getElementById("theme-toggle");
const usersTbody = document.getElementById("users-tbody");
const userSearch = document.getElementById("user-search");
const convUserSelect = document.getElementById("conv-user-select");
const convSessionList = document.getElementById("conv-session-list");
const convMessages = document.getElementById("conv-messages");

let allUsers = [];
let allModels = [];
let currentConvSessionId = null;
let lowCreditThreshold = 5;
let sortColumn = null;
let sortDirection = "asc";
let overviewPeriod = "30d";

// ---------------------------------------------------------------------------
// Theme (reused from script.js)
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

// ---------------------------------------------------------------------------
// Admin check
// ---------------------------------------------------------------------------
async function checkAdmin() {
  try {
    const res = await fetch("/api/admin/check");
    if (!res.ok) {
      window.location.href = "/";
      return false;
    }
    return true;
  } catch {
    window.location.href = "/";
    return false;
  }
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
let convLoaded = false;
function activateAdminTab(tabName) {
  document.querySelectorAll(".admin-tab").forEach((tab) => {
    const active = tab.dataset.tab === tabName;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tabName}`);
  });
  if (tabName === "payments") {
    loadPayments();
  }
  if (tabName === "conversations" && !convLoaded) {
    convLoaded = true;
    loadSessions("");
  }
}

document.querySelectorAll(".admin-tab").forEach((tab) => {
  tab.addEventListener("click", () => activateAdminTab(tab.dataset.tab));
});

// ---------------------------------------------------------------------------
// Users tab
// ---------------------------------------------------------------------------
async function loadUsers() {
  try {
    const res = await fetch("/api/admin/users");
    const data = await res.json();
    allUsers = data.users || [];
  } catch {
    allUsers = [];
  }
  renderUsers();
  populateUserFilter();
}

function sortUsers(users) {
  if (!sortColumn) return users;
  const sorted = [...users];
  sorted.sort((a, b) => {
    let va, vb;
    switch (sortColumn) {
      case "name":
        va = (a.name || "").toLowerCase();
        vb = (b.name || "").toLowerCase();
        return va < vb ? -1 : va > vb ? 1 : 0;
      case "credits":
        return (a.credits || 0) - (b.credits || 0);
      case "tokens":
        va = (a.total_input_tokens || 0) + (a.total_output_tokens || 0) + (a.total_thinking_tokens || 0);
        vb = (b.total_input_tokens || 0) + (b.total_output_tokens || 0) + (b.total_thinking_tokens || 0);
        return va - vb;
      case "last_active":
        va = a.last_active_at || "";
        vb = b.last_active_at || "";
        return va < vb ? -1 : va > vb ? 1 : 0;
      case "created":
        va = a.created_at || "";
        vb = b.created_at || "";
        return va < vb ? -1 : va > vb ? 1 : 0;
      default:
        return 0;
    }
  });
  if (sortDirection === "desc") sorted.reverse();
  return sorted;
}

function updateSortIcons() {
  document.querySelectorAll(".users-table th.sortable").forEach((th) => {
    const icon = th.querySelector(".sort-icon");
    if (th.dataset.sort === sortColumn) {
      icon.textContent = sortDirection === "asc" ? " \u25B2" : " \u25BC";
      th.classList.add("sorted");
    } else {
      icon.textContent = "";
      th.classList.remove("sorted");
    }
  });
}

function renderUsers(filter = "") {
  const q = filter.toLowerCase();
  let filtered = q
    ? allUsers.filter((u) => u.name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q))
    : allUsers;
  filtered = sortUsers(filtered);
  updateSortIcons();

  usersTbody.innerHTML = filtered
    .map((u) => {
      const pic = u.picture
        ? `<img src="${escapeAttr(u.picture)}" class="user-picture" referrerpolicy="no-referrer">`
        : "";
      const date = u.created_at ? formatLocal(u.created_at) : "";
      const lastActive = u.last_active_at ? formatRelative(u.last_active_at) : "-";
      const lastActiveTitle = u.last_active_at ? formatLocal(u.last_active_at) : "";
      const lowClass = u.credits <= lowCreditThreshold ? " low" : "";
      const totalIn = u.total_input_tokens || 0;
      const totalOut = u.total_output_tokens || 0;
      const totalThink = u.total_thinking_tokens || 0;
      const cost = estimateModelCost(u.model_usage || []);
      return `<tr data-user-id="${u.id}">
        <td><button class="user-conversation-link" type="button" onclick="openUserConversations(${u.id})" aria-label="${escapeAttr(u.name)} 사용자의 대화 기록 보기">${pic}<span class="user-identity"><span>${escapeHtml(u.name)}</span><span class="user-email">${escapeHtml(u.email)}</span></span></button></td>
        <td>
          <div class="credit-cell" id="credit-cell-${u.id}">
            <div class="token-wrapper">
              <span class="credit-badge${lowClass}" onclick="toggleAdminPopover(${u.id})">${u.credits} 이용권</span>
            </div>
            <button class="credit-adjust-btn" onclick="showCreditEditor(${u.id}, ${u.credits})">조정</button>
          </div>
        </td>
        <td class="api-token-cell">
          <div class="api-token-wrapper" id="api-token-wrapper-${u.id}">
            <span class="api-usage-chip clickable" onclick="toggleApiTokenPopover(${u.id})">IN ${totalIn.toLocaleString()} / OUT ${totalOut.toLocaleString()} / THK ${totalThink.toLocaleString()}</span>
            <span class="api-cost-chip">${cost}</span>
          </div>
        </td>
        <td class="last-active-cell" title="${lastActiveTitle}">${lastActive}</td>
        <td>${date}</td>
      </tr>`;
    })
    .join("");
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("ko-KR");
}

function formatDuration(value) {
  if (value === null || value === undefined) return "—";
  const milliseconds = Number(value);
  return milliseconds < 1000
    ? `${formatNumber(milliseconds)}ms`
    : `${(milliseconds / 1000).toFixed(1)}초`;
}

function formatUsd(value) {
  const cost = Number(value || 0);
  return cost < 0.01 ? `$${cost.toFixed(4)}` : `$${cost.toFixed(2)}`;
}

function renderAdminOverview(overview) {
  const el = document.getElementById("usage-summary");
  if (!el) return;
  const users = overview.users || {};
  const activity = overview.activity || {};
  const reliability = overview.reliability || {};
  const tokens = overview.tokens || {};
  const activeUsers = Number(users.active_users || 0);
  const questions = Number(activity.questions || 0);
  const questionsPerUser = activeUsers ? (questions / activeUsers).toFixed(1) : "—";
  const successRate = reliability.success_rate === null || reliability.success_rate === undefined
    ? "—"
    : `${reliability.success_rate}%`;
  const fallbackRate = reliability.fallback_rate === null || reliability.fallback_rate === undefined
    ? "—"
    : `${reliability.fallback_rate}%`;
  const modelCards = (overview.models || []).map((model) => `
    <article class="overview-model-card">
      <div><h4>${escapeHtml(model.label || model.model || "미기록 모델")}</h4><span>${formatNumber(model.message_count)}회 응답</span></div>
      <dl>
        <div><dt>입력</dt><dd>${formatNumber(model.input_tokens)}</dd></div>
        <div><dt>출력</dt><dd>${formatNumber(model.output_tokens)}</dd></div>
        <div><dt>추론</dt><dd>${formatNumber(model.thinking_tokens)}</dd></div>
        <div><dt>추정 비용</dt><dd>${formatUsd(model.estimated_cost_usd)}</dd></div>
      </dl>
    </article>
  `).join("") || '<p class="overview-empty">선택한 기간의 모델 사용 기록이 없습니다.</p>';
  const maxQuestions = Math.max(1, ...(overview.daily || []).map((day) => Number(day.questions || 0)));
  const dailyBars = (overview.daily || []).map((day) => {
    const questionCount = Number(day.questions || 0);
    const height = questionCount ? Math.max(8, Math.round(questionCount * 100 / maxQuestions)) : 2;
    const shortDate = day.date.slice(5).replace("-", "/");
    return `<div class="activity-day" title="${escapeAttr(day.date)} · 질문 ${questionCount}회 · 사용자 ${formatNumber(day.active_users)}명">
      <span class="activity-value">${questionCount || ""}</span>
      <span class="activity-bar" style="height:${height}%"></span>
      <span class="activity-date" data-short="${escapeAttr(day.date.slice(-2))}">${shortDate}</span>
    </div>`;
  }).join("");

  el.innerHTML = `
    <div class="overview-kpi-grid">
      <article class="overview-card">
        <h3>사용자</h3><p class="overview-value">${formatNumber(users.total_users)}<span>명</span></p>
        <dl><div><dt>기간 활성</dt><dd>${formatNumber(users.active_users)}명</dd></div><div><dt>신규 가입</dt><dd>${formatNumber(users.new_users)}명</dd></div></dl>
      </article>
      <article class="overview-card">
        <h3>질문과 답변</h3><p class="overview-value">${formatNumber(activity.questions)}<span>회 질문</span></p>
        <dl><div><dt>저장된 답변</dt><dd>${formatNumber(activity.answers)}회</dd></div><div><dt>활성 사용자당</dt><dd>${questionsPerUser}회</dd></div></dl>
      </article>
      <article class="overview-card">
        <h3>답변 안정성</h3><p class="overview-value">${successRate}<span>성공</span></p>
        <dl><div><dt>오류</dt><dd>${formatNumber(reliability.failed_turns)}회</dd></div><div><dt>폴백</dt><dd>${formatNumber(reliability.fallback_turns)}회 · ${fallbackRate}</dd></div><div><dt>검색 저하</dt><dd>${formatNumber(reliability.degraded_retrieval_turns)}회</dd></div></dl>
      </article>
      <article class="overview-card">
        <h3>응답 속도</h3><p class="overview-value">${formatDuration(reliability.avg_first_token_ms)}<span>첫 응답</span></p>
        <dl><div><dt>전체 완료</dt><dd>${formatDuration(reliability.avg_total_ms)}</dd></div><div><dt>추적 요청</dt><dd>${formatNumber(reliability.tracked_turns)}회</dd></div><div><dt>처리 중</dt><dd>${formatNumber(reliability.pending_turns)}회</dd></div></dl>
      </article>
      <article class="overview-card">
        <h3>이용권</h3><p class="overview-value">${formatNumber(activity.credits_used)}<span>장 사용</span></p>
        <dl><div><dt>환불</dt><dd>${formatNumber(activity.credits_refunded)}장</dd></div><div><dt>현재 총 잔액</dt><dd>${formatNumber(users.current_credits)}장</dd></div><div><dt>부족 사용자</dt><dd>${formatNumber(users.low_credit_users)}명</dd></div></dl>
      </article>
      <article class="overview-card">
        <h3>API 토큰과 비용</h3><p class="overview-value">${formatNumber(tokens.total_tokens)}<span>토큰</span></p>
        <dl><div><dt>입력 / 출력</dt><dd>${formatNumber(tokens.input_tokens)} / ${formatNumber(tokens.output_tokens)}</dd></div><div><dt>추론</dt><dd>${formatNumber(tokens.thinking_tokens)}</dd></div><div><dt>추정 비용</dt><dd>${formatUsd(tokens.estimated_cost_usd)}</dd></div></dl>
      </article>
    </div>
    <div class="overview-detail-grid">
      <section class="overview-detail" aria-labelledby="activity-trend-title">
        <div class="overview-detail-title"><h3 id="activity-trend-title">최근 일별 질문</h3><span>KST · 최대 14일</span></div>
        <div class="activity-chart" role="img" aria-label="최근 일별 질문 수 막대 그래프">${dailyBars}</div>
      </section>
      <section class="overview-detail" aria-labelledby="model-cost-title">
        <div class="overview-detail-title"><h3 id="model-cost-title">모델·비용 상세</h3><span>공급자 단가 기준 추정</span></div>
        <div class="overview-models">${modelCards}</div>
      </section>
    </div>
  `;
}

async function loadAdminOverview(period = overviewPeriod) {
  const el = document.getElementById("usage-summary");
  overviewPeriod = period;
  document.querySelectorAll(".overview-period button").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.period === period));
  });
  if (el) el.setAttribute("aria-busy", "true");
  try {
    const res = await fetch(`/api/admin/overview?period=${encodeURIComponent(period)}`);
    if (!res.ok) throw new Error();
    const data = await res.json();
    renderAdminOverview(data.overview || {});
  } catch {
    if (el) el.innerHTML = '<p class="overview-error">운영 통계를 불러오지 못했습니다.</p>';
  } finally {
    if (el) el.setAttribute("aria-busy", "false");
  }
}

document.querySelectorAll(".overview-period button").forEach((button) => {
  button.addEventListener("click", () => loadAdminOverview(button.dataset.period));
});

userSearch.addEventListener("input", () => {
  renderUsers(userSearch.value);
});

document.querySelectorAll(".users-table th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (sortColumn === col) {
      sortDirection = sortDirection === "asc" ? "desc" : "asc";
    } else {
      sortColumn = col;
      sortDirection = col === "credits" || col === "tokens" || col === "last_active" || col === "created" ? "desc" : "asc";
    }
    renderUsers(userSearch.value);
  });
});

// Credit editor
window.showCreditEditor = function (userId, currentCredits) {
  const cell = document.getElementById(`credit-cell-${userId}`);
  if (!cell) return;
  cell.innerHTML = `
    <div class="credit-editor">
      <input type="number" id="credit-input-${userId}" value="${currentCredits}">
      <button class="credit-save-btn" onclick="saveCredits(${userId})">저장</button>
      <button class="credit-cancel-btn" onclick="cancelCreditEdit(${userId}, ${currentCredits})">취소</button>
    </div>
  `;
  document.getElementById(`credit-input-${userId}`).focus();
};

window.saveCredits = async function (userId) {
  const input = document.getElementById(`credit-input-${userId}`);
  if (!input) return;

  const credits = parseInt(input.value, 10);
  if (isNaN(credits) || credits < 0) return;

  const memo = "관리자 조정";

  try {
    const res = await fetch(`/api/admin/users/${userId}/credits`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credits, memo }),
    });
    if (res.ok) {
      const data = await res.json();
      // Update local data
      const user = allUsers.find((u) => u.id === userId);
      if (user) user.credits = data.credits;
      renderUsers(userSearch.value);
      loadAdminOverview();
    } else {
      const err = await res.json();
      alert(err.error || "저장에 실패했습니다");
    }
  } catch {
    alert("저장에 실패했습니다");
  }
};

window.cancelCreditEdit = function (userId, currentCredits) {
  const cell = document.getElementById(`credit-cell-${userId}`);
  if (!cell) return;
  const lowClass = currentCredits <= lowCreditThreshold ? " low" : "";
  cell.innerHTML = `
    <div class="token-wrapper">
      <span class="credit-badge${lowClass}" onclick="toggleAdminPopover(${userId})">${currentCredits} 이용권</span>
    </div>
    <button class="credit-adjust-btn" onclick="showCreditEditor(${userId}, ${currentCredits})">조정</button>
  `;
};

// Token history popover (same UI as chat page)
let adminPopover = null;
let adminPopoverUserId = null;

window.toggleAdminPopover = function (userId) {
  if (adminPopover && adminPopoverUserId === userId) {
    closeAdminPopover();
    return;
  }
  closeAdminPopover();

  adminPopoverUserId = userId;
  const wrapper = document.querySelector(`#credit-cell-${userId} .token-wrapper`);
  if (!wrapper) return;

  adminPopover = document.createElement("div");
  adminPopover.className = "token-popover";
  adminPopover.innerHTML = `
    <div class="token-popover-header">이용권 사용 내역</div>
    <div class="token-history"><div class="token-history-loading">불러오는 중...</div></div>
  `;
  wrapper.appendChild(adminPopover);

  loadAdminTransactions(userId);
  setTimeout(() => document.addEventListener("click", onAdminPopoverOutside), 0);
};

function closeAdminPopover() {
  if (adminPopover) {
    adminPopover.remove();
    adminPopover = null;
    adminPopoverUserId = null;
  }
  document.removeEventListener("click", onAdminPopoverOutside);
}

function onAdminPopoverOutside(e) {
  if (adminPopover && !adminPopover.contains(e.target) && !e.target.classList.contains("credit-badge")) {
    closeAdminPopover();
  }
}

async function loadAdminTransactions(userId) {
  const historyEl = adminPopover?.querySelector(".token-history");
  if (!historyEl) return;

  try {
    const res = await fetch(`/api/admin/users/${userId}/transactions`);
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
      const date = t.created_at ? formatLocal(t.created_at) : "";
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

// ---------------------------------------------------------------------------
// API token usage popover (per-model breakdown)
// ---------------------------------------------------------------------------
let apiTokenPopover = null;
let apiTokenPopoverUserId = null;

window.toggleApiTokenPopover = function (userId) {
  if (apiTokenPopover && apiTokenPopoverUserId === userId) {
    closeApiTokenPopover();
    return;
  }
  closeApiTokenPopover();

  apiTokenPopoverUserId = userId;
  const wrapper = document.getElementById(`api-token-wrapper-${userId}`);
  if (!wrapper) return;

  apiTokenPopover = document.createElement("div");
  apiTokenPopover.className = "token-popover api-token-popover";
  apiTokenPopover.innerHTML = `
    <div class="token-popover-header">모델별 API 사용량</div>
    <div class="token-history"><div class="token-history-loading">불러오는 중...</div></div>
  `;
  wrapper.appendChild(apiTokenPopover);

  loadApiTokenUsage(userId);
  setTimeout(() => document.addEventListener("click", onApiTokenPopoverOutside), 0);
};

function closeApiTokenPopover() {
  if (apiTokenPopover) {
    apiTokenPopover.remove();
    apiTokenPopover = null;
    apiTokenPopoverUserId = null;
  }
  document.removeEventListener("click", onApiTokenPopoverOutside);
}

function onApiTokenPopoverOutside(e) {
  if (apiTokenPopover && !apiTokenPopover.contains(e.target) && !e.target.classList.contains("api-usage-chip")) {
    closeApiTokenPopover();
  }
}

async function loadApiTokenUsage(userId) {
  const historyEl = apiTokenPopover?.querySelector(".token-history");
  if (!historyEl) return;

  try {
    const res = await fetch(`/api/admin/users/${userId}/token-usage`);
    const data = await res.json();
    const usage = data.usage || [];

    if (!usage.length) {
      historyEl.innerHTML = `<div class="token-history-empty">사용 내역이 없습니다</div>`;
      return;
    }

    let totalCost = 0;
    const rows = usage.map((u) => {
      const model = u.model || "(미기록)";
      const p = MODEL_PRICING[u.model] || DEFAULT_PRICING;
      const cost = (u.input_tokens * p.input + u.output_tokens * p.output + u.thinking_tokens * p.thinking) / 1_000_000;
      totalCost += cost;
      const costStr = cost < 0.01 ? "$" + cost.toFixed(4) : "$" + cost.toFixed(2);
      return `<div class="api-model-row">
        <div class="api-model-header">
          <span class="api-model-name">${escapeHtml(model)}</span>
          <span class="api-model-cost">${costStr}</span>
        </div>
        <div class="api-model-details">
          <span>IN ${u.input_tokens.toLocaleString()}</span>
          <span>OUT ${u.output_tokens.toLocaleString()}</span>
          <span>THK ${u.thinking_tokens.toLocaleString()}</span>
          <span class="api-model-count">${u.message_count}회</span>
        </div>
      </div>`;
    }).join("");

    const totalCostStr = totalCost < 0.01 ? "$" + totalCost.toFixed(4) : "$" + totalCost.toFixed(2);
    historyEl.innerHTML = rows + `<div class="api-model-total">
      <span>합계</span>
      <span class="api-model-cost">${totalCostStr}</span>
    </div>`;
  } catch {
    historyEl.innerHTML = `<div class="token-history-empty">불러오기 실패</div>`;
  }
}

// ---------------------------------------------------------------------------
// Conversations tab
// ---------------------------------------------------------------------------
function populateUserFilter() {
  convUserSelect.innerHTML = `<option value="">전체 사용자</option>`;
  for (const u of allUsers) {
    convUserSelect.innerHTML += `<option value="${u.id}">${escapeHtml(u.name)} (${escapeHtml(u.email)})</option>`;
  }
}

window.openUserConversations = function (userId) {
  convLoaded = true;
  activateAdminTab("conversations");
  convUserSelect.value = String(userId);
  loadSessions(String(userId));
};

convUserSelect.addEventListener("change", () => {
  loadSessions(convUserSelect.value);
});

async function loadSessions(userId) {
  try {
    const url = userId ? `/api/admin/users/${userId}/sessions` : `/api/admin/sessions`;
    const res = await fetch(url);
    const data = await res.json();
    const sessions = data.sessions || [];
    renderSessionList(sessions);
  } catch {
    convSessionList.innerHTML = "";
  }
}

function renderSessionList(sessions) {
  currentConvSessionId = null;
  convMessages.innerHTML = `<div class="conv-empty">대화를 선택하세요</div>`;

  if (!sessions.length) {
    convSessionList.innerHTML = `<div class="conv-list-empty">대화 기록이 없습니다</div>`;
    return;
  }

  convSessionList.innerHTML = sessions
    .map((s) => {
      const date = s.updated_at ? formatLocal(s.updated_at) : "";
      const userName = s.user_name || "";
      const deleted = s.deleted_at ? ' <span class="session-deleted-badge">삭제됨</span>' : "";
      return `<div class="conv-session-item${s.deleted_at ? " deleted" : ""}" data-session-id="${s.id}">
        <div class="conv-session-title">${escapeHtml(s.title)}${deleted}</div>
        <div class="conv-session-meta">${escapeHtml(userName)} &middot; ${date}</div>
      </div>`;
    })
    .join("");

  convSessionList.querySelectorAll(".conv-session-item").forEach((el) => {
    el.addEventListener("click", () => {
      convSessionList.querySelectorAll(".conv-session-item").forEach((e) => e.classList.remove("active"));
      el.classList.add("active");
      loadMessages(parseInt(el.dataset.sessionId, 10));
    });
  });
}

async function loadMessages(sessionId) {
  currentConvSessionId = sessionId;
  convMessages.innerHTML = `<div class="conv-empty">불러오는 중...</div>`;

  try {
    const res = await fetch(`/api/admin/sessions/${sessionId}/messages`);
    const data = await res.json();
    const messages = data.messages || [];

    if (!messages.length) {
      convMessages.innerHTML = `<div class="conv-empty">메시지가 없습니다</div>`;
      return;
    }

    convMessages.innerHTML = "";
    for (const msg of messages) {
      const role = msg.role === "user" ? "user" : "assistant";
      const roleLabel = role === "user" ? "사용자" : "어시스턴트";
      const time = msg.created_at ? formatLocal(msg.created_at) : "";

      const el = document.createElement("div");
      el.className = `admin-msg ${role}`;

      const roleEl = document.createElement("div");
      roleEl.className = "admin-msg-role";
      roleEl.textContent = roleLabel;
      el.appendChild(roleEl);

      // Sources (assistant only)
      if (role === "assistant" && msg.sources) {
        try {
          const sources = JSON.parse(msg.sources);
          if (sources.length) {
            const sourcesContainer = document.createElement("div");
            sourcesContainer.className = "sources";
            renderSources(sourcesContainer, sources);
            el.appendChild(sourcesContainer);
          }
        } catch {}
      }

      const contentEl = document.createElement("div");
      contentEl.className = "admin-msg-content";
      if (role === "assistant") {
        contentEl.innerHTML = marked.parse(msg.content || "");
      } else {
        contentEl.textContent = msg.content || "";
      }
      el.appendChild(contentEl);

      const footerEl = document.createElement("div");
      footerEl.className = "admin-msg-footer";
      footerEl.innerHTML = `<span class="admin-msg-time">${time}</span>`;
      if (role === "assistant" && (msg.input_tokens || msg.output_tokens)) {
        const msgIn = msg.input_tokens || 0;
        const msgOut = msg.output_tokens || 0;
        const msgThink = msg.thinking_tokens || 0;
        const msgModel = msg.model || null;
        const msgCost = estimateCost(msgIn, msgOut, msgThink, msgModel);
        const modelLabel = msgModel ? ` [${msgModel}]` : "";
        footerEl.innerHTML += `
          <span class="api-usage-chip small">IN ${msgIn.toLocaleString()} / OUT ${msgOut.toLocaleString()} / THK ${msgThink.toLocaleString()}${modelLabel}</span>
          <span class="api-cost-chip small">${msgCost}</span>`;
      }
      el.appendChild(footerEl);

      convMessages.appendChild(el);
    }
  } catch {
    convMessages.innerHTML = `<div class="conv-empty">불러오기 실패</div>`;
  }
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

// ---------------------------------------------------------------------------
// Models tab
// ---------------------------------------------------------------------------
async function loadModels() {
  try {
    const res = await fetch("/api/admin/models");
    const data = await res.json();
    allModels = data.models || [];
  } catch {
    allModels = [];
  }
  renderModels();
}

function renderModels() {
  const grid = document.getElementById("models-grid");
  if (!grid) return;

  grid.innerHTML = allModels.map((m) => {
    const providerLabel = m.provider === "gemini" ? "Google Gemini" : "Anthropic";
    const providerStatus = !m.provider_available
      ? `<span class="model-provider-status disconnected">API 키 없음</span>`
      : `<span class="model-provider-status connected">API 키 설정됨</span>`;
    const resolvedModel = m.resolved_model
      ? `<span class="model-card-resolved">→ ${escapeHtml(m.resolved_model)}</span>`
      : "";
    const disabled = !m.provider_available ? "disabled" : "";
    const checked = m.admin_enabled ? "checked" : "";
    const roleBadge = m.role === "primary"
      ? `<span class="model-default-badge">기본</span>`
      : `<span class="model-fallback-badge">폴백</span>`;
    return `<div class="model-card${m.available ? "" : " unavailable"}" data-model-id="${m.id}">
      <div class="model-card-body">
        <div class="model-card-header">
          <span class="model-card-label">${escapeHtml(m.label)}</span>
          ${roleBadge}
        </div>
        <div class="model-card-provider">
          <span class="model-card-provider-name">${providerLabel}</span>
          ${providerStatus}
          ${resolvedModel}
        </div>
        <div class="model-card-toggle">
          <label class="toggle-switch">
            <input type="checkbox" ${checked} ${disabled} onchange="toggleModel('${m.id}', this.checked)">
            <span class="toggle-slider"></span>
          </label>
          <span class="toggle-label">${m.admin_enabled ? "활성" : "비활성"}</span>
        </div>
      </div>
    </div>`;
  }).join("");

}

window.toggleModel = async function (modelKey, enabled) {
  const m = allModels.find((m) => m.id === modelKey);
  try {
    const res = await fetch(`/api/admin/models/${modelKey}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (res.ok) {
      if (m) {
        m.admin_enabled = enabled;
        m.available = m.provider_available && enabled;
      }
      renderModels();
    } else {
      const err = await res.json();
      alert(err.error || "변경에 실패했습니다");
      loadModels();
    }
  } catch {
    alert("변경에 실패했습니다");
    loadModels();
  }
};

// ---------------------------------------------------------------------------
// Payments tab
// ---------------------------------------------------------------------------
const BUSINESS_FIELDS = [
  { key: "biz_name", id: "setting-biz-name" },
  { key: "biz_owner", id: "setting-biz-owner" },
  { key: "biz_reg_no", id: "setting-biz-reg-no" },
  { key: "biz_mail_order_no", id: "setting-biz-mail-order-no" },
  { key: "biz_address", id: "setting-biz-address" },
  { key: "biz_tel", id: "setting-biz-tel" },
  { key: "biz_email", id: "setting-biz-email" },
];

const PAYMENT_STATUS_LABEL = {
  pending: "진행 중",
  paid: "완료",
  failed: "실패",
  cancelled: "취소",
  expired: "미완료",
};

function collectBusinessFields() {
  const business = {};
  BUSINESS_FIELDS.forEach(({ key, id }) => {
    business[key] = document.getElementById(id)?.value?.trim() || "";
  });
  return business;
}

// 카드 최소 승인금액 때문에 단가가 낮으면 최소 구매 수량이 올라간다. 관리자가
// 단가만 보고 "1장부터 팔린다"고 오해하지 않도록 계산해서 같이 보여준다.
function renderUnitPriceHint() {
  const hint = document.getElementById("unit-price-hint");
  const price = parseInt(document.getElementById("setting-credit-unit-price")?.value, 10);
  if (!hint) return;
  if (isNaN(price) || price < 1) {
    hint.textContent = "이용권 1장의 판매 가격(원)";
    return;
  }
  const minQuantity = Math.max(1, Math.ceil(1000 / price));
  hint.textContent =
    `이용권 1장 ${price.toLocaleString("ko-KR")}원 · 카드 최소 승인금액 1,000원이라 ${minQuantity}장부터 구매 가능`;
}

async function loadPayments() {
  const tbody = document.getElementById("payments-tbody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="7" class="conv-empty">불러오는 중...</td></tr>`;

  let payments = [];
  try {
    const res = await fetch("/api/admin/payments");
    const data = await res.json();
    payments = data.payments || [];
  } catch {
    tbody.innerHTML = `<tr><td colspan="7" class="conv-empty">불러오지 못했습니다</td></tr>`;
    return;
  }

  if (!payments.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="conv-empty">결제 내역이 없습니다</td></tr>`;
    return;
  }

  tbody.innerHTML = payments.map((payment) => {
    const when = formatLocal(payment.approved_at || payment.cancelled_at || payment.created_at);
    const status = PAYMENT_STATUS_LABEL[payment.status] || payment.status;
    const note = payment.status === "cancelled" && payment.reclaimed !== null
      ? ` (${payment.reclaimed}장 회수)`
      : payment.status === "failed" && payment.fail_reason
        ? ` · ${payment.fail_reason}`
        : "";
    const action = payment.status === "paid"
      ? `<button class="payment-cancel-btn" data-order="${escapeAttr(payment.order_id)}">취소</button>`
      : "";
    return `<tr>
      <td>${escapeHtml(payment.user_email || "탈퇴한 사용자")}</td>
      <td>${escapeHtml(payment.goods_name)}</td>
      <td>${Number(payment.amount).toLocaleString("ko-KR")}원</td>
      <td>${escapeHtml(status)}${escapeHtml(note)}</td>
      <td>${escapeHtml(when)}</td>
      <td class="payment-order-cell">${escapeHtml(payment.order_id)}</td>
      <td>${action}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll(".payment-cancel-btn").forEach((button) => {
    button.addEventListener("click", () => cancelPayment(button));
  });
}

async function cancelPayment(button) {
  const orderId = button.dataset.order;
  const reason = prompt("취소 사유를 입력하세요 (100자 이내)", "관리자 취소");
  if (reason === null) return;
  if (!reason.trim()) return;
  if (!confirm("결제를 전액 취소하고 지급된 이용권을 회수합니다. 계속하시겠습니까?")) return;

  button.disabled = true;
  try {
    const res = await fetch(`/api/admin/payments/${encodeURIComponent(orderId)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason.trim().slice(0, 100) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "취소에 실패했습니다");
    alert(`취소되었습니다. 회수한 이용권 ${data.reclaimed ?? 0}장`);
    loadPayments();
    loadUsers();
  } catch (cause) {
    alert(cause.message);
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Settings tab
// ---------------------------------------------------------------------------
async function loadSettings() {
  try {
    const res = await fetch("/api/admin/settings");
    const data = await res.json();
    const settings = data.settings || {};
    const input = document.getElementById("setting-default-credits");
    if (input && settings.default_credits !== undefined) {
      input.value = settings.default_credits;
    }
    const monthlyRefillInput = document.getElementById("setting-monthly-refill-credits");
    if (monthlyRefillInput && settings.monthly_refill_credits !== undefined) {
      monthlyRefillInput.value = settings.monthly_refill_credits;
    }
    const thresholdInput = document.getElementById("setting-low-credit-threshold");
    if (thresholdInput && settings.low_credit_threshold !== undefined) {
      thresholdInput.value = settings.low_credit_threshold;
      lowCreditThreshold = parseInt(settings.low_credit_threshold, 10) || 5;
    }
    const unlimitedCheckbox = document.getElementById("setting-unlimited-credits");
    const unlimitedLabel = document.getElementById("unlimited-label");
    if (unlimitedCheckbox) {
      const on = settings.unlimited_credits === "true" || settings.unlimited_credits === "1";
      unlimitedCheckbox.checked = on;
      if (unlimitedLabel) unlimitedLabel.textContent = on ? "활성" : "비활성";
    }
    const unitPriceInput = document.getElementById("setting-credit-unit-price");
    if (unitPriceInput && settings.credit_unit_price !== undefined) {
      unitPriceInput.value = settings.credit_unit_price;
    }
    const maxQuantityInput = document.getElementById("setting-credit-max-quantity");
    if (maxQuantityInput && settings.credit_max_quantity !== undefined) {
      maxQuantityInput.value = settings.credit_max_quantity;
    }
    BUSINESS_FIELDS.forEach(({ key, id }) => {
      const field = document.getElementById(id);
      if (field && settings[key] !== undefined) field.value = settings[key];
    });
    renderUnitPriceHint();
  } catch {
    // ignore
  }
}

let _settingsSaveTimer = null;

function autoSaveSettings() {
  clearTimeout(_settingsSaveTimer);
  _settingsSaveTimer = setTimeout(doSaveSettings, 500);
}

async function doSaveSettings() {
  const input = document.getElementById("setting-default-credits");
  const monthlyRefillInput = document.getElementById("setting-monthly-refill-credits");
  const thresholdInput = document.getElementById("setting-low-credit-threshold");
  const unlimitedCheckbox = document.getElementById("setting-unlimited-credits");
  if (!input || !monthlyRefillInput || !thresholdInput) return false;

  const defaultCredits = parseInt(input.value, 10);
  const monthlyRefillCredits = parseInt(monthlyRefillInput.value, 10);
  const threshold = parseInt(thresholdInput.value, 10);
  if (isNaN(defaultCredits) || defaultCredits < 0 ||
      isNaN(monthlyRefillCredits) || monthlyRefillCredits < 0 ||
      isNaN(threshold) || threshold < 0) return false;

  const unitPriceInput = document.getElementById("setting-credit-unit-price");
  const maxQuantityInput = document.getElementById("setting-credit-max-quantity");
  const unitPrice = parseInt(unitPriceInput?.value, 10);
  const maxQuantity = parseInt(maxQuantityInput?.value, 10);
  if (isNaN(unitPrice) || unitPrice < 1 || isNaN(maxQuantity) || maxQuantity < 1) return false;

  try {
    const res = await fetch("/api/admin/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        default_credits: defaultCredits,
        monthly_refill_credits: monthlyRefillCredits,
        low_credit_threshold: threshold,
        unlimited_credits: unlimitedCheckbox ? unlimitedCheckbox.checked : false,
        credit_unit_price: unitPrice,
        credit_max_quantity: maxQuantity,
        business: collectBusinessFields(),
      }),
    });
    if (res.ok) {
      const data = await res.json();
      lowCreditThreshold = parseInt(data.settings.low_credit_threshold, 10) || 5;
      renderUsers(userSearch.value);
      renderUnitPriceHint();
      return true;
    }
  } catch {
    // silently ignore — value will be retried on next change
  }
  return false;
}

document.getElementById("setting-default-credits").addEventListener("change", autoSaveSettings);
document.getElementById("setting-monthly-refill-credits").addEventListener("change", autoSaveSettings);
document.getElementById("setting-low-credit-threshold").addEventListener("change", autoSaveSettings);
document.getElementById("setting-credit-unit-price").addEventListener("change", autoSaveSettings);
document.getElementById("setting-credit-max-quantity").addEventListener("change", autoSaveSettings);
BUSINESS_FIELDS.forEach(({ id }) => {
  document.getElementById(id)?.addEventListener("change", autoSaveSettings);
});

document.getElementById("setting-unlimited-credits").addEventListener("change", () => {
  const label = document.getElementById("unlimited-label");
  if (label) label.textContent = document.getElementById("setting-unlimited-credits").checked ? "활성" : "비활성";
  autoSaveSettings();
});

document.getElementById("monthly-refill-btn").addEventListener("click", async () => {
  const input = document.getElementById("setting-monthly-refill-credits");
  const credits = parseInt(input.value, 10);
  if (isNaN(credits) || credits < 0) return;
  if (!confirm(`이용권이 ${credits}개 미만인 모든 사용자를 ${credits}개까지 즉시 충전합니다. 계속하시겠습니까?`)) return;

  clearTimeout(_settingsSaveTimer);
  if (!await doSaveSettings()) {
    alert("설정 저장에 실패했습니다");
    return;
  }

  try {
    const res = await fetch("/api/admin/credits/monthly-refill", { method: "POST" });
    if (res.ok) {
      const data = await res.json();
      alert(`${data.affected_users}명에게 총 ${data.total_credits}개의 이용권을 충전했습니다.`);
      loadUsers();
      loadAdminOverview();
    } else {
      const err = await res.json();
      alert(err.error || "즉시 충전에 실패했습니다");
    }
  } catch {
    alert("즉시 충전에 실패했습니다");
  }
});

document.getElementById("bulk-credit-btn").addEventListener("click", async () => {
  const input = document.getElementById("bulk-credit-value");
  const credits = parseInt(input.value, 10);
  if (isNaN(credits) || credits < 0) return;
  if (!confirm(`모든 사용자의 이용권을 ${credits}(으)로 일괄 변경합니다. 계속하시겠습니까?`)) return;
  try {
    const res = await fetch("/api/admin/credits/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credits }),
    });
    if (res.ok) {
      const data = await res.json();
      alert(`${data.affected}명의 이용권이 변경되었습니다.`);
      loadUsers();
      loadAdminOverview();
    } else {
      const err = await res.json();
      alert(err.error || "일괄 변경에 실패했습니다");
    }
  } catch {
    alert("일괄 변경에 실패했습니다");
  }
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
// Per-model pricing (per 1M tokens)
const MODEL_PRICING = {
  "gemini-3-flash":    { input: 1.50, output: 7.50,  thinking: 7.50 },
  "gemini-3-pro":      { input: 2.50, output: 15.00, thinking: 15.00 },
  "claude-sonnet-4.6": { input: 3.00, output: 15.00, thinking: 15.00 },
  "claude-opus-4.6":   { input: 5.00, output: 25.00, thinking: 25.00 },
};

// Default pricing (Gemini Flash) for messages without model info
const DEFAULT_PRICING = MODEL_PRICING["gemini-3-flash"];

function estimateCost(inputTokens, outputTokens, thinkingTokens = 0, model = null) {
  const p = (model && MODEL_PRICING[model]) || DEFAULT_PRICING;
  const cost = (inputTokens * p.input + outputTokens * p.output + thinkingTokens * p.thinking) / 1_000_000;
  if (cost < 0.01) return "$" + cost.toFixed(4);
  return "$" + cost.toFixed(2);
}

function estimateModelCost(modelUsage) {
  let total = 0;
  for (const u of modelUsage) {
    const p = (u.model && MODEL_PRICING[u.model]) || DEFAULT_PRICING;
    total += (u.input_tokens * p.input + u.output_tokens * p.output + u.thinking_tokens * p.thinking) / 1_000_000;
  }
  if (total < 0.01) return "$" + total.toFixed(4);
  return "$" + total.toFixed(2);
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

function formatRelative(utcStr) {
  const d = new Date(utcStr + (utcStr.endsWith("Z") ? "" : "Z"));
  const now = new Date();
  const diff = now - d;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return "방금 전";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}분 전`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}시간 전`;
  const days = Math.floor(hr / 24);
  if (days < 30) return `${days}일 전`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}개월 전`;
  return `${Math.floor(months / 12)}년 전`;
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
checkAdmin().then((ok) => {
  if (ok) {
    loadUsers();
    loadAdminOverview();
    loadModels();
    loadSettings();
  }
});
