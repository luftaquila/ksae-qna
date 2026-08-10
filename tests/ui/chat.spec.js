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
      await expect(page.locator(".welcome__title")).toBeVisible();
      await expect(page).toHaveScreenshot(`chat-${viewport.name}-${theme}.png`, { animations: "disabled" });
    });
  }
}

test("chat keyboard, filters, and SSE response", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await page.getByRole("button", { name: "검색 근거 설정" }).click();
  await expect(page.getByRole("dialog", { name: "검색 근거 설정" })).toBeVisible();
  await page.getByRole("button", { name: "설정 적용" }).click();
  await page.locator("#query").fill("지상고 기준은?");
  await page.locator("#query").press("Enter");
  await expect(page.locator('.log-entry[data-role="assistant"] .answer')).toContainText("측정 조건을 먼저 확인해야 합니다");
  await expect(page.getByRole("button", { name: /Evidence ledger/ })).toBeVisible();
  await expect(page.locator("#credit-button")).toContainText("14 이용권");
});

test("chat input matches the server's 2,000-character limit", async ({ page }) => {
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await expect(page.locator("#query")).toHaveAttribute("maxlength", "2000");
  await expect(page.locator("#query-count")).toHaveText("0 / 2,000");
});

test("chat has no serious accessibility violations", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installChatMocks(page);
  await page.goto("/static/index.html");
  await expect(page.locator(".welcome__title")).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact))).toEqual([]);
});

for (const width of [320, 375, 414, 768]) {
  test(`chat does not overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await installChatMocks(page);
    await page.goto("/static/index.html");
    await expect(page.locator(".welcome__title")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
