// @ts-check
const { test, expect } = require('@playwright/test');

test.describe('Theme Toggle', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('starts in dark theme by default', async ({ page }) => {
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  });

  test('toggles to light theme on click', async ({ page }) => {
    await page.click('#themeToggle');
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  });

  test('toggles back to dark theme on second click', async ({ page }) => {
    await page.click('#themeToggle');
    await page.click('#themeToggle');
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  });

  test('theme toggle button is always visible', async ({ page }) => {
    await expect(page.locator('#themeToggle')).toBeVisible();
  });

  test('theme persists across page sections (gate → unlocked)', async ({ page }) => {
    // Switch to light while on gate
    await page.click('#themeToggle');
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Unlock
    await page.fill('#password', 'free');
    await page.click('#unlockForm button[type="submit"]');
    await page.waitForTimeout(1000);

    // Still light
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  });
});
