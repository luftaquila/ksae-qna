import { HttpError, readSSE, requestJSON } from "/static/js/api.js";
import { initTheme } from "/static/js/theme.js";
import {
  closeOnBackdrop,
  escapeAttr,
  escapeHtml,
  formatLocal,
  openDialog,
  renderEvidence,
  renderMarkdown,
  showToast,
} from "/static/js/ui.js";

const refs = {
  chat: document.getElementById("chat"),
  chatScroll: document.querySelector(".chat-scroll"),
  form: document.getElementById("form"),
  query: document.getElementById("query"),
  queryCount: document.getElementById("query-count"),
  send: document.getElementById("send"),
  auth: document.getElementById("auth-area"),
  sessionList: document.getElementById("session-list"),
  newChat: document.getElementById("new-chat-btn"),
  theme: document.getElementById("theme-toggle"),
  sidebar: document.getElementById("sidebar"),
  sidebarToggle: document.getElementById("sidebar-toggle"),
  sidebarOverlay: document.getElementById("sidebar-overlay"),
  conversationStatus: document.getElementById("conversation-status"),
  composerStatus: document.getElementById("composer-status"),
  modelSelect: document.getElementById("model-select"),
  filterDialog: document.getElementById("filter-dialog"),
  filterOpen: document.getElementById("filter-open"),
  filterClose: document.getElementById("filter-close"),
  filterApply: document.getElementById("filter-apply"),
  filterSummary: document.getElementById("filter-summary"),
  collectionHost: document.getElementById("collection-chips"),
};

const state = {
  user: null,
  currentSessionId: null,
  sessions: [],
  models: [],
  collections: [],
  confidenceLevels: [],
  lowCreditThreshold: 5,
  unlimitedCredits: false,
  sidebarOpen: false,
  sending: false,
  pendingDeletes: new Map(),
};

const desktopRail = window.matchMedia("(min-width: 60rem)");
const categoryOptions = ["Formula", "Baja", "EV"];
const examplePrompts = [
  "Formula 차량의 최소 지상고 기준은?",
  "EV 차단 스위치 배치 요건을 설명해줘",
  "Baja 롤케이지 관련 현장 Q&A를 요약해줘",
];

function setSidebar(open) {
  state.sidebarOpen = desktopRail.matches ? false : open;
  refs.sidebar.dataset.open = String(state.sidebarOpen);
  refs.sidebarOverlay.dataset.open = String(state.sidebarOpen);
  refs.sidebar.setAttribute("aria-hidden", String(!desktopRail.matches && !state.sidebarOpen));
  refs.sidebarToggle.setAttribute("aria-expanded", String(state.sidebarOpen));
  refs.sidebarToggle.setAttribute("aria-label", state.sidebarOpen ? "대화 목록 닫기" : "대화 목록 열기");
  refs.sidebarOverlay.tabIndex = state.sidebarOpen ? 0 : -1;
  if (state.sidebarOpen) refs.newChat.focus({ preventScroll: true });
}

function syncSidebarMode() {
  setSidebar(false);
  refs.sidebar.setAttribute("aria-hidden", String(!desktopRail.matches));
}

function setConversationStatus(label, mode = "idle") {
  refs.conversationStatus.dataset.state = mode;
  refs.conversationStatus.querySelector("span:last-child").textContent = label;
}

function setSending(sending) {
  state.sending = sending;
  refs.chat.setAttribute("aria-busy", String(sending));
  refs.send.disabled = sending || !state.user;
  refs.query.disabled = sending || !state.user;
  refs.send.dataset.state = sending ? "loading" : "default";
  refs.send.querySelector(".send-label").textContent = sending ? "응답 생성 중" : "질문 보내기";
  refs.composerStatus.textContent = sending ? "검색과 답변 생성을 진행하고 있습니다" : "Enter 전송 · Shift+Enter 줄바꿈";
  setConversationStatus(sending ? "처리 중" : "대기", sending ? "busy" : "idle");
  if (!sending && state.user) refs.query.focus({ preventScroll: true });
}

function scrollToBottom() {
  window.requestAnimationFrame(() => {
    refs.chatScroll.scrollTop = refs.chatScroll.scrollHeight;
  });
}

