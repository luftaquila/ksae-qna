import { marked } from "/static/vendor/marked.esm.js";

marked.setOptions({ gfm: true, breaks: false });

export function escapeHtml(value = "") {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}

export function escapeAttr(value = "") {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function normalizeUtc(value) {
  if (!value) return null;
  return new Date(String(value) + (String(value).endsWith("Z") ? "" : "Z"));
}

export function formatLocal(value) {
  const date = normalizeUtc(value);
  if (!date || Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "2-digit",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatRelative(value) {
  const date = normalizeUtc(value);
  if (!date || Number.isNaN(date.getTime())) return "—";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absolute = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat("ko", { numeric: "auto" });
  if (absolute < 60) return formatter.format(seconds, "second");
  if (absolute < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (absolute < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  if (absolute < 2592000) return formatter.format(Math.round(seconds / 86400), "day");
  if (absolute < 31536000) return formatter.format(Math.round(seconds / 2592000), "month");
  return formatter.format(Math.round(seconds / 31536000), "year");
}

export function renderMarkdown(markdown = "") {
  const template = document.createElement("template");
  template.innerHTML = marked.parse(String(markdown));
  template.content.querySelectorAll("script, style, iframe, object, embed, form").forEach((node) => node.remove());
  template.content.querySelectorAll("*").forEach((node) => {
    for (const attribute of [...node.attributes]) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim().toLowerCase();
      if (name.startsWith("on") || ((name === "href" || name === "src") && value.startsWith("javascript:"))) {
        node.removeAttribute(attribute.name);
      }
    }
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
  return template.innerHTML;
}

export function showToast({ message, tone = "info", action = null, duration = 6000 }) {
  const region = document.getElementById("toast-region");
  if (!region) return () => {};

  const toast = document.createElement("div");
  toast.className = `toast${tone === "error" ? " toast--error" : ""}`;
  toast.setAttribute("role", tone === "error" ? "alert" : "status");

  const text = document.createElement("span");
  text.className = "toast__message";
  text.textContent = message;
  toast.appendChild(text);

  let timer = null;
  const close = () => {
    if (timer) window.clearTimeout(timer);
    toast.remove();
  };

  if (action) {
    const button = document.createElement("button");
    button.className = "toast__action";
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", async () => {
      close();
      await action.run();
    });
    toast.appendChild(button);
  }

  toast.addEventListener("mouseenter", () => timer && window.clearTimeout(timer));
  toast.addEventListener("mouseleave", () => {
    if (duration > 0) timer = window.setTimeout(close, duration);
  });
  region.appendChild(toast);
  if (duration > 0) timer = window.setTimeout(close, duration);
  return close;
}

export function openDialog(dialog, firstFocus = null) {
  if (!dialog) return;
  dialog.showModal();
  window.requestAnimationFrame(() => (firstFocus || dialog.querySelector("input, select, textarea, button"))?.focus());
}

export function closeOnBackdrop(dialog) {
  dialog?.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

export function chevronIcon() {
  return '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>';
}

export function renderEvidence(container, sources = []) {
  container.replaceChildren();
  if (!sources.length) return;

  const toggle = document.createElement("button");
  toggle.className = "sources-toggle";
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.innerHTML = `<span>Evidence ledger · ${sources.length}건</span>${chevronIcon()}`;

  const list = document.createElement("div");
  list.className = "sources-list";
  list.dataset.open = "false";

  for (const source of sources) {
    const item = document.createElement("article");
    item.className = "source-item";

    const confidence = source.confidence
      ? `<span class="confidence" data-confidence="${escapeAttr(source.confidence)}">${escapeHtml(source.confidence)}</span>`
      : "";
    const dates = source.dates?.length
      ? `<span>발언일 ${escapeHtml(source.dates.slice(0, 3).join(" · "))}${source.dates.length > 3 ? ` 외 ${source.dates.length - 3}건` : ""}</span>`
      : "";
    const link = source.url
      ? `<a href="${escapeAttr(source.url)}" target="_blank" rel="noopener noreferrer">원문 열기</a>`
      : "";
    const score = Number.isFinite(Number(source.score)) ? `${(Number(source.score) * 100).toFixed(1)}%` : "—";
    const title = String(source.source || "근거 자료").replace(/^\[[^\]]*·[^\]]*\]\s*/, "");

    item.innerHTML = `
      <div class="source-item__meta">
        ${confidence}
        <span>유사도 ${score}</span>
        ${dates}
        ${link}
      </div>
      <div class="source-item__title">${escapeHtml(title)}</div>
      <div class="source-item__content">${escapeHtml(source.content || "")}</div>
    `;
    list.appendChild(item);
  }

  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    list.dataset.open = String(open);
  });

  container.append(toggle, list);
}
