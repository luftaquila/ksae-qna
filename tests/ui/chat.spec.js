import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { installChatMocks, setThemeBeforeLoad } from "./mocks.js";

const viewports = [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "tablet", width: 1024, height: 768 },
  { name: "mobile", width: 390, height: 844 },
];

for (const viewport of viewports) {
  for (const theme of ["light", "dark"]) {
    test(`chat visual · ${viewport.name} · ${theme}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await setThemeBeforeLoad(page, theme);
      await installChatMocks(page);
      await page.goto("/static/index.html");
      await page.waitForFunction(() => document.fonts.status === "loaded");
      await expect(page.locator(".welcome-title")).toHaveText("PitBot");
      await expect(page).toHaveScreenshot(`chat-${viewport.name}-${theme}.png`, { animations: "disabled" });
    });
  }
}

test("chat sends a question and renders streamed evidence", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setThemeBeforeLoad(page, "light");
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await page.waitForFunction(() => document.fonts.status === "loaded");
  const chatRequest = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/chat");
  await page.locator("#query").fill("지상고 기준은?");
  await page.locator("#query").press("Enter");
  const chatPayload = (await chatRequest).postDataJSON();
  expect(chatPayload).not.toHaveProperty("confidence");
  expect(chatPayload).not.toHaveProperty("model");
  expect(chatPayload).not.toHaveProperty("category");
  expect(chatPayload).not.toHaveProperty("competition");
  await expect(page.locator(".msg.assistant .answer")).toContainText("측정 조건을 먼저 확인해야 합니다");
  await expect(page.getByRole("button", { name: /참고 문서 1건/ })).toBeVisible();
  await expect(page.locator("#credit-badge")).toContainText("17 이용권");
  await expect(page).toHaveScreenshot("chat-mobile-response-light.png", { animations: "disabled" });
});

test("chat has no serious accessibility violations", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await expect(page.locator(".welcome-title")).toHaveText("PitBot");
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact))).toEqual([]);
});

test("chat matches the server input limit", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await expect(page.locator("#query")).toHaveAttribute("maxlength", "2000");
});

test("chat keeps the original welcome copy", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await expect(page.locator(".welcome-title")).toHaveText("PitBot");
  await expect(page.locator(".welcome-subtitle")).toHaveText("자작자동차 규정 및 Q&A 챗봇");
  await expect(page.locator(".welcome-items")).toContainText("질문 1회당 이용권 1장이 차감됩니다");
  await expect(page.locator(".welcome-items")).toContainText("입력창 상단에서 AI가 검색에 사용할 데이터를 선택할 수 있습니다.");
  await expect(page.locator(".welcome-chip-list")).toContainText("규정 — 2026 대회 규정");
  await expect(page.locator(".welcome-chip-list")).toContainText("Q&A — KSAE Q&A 게시판");
  await expect(page.locator(".welcome-chip-list")).toContainText("AARK — AARK 익명톡방 (2025년 2월 ~ 2026년 7월)");
  await expect(page.locator(".welcome-icon")).toHaveText(["⚡", "📚"]);
  const guideFontSize = await page.locator(".welcome-item").first().evaluate((element) => parseFloat(getComputedStyle(element).fontSize));
  expect(guideFontSize).toBeGreaterThanOrEqual(15);
  await expect(page.locator(".welcome-warn")).toHaveText("LLM은 실수하거나 잘못된 정보를 제공할 수 있으며, AI 답변은 차량검차 시 근거자료로 사용할 수 없습니다.");
  await expect(page.locator(".welcome-warn")).toHaveCSS("word-break", "keep-all");
  await page.setViewportSize({ width: 320, height: 844 });
  const endingRows = await page.locator(".welcome-warn").evaluate((element) => {
    const text = element.firstChild;
    const start = text.length - "없습니다.".length;
    return Array.from({ length: "없습니다.".length }, (_, offset) => {
      const range = document.createRange();
      range.setStart(text, start + offset);
      range.setEnd(text, start + offset + 1);
      return Math.round(range.getBoundingClientRect().top);
    });
  });
  expect(new Set(endingRows).size).toBe(1);
  await expect(page.locator(".welcome")).not.toContainText("사용 모델");
  await expect(page.locator("#model-select")).toHaveCount(0);
  await expect(page.locator("#query")).toHaveAttribute("placeholder", "질문을 입력하세요...");
  await expect(page.locator(".source-group .control-label")).toHaveText("검색 소스");
  await expect(page.locator("#send span")).toHaveText("전송");
});

test("signed-in users can open my page from their profile", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  const accountLink = page.getByRole("link", { name: "김피트 마이페이지" });
  await expect(accountLink).toHaveAttribute("href", "/account");
  await expect(accountLink.locator(".profile-avatar")).toHaveText("김");
});

for (const viewport of [
  { width: 320, height: 568 },
  { width: 375, height: 667 },
  { width: 390, height: 844 },
]) {
  test(`mobile welcome notice is not clipped at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await installChatMocks(page);
    await page.goto("/static/index.html");
    await expect(page.locator(".welcome-warn")).toBeVisible();

    const overflow = await page.locator(".welcome").evaluate((element) => ({
      horizontal: element.scrollWidth - element.clientWidth,
      vertical: element.scrollHeight - element.clientHeight,
    }));
    expect(overflow.horizontal).toBeLessThanOrEqual(1);
    expect(overflow.vertical).toBeLessThanOrEqual(1);

    await page.locator(".welcome-warn").scrollIntoViewIfNeeded();
    const positions = await page.locator(".welcome-warn").evaluate((element) => {
      const notice = element.getBoundingClientRect();
      const chat = element.closest(".chat").getBoundingClientRect();
      return {
        noticeTop: notice.top,
        noticeBottom: notice.bottom,
        chatTop: chat.top,
        chatBottom: chat.bottom,
      };
    });
    expect(positions.noticeTop).toBeGreaterThanOrEqual(positions.chatTop - 1);
    expect(positions.noticeBottom).toBeLessThanOrEqual(positions.chatBottom + 1);
  });
}