function resizeQuery() {
  refs.query.style.height = "auto";
  const maximum = Number.parseFloat(getComputedStyle(refs.query).maxHeight) || 192;
  refs.query.style.height = `${Math.min(refs.query.scrollHeight, maximum)}px`;
  refs.queryCount.textContent = `${refs.query.value.length.toLocaleString()} / 2,000`;
}

function renderAuth() {
  refs.auth.replaceChildren();
  if (!state.user) {
    const login = document.createElement("a");
    login.className = "login-button";
    login.href = "/api/auth/login";
    login.textContent = "Google 로그인";
    refs.auth.appendChild(login);
    refs.query.disabled = true;
    refs.send.disabled = true;
    showWelcome();
    return;
  }

  const profile = document.createElement(state.user.is_admin ? "a" : "div");
  profile.className = "profile-summary";
  if (state.user.is_admin) {
    profile.href = "/admin";
    profile.setAttribute("aria-label", "관리자 작업대로 이동");
  }

  if (state.user.picture) {
    const picture = document.createElement("img");
    picture.className = "profile-picture";
    picture.src = state.user.picture;
    picture.alt = "";
    picture.referrerPolicy = "no-referrer";
    picture.width = 32;
    picture.height = 32;
    profile.appendChild(picture);
  }
  const name = document.createElement("span");
  name.className = "profile-name";
  name.textContent = state.user.name;
  profile.appendChild(name);

  const creditButton = document.createElement("button");
  creditButton.id = "credit-button";
  creditButton.className = "credit-button";
  creditButton.type = "button";
  creditButton.setAttribute("aria-haspopup", "dialog");
  creditButton.addEventListener("click", openCreditHistory);

  const logout = document.createElement("button");
  logout.className = "icon-button";
  logout.type = "button";
  logout.setAttribute("aria-label", "로그아웃");
  logout.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10 5H5v14h5M14 8l4 4-4 4M8 12h10"/></svg>';
  logout.addEventListener("click", handleLogout);

  refs.auth.append(profile, creditButton, logout);
  updateCreditDisplay(state.user.credits);
  setSending(false);
  showWelcome();
}

function updateCreditDisplay(credits) {
  if (state.user && Number.isInteger(credits)) state.user.credits = credits;
  const button = document.getElementById("credit-button");
  if (!button || !state.user) return;
  button.textContent = state.unlimitedCredits ? "∞ 이용권" : `${state.user.credits.toLocaleString()} 이용권`;
  button.dataset.low = String(!state.unlimitedCredits && state.user.credits <= state.lowCreditThreshold);
  button.setAttribute("aria-label", `${button.textContent}, 사용 내역 열기`);
}

async function checkAuth() {
  try {
    const data = await requestJSON("/api/me");
    state.user = data.user || null;
    state.lowCreditThreshold = Number(data.low_credit_threshold ?? 5);
    state.unlimitedCredits = Boolean(data.unlimited_credits);
  } catch {
    state.user = null;
  }
  renderAuth();
  if (state.user) await loadSessions();
}

async function handleLogout() {
  try {
    await requestJSON("/api/auth/logout", { method: "POST" });
  } catch {
    showToast({ message: "로그아웃 요청을 완료하지 못했습니다. 다시 시도해주세요.", tone: "error" });
    return;
  }
  state.user = null;
  state.currentSessionId = null;
  state.sessions = [];
  renderSessions();
  renderAuth();
  showWelcome();
}

