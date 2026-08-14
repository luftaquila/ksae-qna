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
      await expect(page.getByRole("tab", { name: "사용자" })).toHaveAttribute("aria-selected", "true");
      await expect(page).toHaveScreenshot(`admin-${viewport.name}-${theme}.png`, { animations: "disabled" });
    });
  }
}

test("admin navigation and model controls work without health-check copy", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await setThemeBeforeLoad(page, "light");
  await installAdminMocks(page);
  await page.goto("/static/admin.html");
  await page.waitForFunction(() => document.fonts.status === "loaded");
  await page.getByRole("tab", { name: "모델" }).click();
  await expect(page.locator(".model-card")).toHaveCount(2);
  await expect(page.locator("#models-grid")).toContainText("API 키 설정됨");
  await expect(page.locator("#models-grid")).toContainText("기본");
  await expect(page.locator("#models-grid")).toContainText("폴백");
  await expect(page.locator("#models-grid")).not.toContainText("차감 이용권");
  await expect(page.locator("#models-grid")).not.toContainText(/Canary|상태 확인|최근 요청 실패/);
  await expect(page).toHaveScreenshot("admin-desktop-models-light.png", { animations: "disabled" });
  await page.getByRole("tab", { name: "대화 기록" }).click();
  await page.locator(".conv-session-item").first().click();
  await expect(page.locator(".admin-msg")).toHaveCount(2);
  await expect(page).toHaveScreenshot("admin-desktop-conversation-light.png", { animations: "disabled" });
});

test("clicking a user name opens only that user's conversations", async ({ page }) => {
  await installAdminMocks(page);
  await page.goto("/static/admin.html");

  const filteredRequest = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/api/admin/users/1/sessions"
  );
  await page.getByRole("button", { name: "김피트 사용자의 대화 기록 보기" }).click();
  await filteredRequest;

  await expect(page.getByRole("tab", { name: "대화 기록" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#conv-user-select")).toHaveValue("1");
  await expect(page.locator(".conv-session-item")).toHaveCount(1);
  await expect(page.locator(".conv-session-item")).toContainText("Formula 지상고 기준");
});

test("admin has no serious accessibility violations", async ({ page }) => {
  await installAdminMocks(page);
  await page.goto("/static/admin.html");
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze();
  expect(results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact))).toEqual([]);
});

test("admin configures and immediately applies the monthly credit refill", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await setThemeBeforeLoad(page, "light");
  await installAdminMocks(page);
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto("/static/admin.html");
  await page.getByRole("tab", { name: "설정" }).click();

  const input = page.locator("#setting-monthly-refill-credits");
  await expect(input).toHaveValue("20");
  await input.fill("24");

  const settingsRequest = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/api/admin/settings" && request.method() === "PATCH"
  );
  const refillRequest = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/api/admin/credits/monthly-refill" && request.method() === "POST"
  );
  await page.locator("#monthly-refill-btn").click();

  expect((await settingsRequest).postDataJSON()).toMatchObject({ monthly_refill_credits: 24 });
  await refillRequest;
  await expect(page).toHaveScreenshot("admin-desktop-settings-light.png", { animations: "disabled" });
});

for (const width of [320, 375, 414, 768]) {
  test(`admin does not overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await installAdminMocks(page);
    await page.goto("/static/admin.html");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await page.getByRole("tab", { name: "설정" }).click();
    const settingsOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(settingsOverflow).toBeLessThanOrEqual(0);
  });
}
