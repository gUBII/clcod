// @ts-check
const { test, expect } = require('@playwright/test');

async function unlock(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  // Wait for either engine room or workspace to appear
  await expect(
    page.locator('#engineRoom:not(.hidden), #workspace:not(.hidden)').first()
  ).toBeVisible({ timeout: 8000 });
}

test.describe('Post-Unlock UI', () => {
  test('gate hides after unlock', async ({ page }) => {
    await unlock(page);
    await expect(page.locator('#gate')).toHaveClass(/hidden/);
  });

  test('brand title is always visible', async ({ page }) => {
    await unlock(page);
    await expect(page.locator('.chrome__brand h1')).toHaveText("Farhan's Man Cave");
  });

  test('appPhase label is not LOCKED after unlock', async ({ page }) => {
    await unlock(page);
    const phase = await page.locator('#appPhase').textContent();
    expect(phase?.toLowerCase()).not.toBe('locked');
  });

  test('relay and tmux state indicators are present', async ({ page }) => {
    await unlock(page);
    // These are present in engine room view
    const engineRoom = page.locator('#engineRoom');
    if (await engineRoom.isVisible()) {
      await expect(page.locator('#relayState')).toBeVisible();
      await expect(page.locator('#tmuxState')).toBeVisible();
    } else {
      // In workspace view
      await expect(page.locator('#workspaceRelay')).toBeVisible();
      await expect(page.locator('#workspaceTmux')).toBeVisible();
    }
  });
});

test.describe('Engine Room (if visible)', () => {
  test.beforeEach(async ({ page }) => {
    await unlock(page);
  });

  test('engine cards section exists', async ({ page }) => {
    const engineRoom = page.locator('#engineRoom');
    if (await engineRoom.isVisible()) {
      await expect(page.locator('#engineCards')).toBeAttached();
    } else {
      test.skip();
    }
  });

  test('tmux command and copy button are present', async ({ page }) => {
    const engineRoom = page.locator('#engineRoom');
    if (await engineRoom.isVisible()) {
      await expect(page.locator('#tmuxCommand')).toBeVisible();
      await expect(page.locator('#copyTmux')).toBeVisible();
      await expect(page.locator('#copyTmux')).toHaveText('Copy tmux command');
    } else {
      test.skip();
    }
  });
});
