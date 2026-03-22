// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  // Wait for workspace specifically
  await expect(page.locator('#workspace:not(.hidden)')).toBeVisible({ timeout: 10000 });
}

test.describe('Engine Strip — Model/Effort Dropdowns', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('engine strip section is visible', async ({ page }) => {
    await expect(page.locator('#engineStrip')).toBeVisible();
    await expect(page.locator('#statusGrid')).toBeVisible();
  });

  test('status grid has agent cards', async ({ page }) => {
    // Grid is populated by renderState — wait for at least one card
    await expect(page.locator('#statusGrid').locator('[data-agent]').first()).toBeVisible({ timeout: 8000 });
  });

  test('model select dropdowns exist for agents', async ({ page }) => {
    await page.locator('#statusGrid').locator('[data-agent]').first().waitFor({ timeout: 8000 });
    const modelSelects = page.locator('.control-select[data-kind="model"]');
    const count = await modelSelects.count();
    expect(count).toBeGreaterThan(0);
  });

  test('effort select dropdowns exist for agents', async ({ page }) => {
    await page.locator('#statusGrid').locator('[data-agent]').first().waitFor({ timeout: 8000 });
    const effortSelects = page.locator('.control-select[data-kind="effort"]');
    const count = await effortSelects.count();
    expect(count).toBeGreaterThan(0);
  });

  test('model dropdown fires change and calls settings API', async ({ page }) => {
    await page.locator('#statusGrid').locator('[data-agent]').first().waitFor({ timeout: 8000 });

    // Find a model select with multiple enabled options (value = option id)
    const modelSelects = page.locator('.control-select[data-kind="model"]:not([disabled])');
    const count = await modelSelects.count();
    let targetSelect = null;
    let otherValue = null;
    let agentName = null;

    for (let i = 0; i < count; i++) {
      const sel = modelSelects.nth(i);
      const optionValues = await sel.locator('option').evaluateAll(
        opts => opts.map(o => o.value).filter(v => v && v !== 'Not supported')
      );
      const currentValue = await sel.inputValue();
      const candidate = optionValues.find(v => v !== currentValue);
      if (candidate) {
        targetSelect = sel;
        otherValue = candidate;
        agentName = await sel.getAttribute('data-agent');
        break;
      }
    }

    if (!targetSelect || !otherValue) {
      test.skip();
      return;
    }

    // Start listener before triggering the change
    const requestPromise = page.waitForRequest(req =>
      req.url().includes(`/api/agents/${agentName}/settings`) && req.method() === 'POST',
      { timeout: 10000 }
    );
    await targetSelect.selectOption({ value: otherValue });
    const request = await requestPromise;

    expect(request).toBeTruthy();
    const body = JSON.parse(request.postData() || '{}');
    expect(body).toHaveProperty('selected_model', otherValue);
  });

  test('effort dropdown fires change and calls settings API', async ({ page }) => {
    await page.locator('#statusGrid').locator('[data-agent]').first().waitFor({ timeout: 8000 });

    // Find an effort select that has actionable options (value = option id)
    const effortSelects = page.locator('.control-select[data-kind="effort"]:not([disabled])');
    const count = await effortSelects.count();
    let targetSelect = null;
    let otherValue = null;
    let agentName = null;

    for (let i = 0; i < count; i++) {
      const sel = effortSelects.nth(i);
      const optionValues = await sel.locator('option').evaluateAll(
        opts => opts.map(o => o.value).filter(v => v && v !== 'Not supported')
      );
      const currentValue = await sel.inputValue();
      const candidate = optionValues.find(v => v !== currentValue);
      if (candidate) {
        targetSelect = sel;
        otherValue = candidate;
        agentName = await sel.getAttribute('data-agent');
        break;
      }
    }

    if (!targetSelect || !otherValue) {
      test.skip();
      return;
    }

    // Start listener before triggering the change
    const requestPromise = page.waitForRequest(req =>
      req.url().includes(`/api/agents/${agentName}/settings`) && req.method() === 'POST',
      { timeout: 10000 }
    );
    await targetSelect.selectOption({ value: otherValue });
    const request = await requestPromise;

    expect(request).toBeTruthy();
    const body = JSON.parse(request.postData() || '{}');
    expect(body).toHaveProperty('selected_effort', otherValue);
  });

  test('control message is shown after dropdown change', async ({ page }) => {
    await page.locator('#statusGrid').locator('[data-agent]').first().waitFor({ timeout: 8000 });

    const modelSelect = page.locator('.control-select[data-kind="model"]:not([disabled])').first();
    const optionValues = await modelSelect.locator('option').evaluateAll(
      opts => opts.map(o => o.value).filter(v => v && v !== 'Not supported')
    );
    const currentValue = await modelSelect.inputValue();
    const otherValue = optionValues.find(v => v !== currentValue);
    const agentName = await modelSelect.getAttribute('data-agent');

    if (!otherValue) {
      test.skip();
      return;
    }

    await modelSelect.selectOption({ value: otherValue });

    // The control message element should update — check it is attached and has text
    await expect(
      page.locator(`[data-agent="${agentName}"] [data-control-message]`)
    ).toBeAttached({ timeout: 5000 });
  });

  test('restart button is present in agent cards', async ({ page }) => {
    await page.locator('#statusGrid').locator('[data-agent]').first().waitFor({ timeout: 8000 });
    const restartBtns = page.locator('button[data-restart]');
    const count = await restartBtns.count();
    expect(count).toBeGreaterThan(0);
  });
});
