export const collections = [
  { key: "rules", label: "규정", description: "2026 대회 규정 전체 (Formula/Baja/EV)" },
  { key: "qna", label: "Q&A", description: "KSAE Q&A 게시판 질의응답" },
  { key: "kb", label: "AARK", description: "AARK 익명톡방 (2025년 2월 ~ 2026년 7월)" },
];

export const users = [
  {
    id: 1,
    name: "김피트",
    email: "pit@example.com",
    picture: null,
    credits: 18,
    created_at: "2026-07-01T03:00:00",
    last_active_at: null,
    total_input_tokens: 12400,
    total_output_tokens: 3820,
    total_thinking_tokens: 2100,
    model_usage: [
      { model: "gemini-3-pro", input_tokens: 12400, output_tokens: 3820, thinking_tokens: 2100, message_count: 7 },
    ],
  },
  {
    id: 2,
    name: "이포뮬러",
    email: "formula@example.com",
    picture: null,
    credits: 3,
    created_at: "2026-07-12T03:00:00",
    last_active_at: null,
    total_input_tokens: 2800,
    total_output_tokens: 910,
    total_thinking_tokens: 420,
    model_usage: [
      { model: "gemini-3-flash", input_tokens: 2800, output_tokens: 910, thinking_tokens: 420, message_count: 3 },
    ],
  },
];

export const adminModels = [
  {
    id: "gemini-3-flash",
    label: "Gemini Flash (Latest)",
    role: "primary",
    provider: "gemini",
    provider_available: true,
    admin_enabled: true,
    available: true,
    healthy: null,
    resolved_model: null,
  },
  {
    id: "gemini-3-pro",
    label: "Gemini Pro (Latest)",
    role: "fallback",
    provider: "gemini",
    provider_available: true,
    admin_enabled: true,
    available: true,
    healthy: null,
    resolved_model: null,
  },
];

export const adminOverview = {
  period: "30d",
  users: { total_users: 28, active_users: 17, new_users: 6, current_credits: 412, low_credit_users: 4 },
  activity: { questions: 186, answers: 181, credits_used: 186, credits_refunded: 5 },
  reliability: {
    tracked_turns: 186,
    successful_turns: 183,
    fallback_turns: 7,
    failed_turns: 3,
    pending_turns: 0,
    degraded_retrieval_turns: 4,
    avg_first_token_ms: 1280,
    avg_total_ms: 8340,
    success_rate: 98.4,
    fallback_rate: 3.8,
  },
  tokens: { input_tokens: 15200, output_tokens: 4730, thinking_tokens: 2520, total_tokens: 22450, estimated_cost_usd: 0.42 },
  models: [
    { model: "gemini-3-flash", label: "Gemini Flash (Latest)", message_count: 176, input_tokens: 14200, output_tokens: 4400, thinking_tokens: 2300, estimated_cost_usd: 0.31 },
    { model: "gemini-3-pro", label: "Gemini Pro (Latest)", message_count: 7, input_tokens: 1000, output_tokens: 330, thinking_tokens: 220, estimated_cost_usd: 0.11 },
  ],
  daily: [
    { date: "2026-08-01", questions: 8, active_users: 5 },
    { date: "2026-08-02", questions: 12, active_users: 7 },
    { date: "2026-08-03", questions: 6, active_users: 4 },
    { date: "2026-08-04", questions: 15, active_users: 9 },
    { date: "2026-08-05", questions: 9, active_users: 6 },
    { date: "2026-08-06", questions: 18, active_users: 10 },
    { date: "2026-08-07", questions: 14, active_users: 8 },
  ],
};