function createCreditDialog() {
  let dialog = document.getElementById("credit-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "credit-dialog";
  dialog.setAttribute("aria-labelledby", "credit-dialog-title");
  dialog.innerHTML = `
    <div class="dialog__header">
      <div>
        <p class="rail-label">Credit ledger</p>
        <h2 id="credit-dialog-title" class="dialog__title">이용권 사용 내역</h2>
      </div>
      <button class="icon-button" type="button" data-dialog-close aria-label="이용권 내역 닫기">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
      </button>
    </div>
    <div class="dialog__body">
      <div class="credit-history" aria-live="polite"><div class="skeleton"></div><div class="skeleton"></div></div>
      <div class="field">
        <label class="field__label" for="credit-purchase-count">구매 수량</label>
        <input id="credit-purchase-count" type="number" min="1" max="1000" value="5" inputmode="numeric">
        <span class="field__hint">구매 기능은 아직 준비 중입니다.</span>
      </div>
    </div>
    <div class="dialog__footer">
      <button class="button button--secondary" type="button" data-dialog-close>닫기</button>
      <button class="button" type="button" data-purchase>구매 확인</button>
    </div>`;
  dialog.querySelectorAll("[data-dialog-close]").forEach((button) => button.addEventListener("click", () => dialog.close()));
  dialog.querySelector("[data-purchase]").addEventListener("click", () => {
    showToast({ message: "이용권 구매 기능은 준비 중입니다." });
  });
  closeOnBackdrop(dialog);
  document.body.appendChild(dialog);
  return dialog;
}

async function openCreditHistory() {
  const dialog = createCreditDialog();
  const history = dialog.querySelector(".credit-history");
  history.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  openDialog(dialog, dialog.querySelector("#credit-purchase-count"));
  try {
    const data = await requestJSON("/api/transactions");
    const transactions = data.transactions || [];
    if (!transactions.length) {
      history.innerHTML = '<p class="session-empty"><strong>사용 내역이 없습니다.</strong><span>질문을 보내면 이용권 변동이 여기에 기록됩니다.</span></p>';
      return;
    }
    history.innerHTML = transactions.map((transaction) => {
      const positive = Number(transaction.amount) > 0;
      return `<div class="credit-transaction">
        <div>
          <div class="credit-transaction__memo">${escapeHtml(transaction.memo || transaction.type)}</div>
          <div class="credit-transaction__date">${escapeHtml(formatLocal(transaction.created_at))}</div>
        </div>
        <span class="credit-transaction__amount" data-positive="${positive}">${positive ? "+" : ""}${Number(transaction.amount).toLocaleString()}</span>
      </div>`;
    }).join("");
  } catch (error) {
    history.innerHTML = `<p class="session-empty"><strong>내역을 불러오지 못했습니다.</strong><span>${escapeHtml(error.message)}</span></p>`;
  }
}

async function loadSessions() {
  try {
    const data = await requestJSON("/api/sessions");
    state.sessions = (data.sessions || []).filter((session) => !state.pendingDeletes.has(session.id));
  } catch {
    state.sessions = [];
  }
  renderSessions();
}

function renderSessions() {
  refs.sessionList.replaceChildren();
  if (!state.sessions.length) {
    refs.sessionList.innerHTML = '<p class="session-empty"><strong>저장된 대화가 없습니다.</strong><span>첫 질문을 보내면 여기에 기록됩니다.</span></p>';
    return;
  }

  for (const session of state.sessions) {
    const item = document.createElement("div");
    item.className = "session-item";
    item.dataset.active = String(session.id === state.currentSessionId);

    const select = document.createElement("button");
    select.className = "session-item__select";
    select.type = "button";
    select.textContent = session.title || "제목 없는 대화";
    select.setAttribute("aria-current", session.id === state.currentSessionId ? "page" : "false");
    select.addEventListener("click", () => switchSession(session.id));

    const remove = document.createElement("button");
    remove.className = "session-item__delete";
    remove.type = "button";
    remove.setAttribute("aria-label", `‘${session.title || "제목 없는 대화"}’ 삭제`);
    remove.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9 7V4h6v3M8 10v8M12 10v8M16 10v8M7 7l1 14h8l1-14"/></svg>';
    remove.addEventListener("click", () => scheduleSessionDelete(session));

    item.append(select, remove);
    refs.sessionList.appendChild(item);
  }
}

function scheduleSessionDelete(session) {
  if (state.pendingDeletes.has(session.id)) return;
  const index = state.sessions.findIndex((item) => item.id === session.id);
  state.sessions = state.sessions.filter((item) => item.id !== session.id);
  if (state.currentSessionId === session.id) startNewChat(false);
  renderSessions();

  const restore = () => {
    const pending = state.pendingDeletes.get(session.id);
    if (!pending) return;
    window.clearTimeout(pending.timer);
    state.pendingDeletes.delete(session.id);
    state.sessions.splice(Math.min(index, state.sessions.length), 0, session);
    renderSessions();
  };

  const commit = async () => {
    state.pendingDeletes.delete(session.id);
    try {
      await requestJSON(`/api/sessions/${session.id}`, { method: "DELETE" });
    } catch (error) {
      state.sessions.splice(Math.min(index, state.sessions.length), 0, session);
      renderSessions();
      showToast({ message: `대화를 삭제하지 못했습니다. ${error.message}`, tone: "error" });
    }
  };

  const timer = window.setTimeout(commit, 6500);
  state.pendingDeletes.set(session.id, { timer, restore });
  showToast({
    message: "대화를 삭제 대기열에 넣었습니다.",
    duration: 6000,
    action: { label: "되돌리기", run: restore },
  });
}

async function switchSession(id) {
  setSidebar(false);
  state.currentSessionId = id;
  renderSessions();
  refs.chat.replaceChildren();
  refs.chat.innerHTML = '<div class="log-entry" data-role="assistant"><div class="log-entry__role">PitBot</div><div class="log-entry__body"><span class="loading-state">대화 불러오는 중</span></div></div>';
  try {
    const data = await requestJSON(`/api/sessions/${id}/messages`);
    refs.chat.replaceChildren();
    for (const message of data.messages || []) {
      if (message.role === "user") {
        appendUserMessage(message.content || "");
      } else if (message.role === "assistant") {
        const shell = appendAssistantShell(false);
        if (message.sources) {
          try { renderEvidence(shell.sources, JSON.parse(message.sources)); } catch { /* malformed legacy source data */ }
        }
        shell.answer.innerHTML = renderMarkdown(message.content || "");
        if (message.model) shell.meta.textContent = `MODEL ${message.model}`;
      }
    }
    if (!refs.chat.querySelector(".log-entry")) showWelcome(true);
    scrollToBottom();
  } catch (error) {
    refs.chat.replaceChildren();
    showWelcome(true);
    showToast({ message: `대화를 불러오지 못했습니다. ${error.message}`, tone: "error" });
  }
}

function startNewChat(focus = true) {
  state.currentSessionId = null;
  renderSessions();
  refs.chat.replaceChildren();
  showWelcome(true);
  if (focus && state.user) refs.query.focus();
}

async function loadModels() {
  try {
    const data = await requestJSON("/api/models");
    state.models = data.models || [];
  } catch {
    state.models = [];
  }
  renderModels();
  showWelcome();
}

function renderModels() {
  const previous = refs.modelSelect.value || localStorage.getItem("selectedModel");
  refs.modelSelect.replaceChildren();
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = `${model.label} · ${model.credits} 이용권`;
    option.disabled = !model.available;
    refs.modelSelect.appendChild(option);
  }
  const available = state.models.filter((model) => model.available);
  if (!available.length) {
    const option = document.createElement("option");
    option.textContent = "사용 가능한 모델 없음";
    option.disabled = true;
    refs.modelSelect.appendChild(option);
    refs.modelSelect.disabled = true;
    return;
  }
  refs.modelSelect.disabled = false;
  refs.modelSelect.value = available.some((model) => model.id === previous) ? previous : available[0].id;
  localStorage.setItem("selectedModel", refs.modelSelect.value);
}