test("collection chips keep their width when toggled", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  const chips = page.locator(".collection-chip");
  for (let index = 0; index < await chips.count(); index += 1) {
    const chip = chips.nth(index);
    const selectedWidth = await chip.evaluate((element) => element.getBoundingClientRect().width);
    await chip.click();
    const unselectedWidth = await chip.evaluate((element) => element.getBoundingClientRect().width);
    expect(unselectedWidth).toBe(selectedWidth);
  }
});

test("AARK always searches every confidence level", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await expect(page.locator("#confidence-select")).toHaveCount(0);
  await expect(page.locator('.collection-chip input[value="kb"]')).toBeChecked();
});

test("source selector exposes only broad source groups", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await installChatMocks(page);
  await page.goto("/static/index.html");
  const sourceInputs = page.locator('.collection-chip input[name="collections"]');
  await expect(sourceInputs).toHaveCount(3);
  expect(await sourceInputs.evaluateAll((elements) => elements.map((element) => element.value))).toEqual(["rules", "qna", "kb"]);
  await expect(page.locator("#category-select")).toHaveCount(0);
  await expect(page.locator(".rules-detail-toggle")).toHaveCount(0);
  await expect(page.locator(".collection-chip-child")).toHaveCount(0);
  const chipRows = await page.locator(".collection-chip").evaluateAll((elements) =>
    elements.map((element) => Math.round(element.getBoundingClientRect().top)),
  );
  expect(new Set(chipRows).size).toBe(1);

  const rulesInput = sourceInputs.first();
  await rulesInput.focus();
  await expect(rulesInput).toBeFocused();
  await page.keyboard.press("Space");
  await expect(rulesInput).not.toBeChecked();
});

test("source selector always keeps at least one source enabled", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");

  const inputs = page.locator('input[name="collections"]');
  const chips = page.locator(".collection-chip");
  await expect(inputs).toHaveCount(3);
  await chips.nth(0).click();
  await chips.nth(1).click();
  await chips.nth(2).click();

  await expect(inputs.nth(2)).toBeChecked();
});

test("chat payload leaves competition and Q&A routing to the server", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  const chatRequest = page.waitForRequest((request) => new URL(request.url()).pathname === "/api/chat");
  await page.locator("#query").fill("배터리 규정 알려줘");
  await page.locator("#query").press("Enter");
  const payload = (await chatRequest).postDataJSON();
  expect(payload.collections).toEqual(["rules", "qna", "kb"]);
  expect(payload).not.toHaveProperty("category");
  expect(payload).not.toHaveProperty("competition");
});

for (const width of [320, 375, 414, 768]) {
  test(`chat does not overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await installChatMocks(page);
    await page.goto("/static/index.html");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