export async function installChatMocks(page) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/me") return route.fulfill({ json: { user: { id: 1, name: "김피트", email: "pit@example.com", picture: null, credits: 18, is_admin: true }, low_credit_threshold: 5, unlimited_credits: false } });
    if (path === "/api/account/stats") return route.fulfill({ json: { stats: { conversation_count: 12, question_count: 37, credits_used: 35, credits_refunded: 2, input_tokens: 123456, output_tokens: 23456, thinking_tokens: 7890 } } });
    if (path === "/api/collections") return route.fulfill({ json: { collections } });
    if (path === "/api/sessions") return route.fulfill({ json: { sessions: [{ id: 10, title: "Formula 지상고 기준" }] } });
    if (path === "/api/transactions") return route.fulfill({ json: { transactions: [
      { amount: -1, type: "usage", memo: "질문 (Gemini Pro (Latest))", created_at: "2026-08-14T03:00:00" },
      { amount: 1, type: "refund", memo: "오류 환불 (Gemini Pro (Latest))", created_at: "2026-08-14T03:01:00" },
    ] } });
    if (path === "/api/account" && request.method() === "DELETE") return route.fulfill({ json: { ok: true } });
    if (path === "/api/chat") {
      const sources = [{ source: "2026 Formula 규정", score: 0.91, url: "https://example.com/rule", content: "차량의 지상고 측정 조건에 관한 근거입니다." }];
      const body = [
        `event: sources\ndata: ${JSON.stringify(sources)}\n\n`,
        `event: fallback\ndata: ${JSON.stringify({ from: "gemini-3-flash", to: "gemini-3-pro" })}\n\n`,
        `event: token\ndata: ${JSON.stringify("결론부터 말하면, 측정 조건을 먼저 확인해야 합니다.")}\n\n`,
        `event: credits\ndata: ${JSON.stringify({ remaining: 17 })}\n\n`,
        `event: session\ndata: ${JSON.stringify({ session_id: 11 })}\n\n`,
        "event: done\ndata: {}\n\n",
      ].join("");
      return route.fulfill({ status: 200, headers: { "content-type": "text/event-stream", "x-credits-remaining": "17" }, body });
    }
    if (/\/api\/sessions\/\d+\/messages/.test(path)) return route.fulfill({ json: { messages: [] } });
    if (/\/api\/sessions\/\d+/.test(path) && request.method() === "DELETE") return route.fulfill({ json: { ok: true } });
    if (path === "/api/auth/logout") return route.fulfill({ json: { ok: true } });
    return route.fulfill({ status: 404, json: { error: `Unhandled mock: ${path}` } });
  });
}

export async function installAdminMocks(page) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/admin/check") return route.fulfill({ json: { admin: true, email: "admin@example.com" } });
    if (path === "/api/admin/overview") return route.fulfill({ json: { overview: { ...adminOverview, period: url.searchParams.get("period") || "30d" } } });
    if (path === "/api/admin/users") return route.fulfill({ json: { users } });
    if (path === "/api/admin/models") return route.fulfill({ json: { models: adminModels } });
    if (path === "/api/admin/settings" && request.method() === "GET") return route.fulfill({ json: { settings: { default_credits: "15", monthly_refill_credits: "20", low_credit_threshold: "5", unlimited_credits: "false" } } });
    if (path === "/api/admin/settings" && request.method() === "PATCH") {
      const body = request.postDataJSON();
      return route.fulfill({ json: { ok: true, settings: {
        default_credits: String(body.default_credits),
        monthly_refill_credits: String(body.monthly_refill_credits),
        low_credit_threshold: String(body.low_credit_threshold),
        unlimited_credits: String(body.unlimited_credits),
      } } });
    }
    if (path === "/api/admin/sessions") return route.fulfill({ json: { sessions: [{ id: 10, title: "Formula 지상고 기준", user_name: "김피트", updated_at: "2026-08-01T03:00:00", deleted_at: null }] } });
    if (path === "/api/admin/users/1/sessions") return route.fulfill({ json: { sessions: [{ id: 10, title: "Formula 지상고 기준", user_name: "김피트", updated_at: "2026-08-01T03:00:00", deleted_at: null }] } });
    if (/\/api\/admin\/users\/\d+\/sessions/.test(path)) return route.fulfill({ json: { sessions: [] } });
    if (/\/api\/admin\/sessions\/\d+\/messages/.test(path)) return route.fulfill({ json: { messages: [{ id: 1, role: "user", content: "지상고 기준은?", created_at: "2026-08-01T03:00:00" }, { id: 2, role: "assistant", content: "측정 조건을 먼저 확인해야 합니다.", created_at: "2026-08-01T03:01:00", model: "gemini-3-pro", input_tokens: 100, output_tokens: 30, thinking_tokens: 12, sources: "[]" }] } });
    if (/\/api\/admin\/users\/\d+\/transactions/.test(path)) return route.fulfill({ json: { transactions: [] } });
    if (/\/api\/admin\/users\/\d+\/token-usage/.test(path)) return route.fulfill({ json: { usage: users[0].model_usage } });
    if (/\/api\/admin\/users\/\d+\/credits/.test(path) && request.method() === "PATCH") return route.fulfill({ json: { credits: 20 } });
    if (/\/api\/admin\/models\//.test(path) && request.method() === "PATCH") return route.fulfill({ json: { ok: true } });
    if (path === "/api/admin/credits/monthly-refill" && request.method() === "POST") return route.fulfill({ json: { ok: true, target_credits: 24, affected_users: 1, total_credits: 21 } });
    if (path === "/api/admin/credits/bulk" && request.method() === "POST") return route.fulfill({ json: { ok: true, affected: 2 } });
    return route.fulfill({ status: 404, json: { error: `Unhandled mock: ${path}` } });
  });
}

export async function setThemeBeforeLoad(page, theme) {
  await page.addInitScript((value) => localStorage.setItem("theme", value), theme);
}
