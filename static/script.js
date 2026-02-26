const chat = document.getElementById("chat");
const form = document.getElementById("form");
const queryInput = document.getElementById("query");
const sendBtn = document.getElementById("send");

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

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

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
      html += ` <a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">원문 보기</a>`;
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
