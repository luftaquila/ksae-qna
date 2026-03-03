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

// ---------------------------------------------------------------------------
// Theme (reused from script.js)
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
document.querySelectorAll(".admin-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".admin-tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`tab-${tab.dataset.tab}`).classList.add("active");
  });
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
  renderUsageSummary();
  populateUserFilter();
}

function renderUsers(filter = "") {
  const q = filter.toLowerCase();
  const filtered = q
    ? allUsers.filter((u) => u.name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q))
    : allUsers;

  usersTbody.innerHTML = filtered
    .map((u) => {
      const pic = u.picture
        ? `<img src="${escapeAttr(u.picture)}" class="user-picture" referrerpolicy="no-referrer">`
        : "";
      const date = u.created_at ? formatLocal(u.created_at) : "";
      const lowClass = u.credits <= lowCreditThreshold ? " low" : "";
      const totalIn = u.total_input_tokens || 0;
      const totalOut = u.total_output_tokens || 0;
      const totalThink = u.total_thinking_tokens || 0;
      const cost = estimateModelCost(u.model_usage || []);
      return `<tr data-user-id="${u.id}">
        <td>${pic}${escapeHtml(u.name)}</td>
        <td>${escapeHtml(u.email)}</td>
        <td>
          <div class="credit-cell" id="credit-cell-${u.id}">
            <div class="token-wrapper">
              <span class="credit-badge${lowClass}" onclick="toggleAdminPopover(${u.id})">${u.credits} 크레딧</span>
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
        <td>${date}</td>
      </tr>`;
    })
    .join("");
}

function renderUsageSummary() {
  const el = document.getElementById("usage-summary");
  if (!el) return;

  // Aggregate model_usage across all users
  const modelMap = {};
  for (const u of allUsers) {
    for (const mu of (u.model_usage || [])) {
      const key = mu.model || "(미기록)";
      if (!modelMap[key]) modelMap[key] = { input: 0, output: 0, thinking: 0, count: 0, model: mu.model };
      modelMap[key].input += mu.input_tokens;
      modelMap[key].output += mu.output_tokens;
      modelMap[key].thinking += mu.thinking_tokens;
      modelMap[key].count += mu.message_count;
    }
  }

  const models = Object.keys(modelMap).sort();
  if (!models.length) {
    el.innerHTML = "";
    return;
  }

  let totalCost = 0;
  const cards = models.map((key) => {
    const m = modelMap[key];
    const p = (m.model && MODEL_PRICING[m.model]) || DEFAULT_PRICING;
    const cost = (m.input * p.input + m.output * p.output + m.thinking * p.thinking) / 1_000_000;
    totalCost += cost;
    const costStr = cost < 0.01 ? "$" + cost.toFixed(4) : "$" + cost.toFixed(2);
    return `<div class="summary-card">
      <div class="summary-card-title">${escapeHtml(key)}</div>
      <div class="summary-card-stats">
        <span>IN ${m.input.toLocaleString()}</span>
        <span>OUT ${m.output.toLocaleString()}</span>
        <span>THK ${m.thinking.toLocaleString()}</span>
      </div>
      <div class="summary-card-footer">
        <span class="summary-card-count">${m.count.toLocaleString()}회</span>
        <span class="summary-card-cost">${costStr}</span>
      </div>
    </div>`;
  }).join("");

  const totalCostStr = totalCost < 0.01 ? "$" + totalCost.toFixed(4) : "$" + totalCost.toFixed(2);
  el.innerHTML = `
    <div class="summary-cards">${cards}</div>
    <div class="summary-total">
      <span>총 비용</span>
      <span class="summary-total-cost">${totalCostStr}</span>
    </div>
  `;
}

