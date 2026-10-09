import { expect, test } from "@playwright/test";

const USER = process.env.E2E_USER ?? "e2e";
const PASSWORD = process.env.E2E_PASSWORD ?? "e2e-password-not-secret";

test("redirects to login without a session", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("dashboard shows PAPER, labelled test data, and pausing never sells", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Потребител").fill(USER);
  await page.getByLabel("Парола").fill(PASSWORD);
  await page.getByRole("button", { name: "Влез" }).click();
  await expect(page).toHaveURL(/\/$/);

  await expect(page.getByText("PAPER · СИМУЛАЦИЯ")).toBeVisible();
  await expect(page.getByText(/ТЕСТОВИ ДАННИ: цените и портфейлът са от фикстура/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Портфейл (стойност в EUR)" })).toBeVisible();
  await expect(page.getByRole("img", { name: /Стойност на портфейла спрямо задържане/ })).toBeVisible();
  await expect(page.getByText("LIVE е")).toBeVisible();
  await expect(page.locator("option", { hasText: "robinhood (недостъпна)" })).toHaveAttribute("disabled", "");
  await page.screenshot({ path: "test-results/dashboard.png", fullPage: true });

  const pause = page.getByRole("button", { name: "Спри новите поръчки" });
  await pause.click();
  await expect(page.getByText(/Нищо не е продадено/)).toBeVisible();
  const resume = page.getByRole("button", { name: "Възобнови новите поръчки" });
  await expect(resume).toBeVisible();
  await resume.click();
  await expect(page.getByRole("button", { name: "Спри новите поръчки" })).toBeVisible();

  const dialog = page.getByRole("dialog");
  await page.getByRole("button", { name: "Продай позицията" }).click();
  await expect(dialog.getByRole("button", { name: "Продай" })).toBeDisabled(); // needs the explicit checkbox
  await dialog.getByRole("button", { name: "Отказ" }).click();
});
