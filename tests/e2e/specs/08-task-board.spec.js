// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  const workspace = page.locator('#workspace:not(.hidden)');
  const unlockButton = page.locator('#unlockForm button[type="submit"]');
  const locked = await page.evaluate(async () => {
    const response = await fetch('/api/state');
    const payload = await response.json();
    return Boolean(payload.locked);
  });

  if (!locked) {
    await expect(workspace).toBeVisible({ timeout: 10000 });
    return;
  }

  await page.fill('#password', 'free');
  await unlockButton.click();
  await expect(workspace).toBeVisible({ timeout: 10000 });
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

  test('engine control starts collapsed so the board stays in view', async ({ page }) => {
    await page.evaluate(() => localStorage.removeItem('clcod.sectionState'));
    await page.reload();
    await unlockToWorkspace(page);

    const engineStrip = page.locator('#engineStrip');
    const toggle = page.locator('.section-toggle[data-collapse-target="engineStrip"]');

    await expect(engineStrip).toHaveClass(/is-collapsed/);
    await expect(toggle).toHaveText('Expand');

    await toggle.click();

    await expect(engineStrip).not.toHaveClass(/is-collapsed/);
    await expect(toggle).toHaveText('Collapse');
  });

  test('task board hydrates from live state when tasks exist', async ({ page }) => {
    const totalTasks = await page.evaluate(async () => {
      const response = await fetch('/api/state');
      const payload = await response.json();
      return payload.tasks?.total || 0;
    });

    const cardCount = async () => page.locator('.task-board .task-card').count();

    if (totalTasks === 0) {
      await expect.poll(cardCount).toBe(0);
      return;
    }

    await expect.poll(cardCount, { timeout: 5000 }).toBeGreaterThan(0);
  });

  test('task board panel header says "Task Board"', async ({ page }) => {
    await expect(page.locator('.panel--tasks .panel__header h3')).toHaveText('Task Board');
  });

  test('task board panel hint mentions status types', async ({ page }) => {
    const hint = page.locator('.panel--tasks .panel__hint');
    await expect(hint).toContainText('pending');
    await expect(hint).toContainText('done');
  });

  test('transcript font controls persist the selected size', async ({ page }) => {
    await page.evaluate(() => localStorage.removeItem('clcod.transcriptFontScale'));
    await page.reload();
    await unlockToWorkspace(page);

    const transcript = page.locator('#transcript');

    await expect(transcript).toHaveAttribute('data-font-scale', '1');
    await page.click('#transcriptFontUp');
    await expect(transcript).toHaveAttribute('data-font-scale', '2');

    await page.reload();
    await unlockToWorkspace(page);
    await expect(page.locator('#transcript')).toHaveAttribute('data-font-scale', '2');
  });
});