async function loadCollections() {
  try {
    const data = await requestJSON("/api/collections");
    state.collections = data.collections || [];
    state.confidenceLevels = data.confidence_levels || [];
  } catch {
    state.collections = [];
    state.confidenceLevels = [];
  }
  renderCollections();
  updateFilterSummary();
  showWelcome();
}

function createSelect(id, label, options) {
  const wrap = document.createElement("label");
  wrap.className = "collection-filter";
  wrap.innerHTML = `<span class="field__label">${escapeHtml(label)}</span>`;
  const select = document.createElement("select");
  select.id = id;
  for (const optionData of options) {
    const option = document.createElement("option");
    option.value = optionData.value;
    option.textContent = optionData.label;
    select.appendChild(option);
  }
  select.addEventListener("change", updateFilterSummary);
  wrap.appendChild(select);
  return wrap;
}

function renderCollections() {
  refs.collectionHost.replaceChildren();
  if (!state.collections.length) {
    refs.collectionHost.innerHTML = '<p class="session-empty"><strong>검색 소스를 불러오지 못했습니다.</strong><span>창을 닫고 잠시 후 다시 열어보세요.</span></p>';
    return;
  }

  for (const collection of state.collections) {
    const control = document.createElement("div");
    control.className = "collection-control";
    const label = document.createElement("label");
    label.className = "collection-checkbox";
    label.innerHTML = `
      <input type="checkbox" name="collections" value="${escapeAttr(collection.key)}" checked>
      <span>${escapeHtml(collection.label)}<span class="collection-description">${escapeHtml(collection.description || "")}</span></span>`;
    control.appendChild(label);

    let filter = null;
    if (collection.filter === "category") {
      filter = createSelect("category-select", "카테고리", [
        { value: "", label: "전체 카테고리" },
        ...categoryOptions.map((value) => ({ value, label: value })),
      ]);
    }
    if (collection.filter === "confidence") {
      filter = createSelect("confidence-select", "신뢰도", [
        { value: "합의됨,다수의견", label: "합의됨·다수의견 (기본)" },
        { value: "", label: "신뢰도 전체" },
        ...state.confidenceLevels.map((value) => ({ value, label: `${value}만` })),
      ]);
    }
    if (filter) {
      filter.dataset.collection = collection.key;
      control.appendChild(filter);
    }

    label.querySelector("input").addEventListener("change", (event) => {
      if (filter) filter.querySelector("select").disabled = !event.target.checked;
      updateFilterSummary();
    });
    refs.collectionHost.appendChild(control);
  }
}