userSearch.addEventListener("input", () => {
  renderUsers(userSearch.value);
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
      <span class="credit-badge${lowClass}" onclick="toggleAdminPopover(${userId})">${currentCredits} 크레딧</span>
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
    <div class="token-popover-header">크레딧 사용 내역</div>
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

convUserSelect.addEventListener("change", () => {
  loadSessions(convUserSelect.value);
});

async function loadSessions(userId) {
  if (!userId) {
    convSessionList.innerHTML = "";
    convMessages.innerHTML = `<div class="conv-empty">사용자를 선택하세요</div>`;
    currentConvSessionId = null;
    return;
  }

  try {
    const res = await fetch(`/api/admin/users/${userId}/sessions`);
    const data = await res.json();
    const sessions = data.sessions || [];
    renderSessionList(sessions);
  } catch {
    convSessionList.innerHTML = "";
  }
}

function renderSessionList(sessions) {
  currentConvSessionId = null;
  convMessages.innerHTML = `<div class="conv-empty">세션을 선택하세요</div>`;

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

  grid.innerHTML = allModels.map((m, idx) => {
    const providerLabel = m.provider === "gemini" ? "Google Gemini" : "Anthropic";
    const providerStatus = m.provider_available
      ? `<span class="model-provider-status connected">연결됨</span>`
      : `<span class="model-provider-status disconnected">미연결</span>`;
    const disabled = !m.provider_available ? "disabled" : "";
    const checked = m.admin_enabled ? "checked" : "";
    const isCustom = m.credits !== m.default_credits;
    const resetBtn = isCustom
      ? `<button class="model-credits-reset" onclick="resetModelCredits('${m.id}')" title="기본값(${m.default_credits})으로 초기화">초기화</button>`
      : "";
    const defaultBadge = idx === 0 ? `<span class="model-default-badge">기본</span>` : "";
    return `<div class="model-card${m.available ? "" : " unavailable"}" draggable="true" data-model-id="${m.id}">
      <div class="model-drag-handle" title="드래그하여 순서 변경">⠿</div>
      <div class="model-card-body">
        <div class="model-card-header">
          <span class="model-card-label">${escapeHtml(m.label)}</span>
          ${defaultBadge}
        </div>
        <div class="model-card-provider">
          <span class="model-card-provider-name">${providerLabel}</span>
          ${providerStatus}
        </div>
        <div class="model-card-credits-row">
          <label class="model-credits-label">차감 크레딧</label>
          <input type="number" class="model-credits-input" min="0" value="${m.credits}"
            data-model="${m.id}" onchange="updateModelCredits('${m.id}', this.value)">
          ${resetBtn}
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

  initModelDragAndDrop();
}

// ---------------------------------------------------------------------------
// Model drag-and-drop reordering
// ---------------------------------------------------------------------------
let dragSrcEl = null;

function initModelDragAndDrop() {
  const grid = document.getElementById("models-grid");
  if (!grid) return;

  const cards = grid.querySelectorAll(".model-card");
  cards.forEach((card) => {
    card.addEventListener("dragstart", handleDragStart);
    card.addEventListener("dragover", handleDragOver);
    card.addEventListener("dragenter", handleDragEnter);
    card.addEventListener("dragleave", handleDragLeave);
    card.addEventListener("drop", handleDrop);
    card.addEventListener("dragend", handleDragEnd);
  });
}

function handleDragStart(e) {
  dragSrcEl = this;
  this.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", this.dataset.modelId);
}

function handleDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
}

function handleDragEnter(e) {
  e.preventDefault();
  const card = e.target.closest(".model-card");
  if (card && card !== dragSrcEl) {
    card.classList.add("drag-over");
  }
}

function handleDragLeave(e) {
  const card = e.target.closest(".model-card");
  if (card) {
    card.classList.remove("drag-over");
  }
}

function handleDrop(e) {
  e.preventDefault();
  const targetCard = e.target.closest(".model-card");
  if (!targetCard || targetCard === dragSrcEl) return;

  targetCard.classList.remove("drag-over");

  const grid = document.getElementById("models-grid");
  const cards = [...grid.querySelectorAll(".model-card")];
  const fromIdx = cards.indexOf(dragSrcEl);
  const toIdx = cards.indexOf(targetCard);

  // Reorder allModels array
  const [moved] = allModels.splice(fromIdx, 1);
  allModels.splice(toIdx, 0, moved);

  renderModels();
  saveModelOrder();
}

function handleDragEnd() {
  this.classList.remove("dragging");
  document.querySelectorAll(".model-card").forEach((c) => c.classList.remove("drag-over"));
}

async function saveModelOrder() {
  const order = allModels.map((m) => m.id);
  try {
    await fetch("/api/admin/models/order", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order }),
    });
  } catch {
    // Silently fail — order is already applied visually
  }
}

window.toggleModel = async function (modelKey, enabled) {
  const m = allModels.find((m) => m.id === modelKey);
  const credits = m && m.credits !== m.default_credits ? m.credits : null;
  try {
    const res = await fetch(`/api/admin/models/${modelKey}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, credits }),
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

window.updateModelCredits = async function (modelKey, value) {
  const credits = parseInt(value, 10);
  if (isNaN(credits) || credits < 0) return;
  const m = allModels.find((m) => m.id === modelKey);
  if (!m) return;
  try {
    const res = await fetch(`/api/admin/models/${modelKey}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: m.admin_enabled, credits }),
    });
    if (res.ok) {
      const data = await res.json();
      m.credits = data.credits;
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

window.resetModelCredits = async function (modelKey) {
  const m = allModels.find((m) => m.id === modelKey);
  if (!m) return;
  try {
    const res = await fetch(`/api/admin/models/${modelKey}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: m.admin_enabled, credits: null }),
    });
    if (res.ok) {
      m.credits = m.default_credits;
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
  const thresholdInput = document.getElementById("setting-low-credit-threshold");
  const unlimitedCheckbox = document.getElementById("setting-unlimited-credits");
  if (!input || !thresholdInput) return;

  const defaultCredits = parseInt(input.value, 10);
  const threshold = parseInt(thresholdInput.value, 10);
  if (isNaN(defaultCredits) || defaultCredits < 0 || isNaN(threshold) || threshold < 0) return;

  try {
    const res = await fetch("/api/admin/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        default_credits: defaultCredits,
        low_credit_threshold: threshold,
        unlimited_credits: unlimitedCheckbox ? unlimitedCheckbox.checked : false,
      }),
    });
    if (res.ok) {
      const data = await res.json();
      lowCreditThreshold = parseInt(data.settings.low_credit_threshold, 10) || 5;
      renderUsers(userSearch.value);
    }
  } catch {
    // silently ignore — value will be retried on next change
  }
}

document.getElementById("setting-default-credits").addEventListener("change", autoSaveSettings);
document.getElementById("setting-low-credit-threshold").addEventListener("change", autoSaveSettings);

document.getElementById("setting-unlimited-credits").addEventListener("change", () => {
  const label = document.getElementById("unlimited-label");
  if (label) label.textContent = document.getElementById("setting-unlimited-credits").checked ? "활성" : "비활성";
  autoSaveSettings();
});

document.getElementById("bulk-credit-btn").addEventListener("click", async () => {
  const input = document.getElementById("bulk-credit-value");
  const credits = parseInt(input.value, 10);
  if (isNaN(credits) || credits < 0) return;
  if (!confirm(`모든 사용자의 크레딧을 ${credits}(으)로 일괄 변경합니다. 계속하시겠습니까?`)) return;
  try {
    const res = await fetch("/api/admin/credits/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credits }),
    });
    if (res.ok) {
      const data = await res.json();
      alert(`${data.affected}명의 크레딧이 변경되었습니다.`);
      loadUsers();
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
  "gemini-3-flash":    { input: 0.50, output: 3.00,  thinking: 3.00 },
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
    loadModels();
    loadSettings();
  }
});
