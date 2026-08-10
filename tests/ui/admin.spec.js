import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { installAdminMocks, setThemeBeforeLoad } from "./mocks.js";

const viewports = [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "tablet", width: 1024, height: 768 },
  { name: "mobile", width: 390, height: 844 },
];

for (const viewport of viewports) {
  for (const theme of ["light", "dark"]) {
    test(`admin visual · ${viewport.name} · ${theme}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await setThemeBeforeLoad(page, theme);
      await installAdminMocks(page);
      await page.goto("/static/admin.html");
      await page.waitForFunction(() => document.fonts.status === "loaded");
      await expect(page.getByRole("heading", { name: "사용자 운영" })).toBeVisible();
      await expect(page).toHaveScreenshot(`admin-${viewport.name}-${theme}.png`, { animations: "disabled" });
    });
  }
}

test("admin tabs, dialogs, and keyboard model reorder", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installAdminMocks(page);
  await page.goto("/static/admin.html");
  await page.getByRole("tab", { name: /대화/ }).click();
  await page.getByRole("option", { name: /Formula 지상고 기준/ }).click();
  await expect(page.locator(".admin-message")).toHaveCount(2);
  await page.getByRole("tab", { name: /모델/ }).click();
  const firstModel = page.locator(".model-row").first();
  await firstModel.focus();
  await firstModel.press("Alt+ArrowDown");
  await expect(page.locator("#model-order-status")).toContainText("2번째로 이동");
  await page.getByRole("tab", { name: /사용자/ }).click();
  await page.getByRole("button", { name: "조정" }).first().click();
  await expect(page.getByRole("dialog", { name: "사용자 이용권 조정" })).toBeVisible();
});

test("admin has no serious accessibility violations", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installAdminMocks(page);
  await page.goto("/static/admin.html");
  await expect(page.getByRole("heading", { name: "사용자 운영" })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact))).toEqual([]);
});

for (const width of [320, 375, 414, 768]) {
  test(`admin does not overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await installAdminMocks(page);
    await page.goto("/static/admin.html");
    await expect(page.getByRole("heading", { name: "사용자 운영" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
}
