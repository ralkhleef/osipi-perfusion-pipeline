// @ts-check
// End-to-end acceptance spec for the OSIPI perfusion pipeline UI.
//
// This runs against the REAL app in a REAL browser and must be executed on a
// machine where the stack is up (it cannot run inside the CI sandbox because it
// needs Docker + a rendering browser). Companion: tests/e2e/README.md.
//
//   1) docker compose up --build          # app at http://localhost:8000
//   2) npm i -D @playwright/test && npx playwright install chromium
//   3) BASE_URL=http://localhost:8000 npx playwright test tests/e2e/acceptance.spec.js
//
// Screenshots for every step land in tests/e2e/screenshots/.
//
// The selectors are intentionally text/role based so the spec keeps working as
// the DOM evolves; if any locator misses, adjust it to the running app rather
// than loosening an assertion.
const { test, expect } = require("@playwright/test");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://localhost:8000";
const INCOMING = path.resolve(__dirname, "../../submissions/incoming");
const SHOTS = path.resolve(__dirname, "screenshots");
const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });

const LENA_SINGLE = path.join(INCOMING, "lena_01_exact_single_submission.zip");
const LENA_MISSING_ATT = path.join(INCOMING, "lena_03_missing_att_expected_warning.zip");
const LENA_BATCH = path.join(INCOMING, "lena_04_batch_three_submissions.zip");

async function uploadZip(page, filePath) {
  const input = page.locator('input[type="file"]').first();
  await input.setInputFiles(filePath);
}

test.describe("OSIPI acceptance — full workflow", () => {
  test("app starts and /api/config responds", async ({ page, request }) => {
    const cfg = await request.get(`${BASE_URL}/api/config`);
    expect(cfg.ok()).toBeTruthy();
    const body = await cfg.json();
    expect(Array.isArray(body.challenge_types)).toBeTruthy();
    await page.goto(BASE_URL);
    await expect(page).toHaveTitle(/OSIPI|Perfusion|Pipeline/i);
    await shot(page, "01_startup");
  });

  test("Lena single: upload → detect → validate → QC → export", async ({ page }) => {
    await page.goto(BASE_URL);
    await uploadZip(page, LENA_SINGLE);
    // Upload & Detect
    await page.getByRole("button", { name: /upload.*(detect|continue)/i }).click();
    await expect(page.getByText(/ASL/i).first()).toBeVisible({ timeout: 30000 });
    await shot(page, "02_detect");

    // Validate
    await page.getByRole("button", { name: /validate/i }).first().click();
    await expect(page.getByText(/passed|complete|warning/i).first()).toBeVisible({ timeout: 60000 });
    await shot(page, "03_validate");

    // Result-only submissions should not require execution
    await expect(page.getByText(/execution not required|result maps only|result-only/i).first()).toBeVisible();

    // Continue through Run to QC & Preview
    await page.getByRole("button", { name: /continue to (run|score)/i }).first().click();
    await page.getByRole("button", { name: /continue to (score|export)/i }).first().click();
    await expect(page.getByText(/QC & Preview/i).first()).toBeVisible();
    await expect(page.getByText(/Quality checks and generic reference comparisons/i)).toBeVisible();
    await expect(page.getByText(/not official OSIPI/i).first()).toBeVisible();
    await shot(page, "04_qc_preview");

    // Export
    await page.getByRole("button", { name: /continue to export/i }).first().click();
    await expect(page.getByRole("button", { name: /PDF|HTML|CSV|JSON/i }).first()).toBeVisible();
    await shot(page, "05_export");
  });

  test("Missing ATT surfaces a non-blocking warning, not a hard error", async ({ page }) => {
    await page.goto(BASE_URL);
    await uploadZip(page, LENA_MISSING_ATT);
    await page.getByRole("button", { name: /upload.*(detect|continue)/i }).click();
    await page.getByRole("button", { name: /validate/i }).first().click();
    await expect(page.getByText(/ATT|arterial transit/i).first()).toBeVisible({ timeout: 60000 });
    // Continue must remain enabled (warning, not blocking error)
    const cont = page.getByRole("button", { name: /continue to (run|score)/i }).first();
    await expect(cont).toBeEnabled();
    await shot(page, "06_missing_att_warning");
  });

  test("Batch of three stays isolated", async ({ page }) => {
    await page.goto(BASE_URL);
    await uploadZip(page, LENA_BATCH);
    await page.getByRole("button", { name: /upload.*(detect|continue)/i }).click();
    await expect(page.getByText(/sub-001/i)).toBeVisible({ timeout: 30000 });
    await expect(page.getByText(/sub-003/i)).toBeVisible();
    await shot(page, "07_batch");
  });

  test("Start New clears the session", async ({ page }) => {
    await page.goto(BASE_URL);
    await uploadZip(page, LENA_SINGLE);
    await page.getByRole("button", { name: /upload.*(detect|continue)/i }).click();
    await expect(page.getByText(/ASL/i).first()).toBeVisible({ timeout: 30000 });
    page.on("dialog", (d) => d.accept());
    await page.getByRole("button", { name: /start new/i }).first().click();
    // Back on upload with a file chooser, no prior submission text
    await expect(page.locator('input[type="file"]').first()).toBeVisible();
    await shot(page, "08_start_new");
  });

  test("Narrow viewport keeps the primary action visible", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(BASE_URL);
    await uploadZip(page, LENA_SINGLE);
    const submit = page.getByRole("button", { name: /upload.*(detect|continue)/i });
    await expect(submit).toBeVisible();
    await shot(page, "09_narrow");
  });

  test("Refresh after validate restores state", async ({ page }) => {
    await page.goto(BASE_URL);
    await uploadZip(page, LENA_SINGLE);
    await page.getByRole("button", { name: /upload.*(detect|continue)/i }).click();
    await page.getByRole("button", { name: /validate/i }).first().click();
    await expect(page.getByText(/passed|complete|warning/i).first()).toBeVisible({ timeout: 60000 });
    await page.reload();
    // Session should restore to a validated state, not a blank upload
    await expect(page.getByText(/lena_01|ASL|validat/i).first()).toBeVisible({ timeout: 15000 });
    await shot(page, "10_refresh_restore");
  });
});
