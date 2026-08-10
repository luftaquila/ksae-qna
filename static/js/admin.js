import { jsonOptions, requestJSON } from "/static/js/api.js";
import { initTheme } from "/static/js/theme.js";
import {
  closeOnBackdrop,
  escapeAttr,
  escapeHtml,
  formatLocal,
  formatRelative,
  openDialog,
  renderEvidence,
  renderMarkdown,
  showToast,
} from "/static/js/ui.js";

const refs = {
  theme: document.getElementById("theme-toggle"),
  tabs: [...document.querySelectorAll(".admin-tab")],
  panels: [...document.querySelectorAll(".admin-panel")],
  usersBody: document.getElementById("users-tbody"),
  userSearch: document.getElementById("user-search"),
  usageSummary: document.getElementById("usage-summary"),
  convUser: document.getElementById("conv-user-select"),
  convSessionList: document.getElementById("conv-session-list"),
  convSessionCount: document.getElementById("conv-session-count"),
  convMessages: document.getElementById("conv-messages"),
  models: document.getElementById("models-grid"),
  modelOrderStatus: document.getElementById("model-order-status"),
  settingsForm: document.getElementById("settings-form"),
  settingsStatus: document.getElementById("settings-status"),
  unlimited: document.getElementById("setting-unlimited-credits"),
  unlimitedLabel: document.getElementById("unlimited-label"),
  defaultCredits: document.getElementById("setting-default-credits"),
  lowThreshold: document.getElementById("setting-low-credit-threshold"),
  bulkValue: document.getElementById("bulk-credit-value"),
  bulkButton: document.getElementById("bulk-credit-btn"),
  creditDialog: document.getElementById("credit-dialog"),
  creditForm: document.getElementById("credit-form"),
  creditUser: document.getElementById("credit-user"),
  creditValue: document.getElementById("credit-value"),
  creditSave: document.getElementById("credit-save"),
  detailDialog: document.getElementById("detail-dialog"),
  detailLabel: document.getElementById("detail-dialog-label"),
  detailTitle: document.getElementById("detail-dialog-title"),
  detailBody: document.getElementById("detail-dialog-body"),
  bulkDialog: document.getElementById("bulk-dialog"),
  bulkCopy: document.getElementById("bulk-dialog-copy"),
  bulkConfirm: document.getElementById("bulk-confirm"),
};

const state = {
  users: [],
  models: [],
  sortColumn: null,
  sortDirection: "asc",
  lowCreditThreshold: 5,
  conversationLoaded: false,
  currentConversationId: null,
  dragModelId: null,
  settingsTimer: null,
};

const fallbackPricing = {
  "gemini-3-flash": { input: 0.5, output: 3, thinking: 3 },
  "gemini-3-pro": { input: 2.5, output: 15, thinking: 15 },
  "claude-sonnet-4.6": { input: 3, output: 15, thinking: 15 },
  "claude-opus-4.6": { input: 5, output: 25, thinking: 25 },
};

function formatCost(value) {
  return `$${value < 0.01 ? value.toFixed(4) : value.toFixed(2)}`;
}

function usageCost(usage) {
  const pricing = fallbackPricing[usage.model] || fallbackPricing["gemini-3-flash"];
  return (
    Number(usage.input_tokens || 0) * pricing.input
    + Number(usage.output_tokens || 0) * pricing.output
    + Number(usage.thinking_tokens || 0) * pricing.thinking
  ) / 1_000_000;
}

function userCost(modelUsage = []) {
  return modelUsage.reduce((sum, usage) => sum + usageCost(usage), 0);
}

async function checkAdmin() {
  try {
    await requestJSON("/api/admin/check");
    return true;
  } catch {
    window.location.assign("/");
    return false;
  }
}

function activateTab(tab, focus = false) {
  for (const candidate of refs.tabs) {
    const selected = candidate === tab;
    candidate.setAttribute("aria-selected", String(selected));
    candidate.tabIndex = selected ? 0 : -1;
  }
  for (const panel of refs.panels) panel.hidden = panel.id !== `tab-${tab.dataset.tab}`;
  if (focus) tab.focus();
  if (tab.dataset.tab === "conversations" && !state.conversationLoaded) {
    state.conversationLoaded = true;
    loadConversationSessions("");
  }
}

