export class HttpError extends Error {
  constructor(message, status, data = null) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.data = data;
  }
}

export async function requestJSON(url, options = {}) {
  const response = await fetch(url, options);
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : null;

  if (!response.ok) {
    const message = data?.error || `요청을 처리하지 못했습니다. (${response.status})`;
    throw new HttpError(message, response.status, data);
  }
  return data || {};
}

export function jsonOptions(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export async function readSSE(response, onEvent) {
  if (!response.body) throw new Error("스트리밍 응답을 열지 못했습니다.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = async (block) => {
    let type = "message";
    const lines = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) type = line.slice(6).trim();
      if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
    }
    if (!lines.length) return;
    const raw = lines.join("\n");
    let data = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      // Plain-text SSE data remains text.
    }
    await onEvent(type, data);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() || "";
    for (const block of blocks) await dispatch(block);
  }

  buffer += decoder.decode();
  if (buffer.trim()) await dispatch(buffer);
}