function selectedCollections() {
  return [...document.querySelectorAll('input[name="collections"]:checked')].map((input) => input.value);
}

function selectedConfidence() {
  const select = document.getElementById("confidence-select");
  if (!select || select.disabled || !select.value) return null;
  return select.value.split(",");
}

function updateFilterSummary() {
  const selected = selectedCollections();
  const labels = state.collections.filter((collection) => selected.includes(collection.key)).map((collection) => collection.label);
  refs.filterSummary.textContent = labels.length ? `${labels.join(" · ")} · ${labels.length}개 소스` : "검색 소스 없음";
}

function showWelcome(force = false) {
  if (!force && refs.chat.querySelector(".log-entry")) return;
  const models = state.models.filter((model) => model.available);
  const modelRows = models.length
    ? models.map((model) => `<li><strong>${escapeHtml(model.label)}</strong><code>${Number(model.credits).toLocaleString()} CR</code></li>`).join("")
    : '<li><strong>사용 가능한 모델 없음</strong><code>—</code></li>';
  const sourceRows = state.collections.length
    ? state.collections.map((collection) => `<li><strong>${escapeHtml(collection.label)}</strong><code>${escapeHtml(collection.key)}</code></li>`).join("")
    : '<li><strong>검색 소스 확인 중</strong><code>—</code></li>';
  const promptRows = examplePrompts.map((prompt) => `<li><button class="prompt-button" type="button" data-prompt="${escapeAttr(prompt)}">${escapeHtml(prompt)}</button></li>`).join("");
  const login = state.user ? "" : `<div class="welcome__login"><span>질문을 보내려면 로그인이 필요합니다.</span><a class="login-button" href="/api/auth/login">Google 로그인</a></div>`;

  refs.chat.innerHTML = `
    <div class="welcome">
      <header class="welcome__header">
        <p class="rail-label">Pit lane knowledge</p>
        <h2 class="welcome__title">질문을 규정과 현장 기록에 연결합니다.</h2>
        <p class="welcome__lede">검색할 자료와 모델을 고른 뒤 질문하세요. 답변 아래의 근거 장부에서 출처·유사도·신뢰도를 바로 대조할 수 있습니다.</p>
      </header>
      ${login}
      <div class="welcome__grid">
        <section class="welcome__section">
          <h2>모델 / 이용권</h2>
          <ul class="model-roster">${modelRows}</ul>
        </section>
        <section class="welcome__section">
          <h2>검색 채널</h2>
          <ul class="source-roster">${sourceRows}</ul>
        </section>
        <section class="welcome__section welcome__section--prompts">
          <h2>질문 예시</h2>
          <ul class="prompt-list">${promptRows}</ul>
        </section>
      </div>
      <p class="welcome__warning">AI 답변은 실수할 수 있으며 차량 검차의 근거 자료로 사용할 수 없습니다. 중요한 판단은 근거 장부의 원문과 함께 확인하세요. 문의: <a href="mailto:mail@luftaquila.io">mail@luftaquila.io</a></p>
    </div>`;

  refs.chat.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      refs.query.value = button.dataset.prompt;
      resizeQuery();
      refs.query.focus();
    });
  });
}