function handleTabKeys(event) {
  const current = refs.tabs.indexOf(event.currentTarget);
  let next = null;
  if (["ArrowDown", "ArrowRight"].includes(event.key)) next = (current + 1) % refs.tabs.length;
  if (["ArrowUp", "ArrowLeft"].includes(event.key)) next = (current - 1 + refs.tabs.length) % refs.tabs.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = refs.tabs.length - 1;
  if (next === null) return;
  event.preventDefault();
  activateTab(refs.tabs[next], true);
}

refs.tabs.forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab));
  tab.addEventListener("keydown", handleTabKeys);
});

async function loadUsers() {
  try {
    const data = await requestJSON("/api/admin/users");
    state.users = data.users || [];
    renderUsers();
    renderUsageSummary();
    populateUserFilter();
  } catch (error) {
    state.users = [];
    refs.usersBody.innerHTML = `<tr><td colspan="6"><div class="workbench-empty"><strong>사용자를 불러오지 못했습니다.</strong><span>${escapeHtml(error.message)}</span></div></td></tr>`;
    refs.usageSummary.innerHTML = "";
  }
}

function filteredUsers() {
  const query = refs.userSearch.value.trim().toLocaleLowerCase("ko");
  let users = query
    ? state.users.filter((user) => String(user.name || "").toLocaleLowerCase("ko").includes(query) || String(user.email || "").toLocaleLowerCase("ko").includes(query))
    : [...state.users];
  if (!state.sortColumn) return users;

  const values = {
    name: (user) => String(user.name || "").toLocaleLowerCase("ko"),
    email: (user) => String(user.email || "").toLocaleLowerCase("ko"),
    credits: (user) => Number(user.credits || 0),
    tokens: (user) => Number(user.total_input_tokens || 0) + Number(user.total_output_tokens || 0) + Number(user.total_thinking_tokens || 0),
    last_active: (user) => user.last_active_at || "",
    created: (user) => user.created_at || "",
  };
  const getter = values[state.sortColumn];
  users.sort((a, b) => {
    const first = getter(a);
    const second = getter(b);
    return typeof first === "number" ? first - second : first.localeCompare(second);
  });
  if (state.sortDirection === "desc") users.reverse();
  return users;
}

