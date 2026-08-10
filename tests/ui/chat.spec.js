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
  expect((await chatRequest).postDataJSON()).not.toHaveProperty("confidence");
  await expect(page.locator(".msg.assistant .answer")).toContainText("측정 조건을 먼저 확인해야 합니다");
  await expect(page.getByRole("button", { name: /참고 문서 1건/ })).toBeVisible();
  await expect(page.locator("#credit-badge")).toContainText("14 이용권");
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
  await expect(page.locator(".welcome-items")).toContainText("질문 1회당 선택한 모델에 따라 이용권이 차감됩니다");
  await expect(page.locator(".welcome-items")).toContainText("입력창 상단에서 AI가 검색에 사용할 데이터를 선택할 수 있습니다.");
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
  await expect(page.locator("#query")).toHaveAttribute("placeholder", "질문을 입력하세요...");
  await expect(page.locator(".source-group .control-label")).toHaveText("검색 소스");
  await expect(page.locator("#send span")).toHaveText("전송");
});

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

for (const width of [320, 375, 414, 768]) {
  test(`chat does not overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await installChatMocks(page);
    await page.goto("/static/index.html");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
