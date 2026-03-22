// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  await expect(page.locator('#workspace:not(.hidden)')).toBeVisible({ timeout: 10000 });
}

test.describe('Dispatcher Bar', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('dispatcher bar is visible in workspace', async ({ page }) => {
    await expect(page.locator('.dispatcher-bar')).toBeVisible();
  });

  test('dispatcher bar shows Routes stat', async ({ page }) => {
    await expect(page.locator('#dispatcherRoutes')).toBeVisible();
  });

  test('dispatcher bar shows Absorbed stat', async ({ page }) => {
    await expect(page.locator('#dispatcherAbsorbs')).toBeVisible();
  });

  test('dispatcher bar shows Tokens saved stat', async ({ page }) => {
    await expect(page.locator('#dispatcherTokens')).toBeVisible();
  });

  test('dispatcher label is present', async ({ page }) => {
    await expect(page.locator('#dispatcherLabel')).toBeVisible();
    await expect(page.locator('#dispatcherLabel')).toContainText(/dispatcher/i);
  });

  test('dispatcher dot indicator is visible', async ({ page }) => {
    await expect(page.locator('#dispatcherDot')).toBeAttached();
  });
});

test.describe('Dispatcher Modal', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('dispatcher modal is hidden initially', async ({ page }) => {
    await expect(page.locator('#dispatcherModal')).toHaveClass(/hidden/);
  });

  test('clicking dispatcher bar opens health modal', async ({ page }) => {
    await page.click('.dispatcher-bar');
    await expect(page.locator('#dispatcherModal')).not.toHaveClass(/hidden/);
  });

  test('dispatcher modal has header "Dispatcher Health"', async ({ page }) => {
    await page.click('.dispatcher-bar');
    await expect(page.locator('#dispatcherModal .modal-panel__header h3')).toHaveText('Dispatcher Health');
  });

  test('dispatcher modal close button works', async ({ page }) => {
    await page.click('.dispatcher-bar');
    await expect(page.locator('#dispatcherModal')).not.toHaveClass(/hidden/);
    await page.click('#dispatcherModal .modal-panel__close');
    await expect(page.locator('#dispatcherModal')).toHaveClass(/hidden/);
  });
});