function appendUserMessage(text) {
  const entry = document.createElement("article");
  entry.className = "log-entry";
  entry.dataset.role = "user";
  entry.innerHTML = '<div class="log-entry__role">You</div><div class="log-entry__body"></div>';
  entry.querySelector(".log-entry__body").textContent = text;
  refs.chat.appendChild(entry);
  scrollToBottom();
}

function appendAssistantShell(loading = true) {
  const entry = document.createElement("article");
  entry.className = "log-entry";
  entry.dataset.role = "assistant";
  entry.innerHTML = `
    <div class="log-entry__role">PitBot</div>
    <div class="log-entry__body">
      <div class="response-meta">${loading ? "RETRIEVAL PENDING" : "ARCHIVED RESPONSE"}</div>
      <div class="answer">${loading ? '<span class="loading-state">검색 준비 중</span>' : ""}</div>
      <div class="sources"></div>
    </div>`;
  refs.chat.appendChild(entry);
  scrollToBottom();
  return {
    entry,
    answer: entry.querySelector(".answer"),
    sources: entry.querySelector(".sources"),
    meta: entry.querySelector(".response-meta"),
  };
}

function answerNotices(answer) {
  try { return JSON.parse(answer.dataset.notices || "[]"); } catch { return []; }
}

function addAnswerNotice(answer, message) {
  const notices = answerNotices(answer);
  if (!notices.includes(message)) notices.push(message);
  answer.dataset.notices = JSON.stringify(notices);
}

function renderAnswer(answer, markdown) {
  const notices = answerNotices(answer)
    .map((notice) => `<div class="answer-notice">${escapeHtml(notice)}</div>`)
    .join("");
  answer.innerHTML = notices + renderMarkdown(markdown || "");
}

function modelLabel(id) {
  return state.models.find((model) => model.id === id)?.label || id || "모델";
}