function updateSortUI() {
  document.querySelectorAll(".sort-button").forEach((button) => {
    const active = button.dataset.sort === state.sortColumn;
    button.dataset.active = String(active);
    button.querySelector(".sort-icon").textContent = active ? (state.sortDirection === "asc" ? "↑" : "↓") : "";
    const th = button.closest("th");
    if (active) th.setAttribute("aria-sort", state.sortDirection === "asc" ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
  });
}

function renderUsers() {
  const users = filteredUsers();
  updateSortUI();
  if (!users.length) {
    refs.usersBody.innerHTML = '<tr><td colspan="6"><div class="workbench-empty"><strong>조건에 맞는 사용자가 없습니다.</strong><span>검색어를 지우거나 다른 이름을 입력하세요.</span></div></td></tr>';
    return;
  }

  refs.usersBody.innerHTML = users.map((user) => {
    const input = Number(user.total_input_tokens || 0);
    const output = Number(user.total_output_tokens || 0);
    const thinking = Number(user.total_thinking_tokens || 0);
    const picture = user.picture
      ? `<img class="user-picture" src="${escapeAttr(user.picture)}" alt="" width="32" height="32" referrerpolicy="no-referrer">`
      : "";
    return `<tr data-user-id="${user.id}">
      <td data-label="사용자"><div class="user-primary">${picture}<span class="user-name">${escapeHtml(user.name || "이름 없음")}</span></div></td>
      <td data-label="이메일">${escapeHtml(user.email || "—")}</td>
      <td data-label="이용권"><div class="cell-actions">
        <button class="data-button" type="button" data-action="transactions" data-user-id="${user.id}" data-low="${Number(user.credits) <= state.lowCreditThreshold}">${Number(user.credits || 0).toLocaleString()} CR</button>
        <button class="credit-adjust" type="button" data-action="credit" data-user-id="${user.id}">조정</button>
      </div></td>
      <td data-label="API 토큰"><div class="token-usage">
        <button class="data-button" type="button" data-action="usage" data-user-id="${user.id}">IN ${input.toLocaleString()} / OUT ${output.toLocaleString()} / THK ${thinking.toLocaleString()}</button>
        <span class="token-cost">${formatCost(userCost(user.model_usage || []))}</span>
      </div></td>
      <td data-label="최근 사용" title="${escapeAttr(formatLocal(user.last_active_at))}">${escapeHtml(formatRelative(user.last_active_at))}</td>
      <td data-label="가입일">${escapeHtml(formatLocal(user.created_at))}</td>
    </tr>`;
  }).join("");
}

function renderUsageSummary() {
  const models = new Map();
  for (const user of state.users) {
    for (const usage of user.model_usage || []) {
      const key = usage.model || "미기록 모델";
      if (!models.has(key)) models.set(key, { model: usage.model, input_tokens: 0, output_tokens: 0, thinking_tokens: 0, message_count: 0 });
      const aggregate = models.get(key);
      aggregate.input_tokens += Number(usage.input_tokens || 0);
      aggregate.output_tokens += Number(usage.output_tokens || 0);
      aggregate.thinking_tokens += Number(usage.thinking_tokens || 0);
      aggregate.message_count += Number(usage.message_count || 0);
    }
  }
  if (!models.size) {
    refs.usageSummary.innerHTML = '<div class="workbench-empty"><strong>아직 모델 사용량이 없습니다.</strong><span>답변이 생성되면 여기에 누적됩니다.</span></div>';
    return;
  }

  let total = 0;
  const rows = [...models.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, usage]) => {
    const cost = usageCost(usage);
    total += cost;
    return `<div class="telemetry-model">
      <span class="telemetry-model__name">${escapeHtml(name)}</span>
      <span class="telemetry-model__numbers"><span>IN ${usage.input_tokens.toLocaleString()}</span><span>OUT ${usage.output_tokens.toLocaleString()}</span><span>THK ${usage.thinking_tokens.toLocaleString()}</span><span>${usage.message_count.toLocaleString()}회</span></span>
      <span class="telemetry-model__cost">${formatCost(cost)}</span>
    </div>`;
  }).join("");
  refs.usageSummary.innerHTML = `${rows}<div class="telemetry-total"><strong>추정 총 비용</strong><span class="telemetry-total__cost">${formatCost(total)}</span></div>`;
}

document.querySelectorAll(".sort-button").forEach((button) => {
  button.addEventListener("click", () => {
    const column = button.dataset.sort;
    if (state.sortColumn === column) state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    else {
      state.sortColumn = column;
      state.sortDirection = ["credits", "tokens", "last_active", "created"].includes(column) ? "desc" : "asc";
    }
    renderUsers();
  });
});

refs.userSearch.addEventListener("input", renderUsers);
refs.usersBody.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const user = state.users.find((candidate) => candidate.id === Number(button.dataset.userId));
  if (!user) return;
  if (button.dataset.action === "credit") openCreditDialog(user);
  if (button.dataset.action === "transactions") openTransactions(user);
  if (button.dataset.action === "usage") openTokenUsage(user);
});

function openCreditDialog(user) {
  refs.creditDialog.dataset.userId = String(user.id);
  refs.creditUser.textContent = `${user.name || "이름 없음"} · ${user.email || "이메일 없음"}`;
  refs.creditValue.value = user.credits;
  refs.creditValue.setAttribute("aria-invalid", "false");
  openDialog(refs.creditDialog, refs.creditValue);
}

