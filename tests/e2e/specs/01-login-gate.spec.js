// @ts-check
const { test, expect } = require('@playwright/test');

const PASSWORD = 'free';
const WRONG_PASSWORD = 'wrongpassword';

test.describe('Login Gate', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('shows gate on first load', async ({ page }) => {
    await expect(page).toHaveTitle("Farhan's Man Cave");
    await expect(page.locator('#gate')).toBeVisible();
    await expect(page.locator('#engineRoom')).toHaveClass(/hidden/);
    await expect(page.locator('#workspace')).toHaveClass(/hidden/);
  });

  test('shows password input and Ignite button', async ({ page }) => {
    await expect(page.locator('#password')).toBeVisible();
    await expect(page.locator('#unlockForm button[type="submit"]')).toHaveText('Ignite');
  });

  test('shows error on wrong password', async ({ page }) => {
    await page.fill('#password', WRONG_PASSWORD);
    await page.click('#unlockForm button[type="submit"]');
    await expect(page.locator('#unlockError')).toBeVisible();
    await expect(page.locator('#unlockError')).toContainText(/incorrect password/i);
  });

  test('error is hidden on page load', async ({ page }) => {
    const error = page.locator('#unlockError');
    await expect(error).toBeHidden();
  });

  test('unlocks with correct password and shows engine room or workspace', async ({ page }) => {
    await page.fill('#password', PASSWORD);
    await page.click('#unlockForm button[type="submit"]');
    // After unlock, either engine room or workspace becomes visible
    await expect(
      page.locator('#engineRoom:not(.hidden), #workspace:not(.hidden)')
    ).toBeVisible({ timeout: 10000 });
    await expect(page.locator('#gate')).toHaveClass(/hidden/);
  });

  test('appPhase label changes after unlock', async ({ page }) => {
    const initialPhase = await page.locator('#appPhase').textContent();
    await page.fill('#password', PASSWORD);
    await page.click('#unlockForm button[type="submit"]');
    await page.waitForTimeout(1000);
    const newPhase = await page.locator('#appPhase').textContent();
    // Phase should have changed from the locked state
    expect(newPhase).not.toBe('LOCKED');
  });
});
