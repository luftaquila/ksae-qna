import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const user = {
  id: 1,
  name: "김피트",
  email: "pit@example.com",
  picture: null,
  credits: 18,
  is_admin: false,
};

const stats = {
  conversation_count: 12,
  question_count: 37,
  credits_used: 35,
  credits_refunded: 2,
  input_tokens: 123456,
  output_tokens: 23456,
  thinking_tokens: 7890,
};

for (const { name, viewport } of [
  { name: "desktop", viewport: { width: 1280, height: 900 } },
  { name: "mobile", viewport: { width: 320, height: 844 } },
]) {
  test(`account visual · ${name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.route("**/api/me", (route) => route.fulfill({ json: { user } }));
    await page.route("**/api/account/stats", (route) => route.fulfill({ json: { stats } }));
    await page.goto("/static/account.html");
    await expect(page).toHaveScreenshot(`account-${name}-light.png`, {
      animations: "disabled",
      fullPage: true,
    });
  });
}

test("signup requires concise privacy consent after Google linking", async ({ page }) => {
  await page.route("**/api/auth/signup-pending", (route) => route.fulfill({
    json: { name: user.name, email: user.email, picture: null, privacy_consent_version: "2026-08-14" },
  }));
  await page.route("**/api/auth/signup-consent", (route) => route.fulfill({ json: { ok: true } }));

  await page.goto("/static/signup.html");
  await expect(page.locator(".privacy-notice")).toContainText("회원 탈퇴 시까지");
  await expect(page.locator(".privacy-notice")).toContainText("거부 시 회원가입과 서비스 이용이 불가합니다");
  await expect(page.locator("#signup-submit")).toBeDisabled();
  await page.locator("#privacy-consent").check();
  await expect(page.locator("#signup-submit")).toBeEnabled();

  const consentRequest = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/api/auth/signup-consent"
  );
  await page.locator("#signup-submit").click();
  expect((await consentRequest).postDataJSON()).toEqual({ privacy_consent: true });
});

test("my page requires explicit confirmation before permanent deletion", async ({ page }) => {
  await page.route("**/api/me", (route) => route.fulfill({ json: { user } }));
  await page.route("**/api/account/stats", (route) => route.fulfill({ json: { stats } }));
  await page.route("**/api/account", (route) => route.fulfill({ json: { ok: true } }));
  page.on("dialog", (dialog) => dialog.accept());

  await page.goto("/static/account.html");
  await expect(page.locator("#account-user")).toContainText(user.email);
  await expect(page.locator("#delete-account")).toBeDisabled();
  await page.locator("#delete-confirmation").fill("회원탈퇴");
  await expect(page.locator("#delete-account")).toBeEnabled();

  const deleteRequest = page.waitForRequest((request) =>
    new URL(request.url()).pathname === "/api/account" && request.method() === "DELETE"
  );
  await page.locator("#delete-account").click();
  expect((await deleteRequest).postDataJSON()).toEqual({ confirmation: "회원탈퇴" });
});

test("my page shows lifetime usage statistics without model details", async ({ page }) => {
  await page.route("**/api/me", (route) => route.fulfill({ json: { user } }));
  await page.route("**/api/account/stats", (route) => route.fulfill({ json: { stats } }));

  await page.goto("/static/account.html");

  await expect(page.locator("#usage-stats")).toContainText("12");
  await expect(page.locator("#usage-stats")).toContainText("37");
  await expect(page.locator("#usage-stats")).toContainText("123,456");
  await expect(page.locator("#usage-stats")).not.toContainText(/Gemini|Flash|Pro|폴백|대체 모델/i);
});

for (const pageName of ["signup", "account"]) {
  test(`${pageName} page is accessible and does not overflow at 320px`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 844 });
    if (pageName === "signup") {
      await page.route("**/api/auth/signup-pending", (route) => route.fulfill({
        json: { name: user.name, email: user.email, picture: null, privacy_consent_version: "2026-08-14" },
      }));
    } else {
      await page.route("**/api/me", (route) => route.fulfill({ json: { user } }));
      await page.route("**/api/account/stats", (route) => route.fulfill({ json: { stats } }));
    }

    await page.goto(`/static/${pageName}.html`);
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(results.violations.filter((violation) =>
      ["serious", "critical"].includes(violation.impact)
    )).toEqual([]);
  });
}
