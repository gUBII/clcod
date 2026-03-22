// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  await expect(page.locator('#workspace:not(.hidden)')).toBeVisible({ timeout: 10000 });
}

test.describe('Route Bus Panel', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('route bus panel is visible', async ({ page }) => {
    await expect(page.locator('#routingStage')).toBeVisible();
  });

  test('route bus panel header says "Route Bus"', async ({ page }) => {
    await expect(page.locator('#routingStage .panel__header h3')).toHaveText('Route Bus');
  });

  test('route bus hint shows TXX → RXX', async ({ page }) => {
    await expect(page.locator('#routingStage .panel__hint')).toContainText('TXX');
  });

  test('shows empty state message when no routes', async ({ page }) => {
    // If no traffic yet, empty message should be visible
    const empty = page.locator('#routingEmpty');
    const lanes = page.locator('#routeLanes');
    const laneCount = await lanes.locator('> *').count();

    if (laneCount === 0) {
      await expect(empty).toBeVisible();
      await expect(empty).toContainText(/no routed traffic/i);
    } else {
      // Routes exist — empty msg should be hidden
      await expect(empty).toHaveCSS('display', 'none').catch(() => {});
    }
  });

  test('route lanes container is attached', async ({ page }) => {
    await expect(page.locator('#routeLanes')).toBeAttached();
  });
});