async function submitQuestion(event) {
  event.preventDefault();
  if (state.sending) return;
  const query = refs.query.value.trim();
  if (!query) return;
  if (!state.user) {
    showToast({ message: "질문을 보내려면 Google 로그인이 필요합니다." });
    return;
  }

  const model = refs.modelSelect.value;
  if (!model) {
    showToast({ message: "사용할 수 있는 모델이 없습니다.", tone: "error" });
    return;
  }

  if (refs.chat.querySelector(".welcome")) refs.chat.replaceChildren();
  appendUserMessage(query);
  const shell = appendAssistantShell(true);
  refs.query.value = "";
  resizeQuery();
  setSending(true);

  const category = document.getElementById("category-select");
  const payload = {
    query,
    collections: selectedCollections(),
    category: category && !category.disabled ? category.value || null : null,
    confidence: selectedConfidence(),
    model,
  };
  if (state.currentSessionId) payload.session_id = state.currentSessionId;

  let fullText = "";
  let streamDone = false;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      let data = {};
      try { data = await response.json(); } catch { /* non-json failure */ }
      throw new HttpError(data.error || `질문을 처리하지 못했습니다. (${response.status})`, response.status, data);
    }

    const remaining = response.headers.get("X-Credits-Remaining");
    if (remaining !== null) updateCreditDisplay(Number.parseInt(remaining, 10));

    await readSSE(response, async (type, data) => {
      if (type === "rewrite" && typeof data === "string") {
        shell.answer.innerHTML = `<span class="loading-state">검색어 정리 · ${escapeHtml(data)}</span>`;
        shell.meta.textContent = "QUERY REWRITTEN";
      } else if (type === "sources" && Array.isArray(data)) {
        renderEvidence(shell.sources, data);
        shell.answer.innerHTML = '<span class="loading-state">근거 확인 후 답변 생성 중</span>';
        shell.meta.textContent = `${data.length} SOURCES RETRIEVED`;
      } else if (type === "retrieval" && data && typeof data === "object") {
        if (data.status === "partial") addAnswerNotice(shell.answer, "일부 검색 경로가 실패해 사용 가능한 결과만으로 답변했습니다.");
      } else if (type === "fallback" && data && typeof data === "object") {
        addAnswerNotice(shell.answer, `${modelLabel(data.from)} 응답에 실패해 ${modelLabel(data.to)}(으)로 자동 전환했습니다. 이용권 차액은 환불됩니다.`);
        shell.meta.textContent = `FALLBACK · ${modelLabel(data.to)}`;
      } else if (type === "credits" && Number.isInteger(data?.remaining)) {
        updateCreditDisplay(data.remaining);
      } else if (type === "session" && data?.session_id) {
        state.currentSessionId = data.session_id;
      } else if (type === "model" && data && typeof data === "object") {
        shell.meta.textContent = `MODEL ${data.resolved_model || data.resolved_model_id || model}`;
      } else if (type === "usage" && data && typeof data === "object") {
        const input = Number(data.input_tokens || 0).toLocaleString();
        const output = Number(data.output_tokens || 0).toLocaleString();
        const thinking = Number(data.thinking_tokens || 0).toLocaleString();
        shell.meta.textContent = `MODEL ${data.resolved_model || model} · IN ${input} / OUT ${output} / THK ${thinking}`;
      } else if (type === "token" && typeof data === "string") {
        fullText += data;
        renderAnswer(shell.answer, fullText);
        scrollToBottom();
      } else if (type === "done") {
        streamDone = true;
      }
    });

    if (!fullText) {
      fullText = "답변 내용이 비어 있습니다. 같은 질문을 다시 보내주세요.";
      addAnswerNotice(shell.answer, "모델 응답이 비어 있어 이용 가능한 답변을 표시하지 못했습니다.");
    }
    if (!streamDone) addAnswerNotice(shell.answer, "연결이 끝나기 전에 스트림이 닫혔습니다. 저장된 대화에서 완성된 답변을 다시 확인할 수 있습니다.");
    renderAnswer(shell.answer, fullText);
    await loadSessions();
  } catch (error) {
    if (fullText) {
      addAnswerNotice(shell.answer, "네트워크 연결이 끊어져 화면의 응답이 중단됐습니다. 서버에서는 생성과 저장을 계속합니다.");
      renderAnswer(shell.answer, fullText);
    } else {
      const instruction = error.status === 401
        ? "로그인 상태가 만료됐습니다. 다시 로그인한 뒤 질문을 보내주세요."
        : error.status === 402
          ? "이용권이 부족합니다. 이용권을 충전한 뒤 질문을 다시 보내주세요."
          : error.status === 503
            ? `${error.message} 다른 모델을 선택하거나 잠시 후 다시 시도해주세요.`
            : `${error.message} 잠시 후 다시 시도해주세요.`;
      shell.answer.textContent = instruction;
      shell.entry.dataset.state = "error";
      setConversationStatus("오류", "error");
      if (error.status === 401) {
        state.user = null;
        renderAuth();
      }
      if (error.status === 402) updateCreditDisplay(0);
    }
  } finally {
    setSending(false);
    scrollToBottom();
  }
}

refs.sidebarToggle.addEventListener("click", () => setSidebar(!state.sidebarOpen));
refs.sidebarOverlay.addEventListener("click", () => setSidebar(false));
desktopRail.addEventListener("change", syncSidebarMode);
refs.newChat.addEventListener("click", () => {
  setSidebar(false);
  startNewChat();
});

refs.filterOpen.addEventListener("click", () => openDialog(refs.filterDialog, refs.filterDialog.querySelector('input[name="collections"]')));
refs.filterClose.addEventListener("click", () => refs.filterDialog.close());
refs.filterApply.addEventListener("click", () => {
  updateFilterSummary();
  refs.filterDialog.close();
  refs.filterOpen.focus();
});
closeOnBackdrop(refs.filterDialog);

refs.modelSelect.addEventListener("change", () => localStorage.setItem("selectedModel", refs.modelSelect.value));
refs.query.addEventListener("input", resizeQuery);
refs.query.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    refs.form.requestSubmit();
  }
});
refs.form.addEventListener("submit", submitQuestion);

initTheme(refs.theme);
syncSidebarMode();
resizeQuery();
showWelcome(true);
Promise.all([loadModels(), loadCollections(), checkAuth()]);
