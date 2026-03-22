// @ts-check
const { test, expect } = require('@playwright/test');

test.describe('Project Picker', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
  });

  test('project picker is visible in chrome header', async ({ page }) => {
    await expect(page.locator('#projectPicker')).toBeVisible();
    await expect(page.locator('#projectName')).toBeVisible();
  });

  test('dropdown is hidden by default', async ({ page }) => {
    await expect(page.locator('#projectDropdown')).toHaveClass(/hidden/);
  });

  test('clicking dropdown button shows project dropdown', async ({ page }) => {
    await page.click('#projectMenuBtn');
    await expect(page.locator('#projectDropdown')).not.toHaveClass(/hidden/);
  });

  test('project dropdown contains local path input', async ({ page }) => {
    await page.click('#projectMenuBtn');
    await expect(page.locator('#projectPathInput')).toBeVisible();
    await expect(page.locator('#lockPathBtn')).toBeVisible();
  });

  test('project dropdown contains clone URL input', async ({ page }) => {
    await page.click('#projectMenuBtn');
    await expect(page.locator('#projectUrlInput')).toBeVisible();
    await expect(page.locator('#cloneUrlBtn')).toBeVisible();
  });

  test('project dropdown contains unlock button', async ({ page }) => {
    await page.click('#projectMenuBtn');
    await expect(page.locator('#unlockProjectBtn')).toBeVisible();
    await expect(page.locator('#unlockProjectBtn')).toHaveText('Unlock (return home)');
  });

  test('clicking dropdown button again closes the dropdown', async ({ page }) => {
    await page.click('#projectMenuBtn');
    await expect(page.locator('#projectDropdown')).not.toHaveClass(/hidden/);
    await page.click('#projectMenuBtn');
    await expect(page.locator('#projectDropdown')).toHaveClass(/hidden/);
  });
});