refs.creditForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = Number(refs.creditDialog.dataset.userId);
  const credits = Number.parseInt(refs.creditValue.value, 10);
  if (!Number.isInteger(credits) || credits < 0 || credits > 10000) {
    refs.creditValue.setAttribute("aria-invalid", "true");
    document.getElementById("credit-hint").textContent = "0–10,000 사이의 정수를 입력하세요.";
    refs.creditValue.focus();
    return;
  }

  refs.creditSave.disabled = true;
  refs.creditSave.dataset.state = "loading";
  refs.creditSave.textContent = "저장 중";
  try {
    const data = await requestJSON(`/api/admin/users/${userId}/credits`, jsonOptions("PATCH", { credits, memo: "관리자 조정" }));
    const user = state.users.find((candidate) => candidate.id === userId);
    if (user) user.credits = data.credits;
    refs.creditDialog.close();
    renderUsers();
  } catch (error) {
    showToast({ message: `이용권을 저장하지 못했습니다. ${error.message}`, tone: "error" });
  } finally {
    refs.creditSave.disabled = false;
    refs.creditSave.dataset.state = "default";
    refs.creditSave.textContent = "변경 저장";
  }
});

function prepareDetail(label, title) {
  refs.detailLabel.textContent = label;
  refs.detailTitle.textContent = title;
  refs.detailBody.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  openDialog(refs.detailDialog, refs.detailDialog.querySelector("[data-dialog-close]"));
}

