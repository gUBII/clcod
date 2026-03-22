// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  await expect(page.locator('#workspace:not(.hidden)')).toBeVisible({ timeout: 10000 });
}

test.describe('Task Board', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('task board has three columns: Pending, Active, Done', async ({ page }) => {
    await expect(page.locator('#tasksPending').locator('..').locator('.task-board__col-title')).toHaveText('Pending');
    await expect(page.locator('#tasksActive').locator('..').locator('.task-board__col-title')).toHaveText('Active');
    await expect(page.locator('#tasksDone').locator('..').locator('.task-board__col-title')).toHaveText('Done');
  });

  test('task board columns are visible', async ({ page }) => {
    await expect(page.locator('#tasksPending')).toBeVisible();
    await expect(page.locator('#tasksActive')).toBeVisible();
    await expect(page.locator('#tasksDone')).toBeVisible();
  });

  test('task board panel header says "Task Board"', async ({ page }) => {
    await expect(page.locator('.panel--tasks .panel__header h3')).toHaveText('Task Board');
  });

  test('task board panel hint mentions status types', async ({ page }) => {
    const hint = page.locator('.panel--tasks .panel__hint');
    await expect(hint).toContainText('pending');
    await expect(hint).toContainText('done');
  });
});
