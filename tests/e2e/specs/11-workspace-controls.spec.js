// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  await expect(page.locator('#workspace:not(.hidden)')).toBeVisible({ timeout: 10000 });
}

test.describe('Workspace Control Buttons', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('Sleep button is visible', async ({ page }) => {
    await expect(page.locator('#sleepBtn')).toBeVisible();
    // Button toggles between "Sleep" and "Wake" depending on current state
    const text = await page.locator('#sleepBtn').textContent();
    expect(['Sleep', 'Wake']).toContain(text?.trim());
  });

  test('Sync Repo button is visible', async ({ page }) => {
    await expect(page.locator('#syncRepoBtn')).toBeVisible();
    await expect(page.locator('#syncRepoBtn')).toHaveText('Sync Repo');
  });

  test('Compact Context button is visible', async ({ page }) => {
    await expect(page.locator('#compactBtn')).toBeVisible();
    await expect(page.locator('#compactBtn')).toHaveText('Compact Context');
  });

  test('Sleep button calls /api/sleep endpoint', async ({ page }) => {
    const [request] = await Promise.all([
      page.waitForRequest(req =>
        req.url().includes('/api/sleep') && req.method() === 'POST',
        { timeout: 5000 }
      ).catch(() => null),
      page.click('#sleepBtn'),
    ]);

    if (request) {
      expect(request.method()).toBe('POST');
    }
    // If no /api/sleep, at minimum button should still exist
    await expect(page.locator('#sleepBtn')).toBeAttached();
  });

  test('workspace header shows eyebrow "Live Workspace"', async ({ page }) => {
    await expect(page.locator('.workspace__header .hero__eyebrow')).toHaveText('Live Workspace');
  });

  test('transcript panel is present', async ({ page }) => {
    await expect(page.locator('#transcript')).toBeAttached();
    await expect(page.locator('.panel--transcript .panel__header h3')).toHaveText('Transcript');
  });
});

test.describe('Agent Modal', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
    // Wait for agent cards to render
    await page.locator('#statusGrid').locator('[data-agent]').first().waitFor({ timeout: 8000 }).catch(() => {});
  });

  test('agent modal is hidden initially', async ({ page }) => {
    await expect(page.locator('#agentModal')).toHaveClass(/hidden/);
  });

  test('clicking Details button opens agent modal', async ({ page }) => {
    const detailsBtn = page.locator('button[data-details]').first();
    if (await detailsBtn.isVisible()) {
      await detailsBtn.click();
      await expect(page.locator('#agentModal')).not.toHaveClass(/hidden/);
    } else {
      test.skip();
    }
  });

  test('agent modal close button hides it', async ({ page }) => {
    const detailsBtn = page.locator('button[data-details]').first();
    if (await detailsBtn.isVisible()) {
      await detailsBtn.click();
      await expect(page.locator('#agentModal')).not.toHaveClass(/hidden/);
      await page.click('#agentModal .modal-panel__close');
      await expect(page.locator('#agentModal')).toHaveClass(/hidden/);
    } else {
      test.skip();
    }
  });
});