async function openTransactions(user) {
  prepareDetail("Credit ledger", `${user.name || "사용자"} · 이용권 내역`);
  try {
    const data = await requestJSON(`/api/admin/users/${user.id}/transactions`);
    const transactions = data.transactions || [];
    refs.detailBody.innerHTML = transactions.length
      ? `<div class="detail-list">${transactions.map((transaction) => `<div class="detail-row">
          <div><div class="detail-row__title">${escapeHtml(transaction.memo || transaction.type)}</div><div class="detail-row__meta">${escapeHtml(formatLocal(transaction.created_at))}</div></div>
          <span class="detail-row__value">${Number(transaction.amount) > 0 ? "+" : ""}${Number(transaction.amount).toLocaleString()}</span>
        </div>`).join("")}</div>`
      : '<div class="workbench-empty"><strong>이용권 내역이 없습니다.</strong><span>변동이 생기면 여기에 기록됩니다.</span></div>';
  } catch (error) {
    refs.detailBody.innerHTML = `<div class="workbench-empty"><strong>내역을 불러오지 못했습니다.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

async function openTokenUsage(user) {
  prepareDetail("API telemetry", `${user.name || "사용자"} · 모델별 사용량`);
  try {
    const data = await requestJSON(`/api/admin/users/${user.id}/token-usage`);
    const usage = data.usage || [];
    refs.detailBody.innerHTML = usage.length
      ? `<div class="detail-list">${usage.map((item) => `<div class="detail-row">
          <div><div class="detail-row__title">${escapeHtml(item.model || "미기록 모델")}</div><div class="detail-row__meta">IN ${Number(item.input_tokens || 0).toLocaleString()} / OUT ${Number(item.output_tokens || 0).toLocaleString()} / THK ${Number(item.thinking_tokens || 0).toLocaleString()} · ${Number(item.message_count || 0).toLocaleString()}회</div></div>
          <span class="detail-row__value">${formatCost(usageCost(item))}</span>
        </div>`).join("")}</div>`
      : '<div class="workbench-empty"><strong>토큰 사용량이 없습니다.</strong><span>모델 응답이 기록되면 여기에 집계됩니다.</span></div>';
  } catch (error) {
    refs.detailBody.innerHTML = `<div class="workbench-empty"><strong>사용량을 불러오지 못했습니다.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function populateUserFilter() {
  const value = refs.convUser.value;
  refs.convUser.innerHTML = '<option value="">전체 사용자</option>' + state.users.map((user) => `<option value="${user.id}">${escapeHtml(user.name || "이름 없음")} · ${escapeHtml(user.email || "")}</option>`).join("");
  refs.convUser.value = value;
}

refs.convUser.addEventListener("change", () => loadConversationSessions(refs.convUser.value));

async function loadConversationSessions(userId) {
  refs.convSessionList.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  refs.convMessages.innerHTML = '<div class="workbench-empty"><strong>대화를 선택하세요.</strong><span>왼쪽 세션 목록에서 검토할 기록을 고르세요.</span></div>';
  state.currentConversationId = null;
  try {
    const url = userId ? `/api/admin/users/${userId}/sessions` : "/api/admin/sessions";
    const data = await requestJSON(url);
    renderConversationSessions(data.sessions || []);
  } catch (error) {
    refs.convSessionList.innerHTML = `<div class="workbench-empty"><strong>세션을 불러오지 못했습니다.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function renderConversationSessions(sessions) {
  refs.convSessionCount.textContent = sessions.length.toLocaleString();
  if (!sessions.length) {
    refs.convSessionList.innerHTML = '<div class="workbench-empty"><strong>대화 기록이 없습니다.</strong><span>선택한 사용자의 세션이 없습니다.</span></div>';
    return;
  }
  refs.convSessionList.innerHTML = sessions.map((session) => `<button class="conv-session-item" type="button" role="option" aria-selected="false" data-session-id="${session.id}" data-deleted="${Boolean(session.deleted_at)}">
    <span class="conv-session-title">${escapeHtml(session.title || "제목 없는 대화")}</span>
    <span class="conv-session-meta">${escapeHtml(session.user_name || "")} · ${escapeHtml(formatLocal(session.updated_at))} ${session.deleted_at ? '<span class="deleted-label">삭제됨</span>' : ""}</span>
  </button>`).join("");
}

refs.convSessionList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-session-id]");
  if (!button) return;
  refs.convSessionList.querySelectorAll("[aria-selected]").forEach((item) => item.setAttribute("aria-selected", String(item === button)));
  loadConversationMessages(Number(button.dataset.sessionId));
});

async function loadConversationMessages(sessionId) {
  state.currentConversationId = sessionId;
  refs.convMessages.innerHTML = '<div class="workbench-empty"><strong>메시지를 불러오는 중입니다.</strong><span>저장된 답변과 근거를 읽고 있습니다.</span></div>';
  try {
    const data = await requestJSON(`/api/admin/sessions/${sessionId}/messages`);
    const messages = data.messages || [];
    if (!messages.length) {
      refs.convMessages.innerHTML = '<div class="workbench-empty"><strong>메시지가 없습니다.</strong><span>이 세션에는 저장된 대화가 없습니다.</span></div>';
      return;
    }
    refs.convMessages.replaceChildren();
    for (const message of messages) appendAdminMessage(message);
  } catch (error) {
    refs.convMessages.innerHTML = `<div class="workbench-empty"><strong>메시지를 불러오지 못했습니다.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function appendAdminMessage(message) {
  const role = message.role === "user" ? "user" : "assistant";
  const article = document.createElement("article");
  article.className = "admin-message";
  article.dataset.role = role;

  const roleElement = document.createElement("div");
  roleElement.className = "admin-message__role";
  roleElement.textContent = role === "user" ? "YOU" : "PITBOT";

  const content = document.createElement("div");
  content.className = "admin-message__content";
  if (role === "assistant") content.innerHTML = renderMarkdown(message.content || "");
  else content.textContent = message.content || "";

  article.append(roleElement, content);
  if (role === "assistant" && message.sources) {
    try {
      const sources = JSON.parse(message.sources);
      if (sources.length) {
        const sourceContainer = document.createElement("div");
        sourceContainer.className = "sources";
        renderEvidence(sourceContainer, sources);
        article.appendChild(sourceContainer);
      }
    } catch { /* malformed legacy source data */ }
  }

  const footer = document.createElement("div");
  footer.className = "admin-message__footer";
  footer.innerHTML = `<span>${escapeHtml(formatLocal(message.created_at))}</span>`;
  if (role === "assistant" && (message.input_tokens || message.output_tokens || message.thinking_tokens)) {
    const item = {
      model: message.model,
      input_tokens: Number(message.input_tokens || 0),
      output_tokens: Number(message.output_tokens || 0),
      thinking_tokens: Number(message.thinking_tokens || 0),
    };
    footer.innerHTML += `<span>IN ${item.input_tokens.toLocaleString()} / OUT ${item.output_tokens.toLocaleString()} / THK ${item.thinking_tokens.toLocaleString()}</span><span>${escapeHtml(message.model || "미기록 모델")}</span><span>${formatCost(usageCost(item))}</span>`;
  }
  article.appendChild(footer);
  refs.convMessages.appendChild(article);
}

async function loadModels() {
  try {
    const data = await requestJSON("/api/admin/models");
    state.models = data.models || [];
    renderModels();
  } catch (error) {
    state.models = [];
    refs.models.innerHTML = `<div class="workbench-empty"><strong>모델을 불러오지 못했습니다.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

function providerState(model) {
  if (!model.provider_available) return { status: "disconnected", label: "API 키 없음" };
  return { status: "connected", label: "API 키 설정됨" };
}

function renderModels() {
  if (!state.models.length) {
    refs.models.innerHTML = '<div class="workbench-empty"><strong>등록된 모델이 없습니다.</strong><span>서버 모델 구성을 확인하세요.</span></div>';
    return;
  }
  refs.models.innerHTML = state.models.map((model, index) => {
    const provider = providerState(model);
    const customCredits = Number(model.credits) !== Number(model.default_credits);
    return `<article class="model-row" draggable="true" tabindex="0" data-model-id="${escapeAttr(model.id)}" aria-label="${escapeAttr(model.label)} 모델, ${index + 1}번째">
      <div class="model-identity">
        <button class="model-drag" type="button" data-action="drag" aria-label="${escapeAttr(model.label)} 순서 이동 핸들" title="드래그 또는 행에서 Alt+방향키">↕</button>
        <div><div class="model-name">${escapeHtml(model.label)} ${index === 0 ? '<span class="model-default">DEFAULT</span>' : ""}</div><div class="model-id">${escapeHtml(model.id)}</div></div>
      </div>
      <div class="provider-line"><span>${model.provider === "gemini" ? "Google Gemini" : "Anthropic"}</span><span class="provider-status" data-status="${provider.status}">${provider.label}</span>${model.resolved_model ? `<span class="model-resolved">→ ${escapeHtml(model.resolved_model)}</span>` : ""}</div>
      <div class="model-credit">
        <label class="field" for="model-credit-${escapeAttr(model.id)}"><span class="field__label">이용권</span><input id="model-credit-${escapeAttr(model.id)}" type="number" min="0" value="${Number(model.credits)}" data-action="credits" inputmode="numeric"></label>
        ${customCredits ? '<button class="text-button" type="button" data-action="reset">초기화</button>' : ""}
      </div>
      <label class="model-status-control"><input class="model-toggle" type="checkbox" data-action="toggle" ${model.admin_enabled ? "checked" : ""} ${!model.provider_available ? "disabled" : ""}><span>${model.admin_enabled ? "활성" : "비활성"}</span></label>
    </article>`;
  }).join("");
  bindModelDrag();
}

function bindModelDrag() {
  refs.models.querySelectorAll(".model-row").forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      state.dragModelId = row.dataset.modelId;
      row.dataset.dragging = "true";
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", row.dataset.modelId);
    });
    row.addEventListener("dragover", (event) => {
      event.preventDefault();
      if (state.dragModelId && state.dragModelId !== row.dataset.modelId) row.dataset.dragOver = "true";
    });
    row.addEventListener("dragleave", () => { row.dataset.dragOver = "false"; });
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      row.dataset.dragOver = "false";
      moveModel(state.dragModelId, row.dataset.modelId);
    });
    row.addEventListener("dragend", () => {
      state.dragModelId = null;
      refs.models.querySelectorAll(".model-row").forEach((candidate) => {
        candidate.dataset.dragging = "false";
        candidate.dataset.dragOver = "false";
      });
    });
    row.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const index = state.models.findIndex((model) => model.id === row.dataset.modelId);
      const targetIndex = event.key === "ArrowUp" ? index - 1 : index + 1;
      if (targetIndex < 0 || targetIndex >= state.models.length) return;
      moveModel(row.dataset.modelId, state.models[targetIndex].id, true);
    });
  });
}

async function moveModel(sourceId, targetId, focusAfter = false) {
  if (!sourceId || sourceId === targetId) return;
  const sourceIndex = state.models.findIndex((model) => model.id === sourceId);
  const targetIndex = state.models.findIndex((model) => model.id === targetId);
  if (sourceIndex < 0 || targetIndex < 0) return;
  const previous = [...state.models];
  const [moved] = state.models.splice(sourceIndex, 1);
  state.models.splice(targetIndex, 0, moved);
  renderModels();
  const newIndex = state.models.findIndex((model) => model.id === sourceId);
  refs.modelOrderStatus.textContent = `${moved.label} 모델을 ${newIndex + 1}번째로 이동했습니다.`;
  if (focusAfter) refs.models.querySelector(`[data-model-id="${CSS.escape(sourceId)}"]`)?.focus();
  try {
    await requestJSON("/api/admin/models/order", jsonOptions("PUT", { order: state.models.map((model) => model.id) }));
  } catch (error) {
    state.models = previous;
    renderModels();
    showToast({ message: `모델 순서를 저장하지 못했습니다. ${error.message}`, tone: "error" });
  }
}

refs.models.addEventListener("change", async (event) => {
  const row = event.target.closest(".model-row");
  const model = state.models.find((candidate) => candidate.id === row?.dataset.modelId);
  if (!model) return;
  if (event.target.dataset.action === "toggle") {
    const previous = model.admin_enabled;
    model.admin_enabled = event.target.checked;
    renderModels();
    try {
      await requestJSON(`/api/admin/models/${model.id}`, jsonOptions("PATCH", { enabled: model.admin_enabled, credits: Number(model.credits) === Number(model.default_credits) ? null : Number(model.credits) }));
    } catch (error) {
      model.admin_enabled = previous;
      renderModels();
      showToast({ message: `모델 상태를 저장하지 못했습니다. ${error.message}`, tone: "error" });
    }
  }
  if (event.target.dataset.action === "credits") {
    const credits = Number.parseInt(event.target.value, 10);
    if (!Number.isInteger(credits) || credits < 0) {
      event.target.setAttribute("aria-invalid", "true");
      return;
    }
    await updateModelCredits(model, credits);
  }
});

refs.models.addEventListener("click", async (event) => {
  const reset = event.target.closest('[data-action="reset"]');
  if (!reset) return;
  const row = reset.closest(".model-row");
  const model = state.models.find((candidate) => candidate.id === row.dataset.modelId);
  if (model) await updateModelCredits(model, null);
});

async function updateModelCredits(model, credits) {
  const previous = model.credits;
  model.credits = credits === null ? model.default_credits : credits;
  renderModels();
  try {
    const data = await requestJSON(`/api/admin/models/${model.id}`, jsonOptions("PATCH", { enabled: model.admin_enabled, credits }));
    model.credits = data.credits;
    renderModels();
  } catch (error) {
    model.credits = previous;
    renderModels();
    showToast({ message: `모델 이용권을 저장하지 못했습니다. ${error.message}`, tone: "error" });
  }
}

async function loadSettings() {
  try {
    const data = await requestJSON("/api/admin/settings");
    const settings = data.settings || {};
    refs.defaultCredits.value = settings.default_credits ?? 15;
    refs.lowThreshold.value = settings.low_credit_threshold ?? 5;
    state.lowCreditThreshold = Number(settings.low_credit_threshold ?? 5);
    refs.unlimited.checked = settings.unlimited_credits === "true" || settings.unlimited_credits === "1" || settings.unlimited_credits === true;
    refs.unlimitedLabel.textContent = refs.unlimited.checked ? "활성" : "비활성";
    setSettingsStatus("저장됨", "idle");
  } catch (error) {
    setSettingsStatus("불러오기 실패", "error");
    showToast({ message: `설정을 불러오지 못했습니다. ${error.message}`, tone: "error" });
  }
}

function setSettingsStatus(message, mode) {
  refs.settingsStatus.textContent = message;
  refs.settingsStatus.dataset.state = mode;
}

function scheduleSettingsSave() {
  window.clearTimeout(state.settingsTimer);
  setSettingsStatus("변경 대기", "saving");
  state.settingsTimer = window.setTimeout(saveSettings, 500);
}

async function saveSettings() {
  const defaultCredits = Number.parseInt(refs.defaultCredits.value, 10);
  const threshold = Number.parseInt(refs.lowThreshold.value, 10);
  if (![defaultCredits, threshold].every((value) => Number.isInteger(value) && value >= 0 && value <= 10000)) {
    setSettingsStatus("입력 확인 필요", "error");
    return;
  }
  setSettingsStatus("저장 중", "saving");
  try {
    const data = await requestJSON("/api/admin/settings", jsonOptions("PATCH", {
      default_credits: defaultCredits,
      low_credit_threshold: threshold,
      unlimited_credits: refs.unlimited.checked,
    }));
    state.lowCreditThreshold = Number(data.settings.low_credit_threshold ?? threshold);
    refs.unlimitedLabel.textContent = refs.unlimited.checked ? "활성" : "비활성";
    renderUsers();
    setSettingsStatus("저장됨", "idle");
  } catch (error) {
    setSettingsStatus("저장 실패", "error");
    showToast({ message: `설정을 저장하지 못했습니다. ${error.message}`, tone: "error" });
  }
}

[refs.defaultCredits, refs.lowThreshold, refs.unlimited].forEach((control) => control.addEventListener("change", () => {
  if (control === refs.unlimited) refs.unlimitedLabel.textContent = refs.unlimited.checked ? "활성" : "비활성";
  scheduleSettingsSave();
}));

refs.bulkButton.addEventListener("click", () => {
  const credits = Number.parseInt(refs.bulkValue.value, 10);
  if (!Number.isInteger(credits) || credits < 0 || credits > 10000) {
    refs.bulkValue.setAttribute("aria-invalid", "true");
    refs.bulkValue.focus();
    return;
  }
  refs.bulkValue.setAttribute("aria-invalid", "false");
  refs.bulkCopy.textContent = `${state.users.length.toLocaleString()}개 계정의 현재 이용권 잔액을 ${credits.toLocaleString()}으로 변경합니다. 이 작업은 각 계정의 이용권 내역에 기록됩니다.`;
  refs.bulkDialog.dataset.credits = String(credits);
  openDialog(refs.bulkDialog, refs.bulkDialog.querySelector(".button--secondary"));
});

refs.bulkConfirm.addEventListener("click", async () => {
  const credits = Number.parseInt(refs.bulkDialog.dataset.credits, 10);
  refs.bulkConfirm.disabled = true;
  refs.bulkConfirm.dataset.state = "loading";
  refs.bulkConfirm.textContent = "변경 중";
  try {
    await requestJSON("/api/admin/credits/bulk", jsonOptions("POST", { credits, memo: "관리자 일괄 조정" }));
    refs.bulkDialog.close();
    await loadUsers();
  } catch (error) {
    showToast({ message: `전체 이용권을 변경하지 못했습니다. ${error.message}`, tone: "error" });
  } finally {
    refs.bulkConfirm.disabled = false;
    refs.bulkConfirm.dataset.state = "default";
    refs.bulkConfirm.textContent = "전체 변경 실행";
  }
});

for (const dialog of [refs.creditDialog, refs.detailDialog, refs.bulkDialog]) {
  dialog.querySelectorAll("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
  closeOnBackdrop(dialog);
}

initTheme(refs.theme);
if (await checkAdmin()) {
  await Promise.all([loadUsers(), loadModels(), loadSettings()]);
}
