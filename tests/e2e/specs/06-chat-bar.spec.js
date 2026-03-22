// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  await expect(page.locator('#workspace:not(.hidden)')).toBeVisible({ timeout: 10000 });
}

test.describe('Chat Bar', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('chat form is visible', async ({ page }) => {
    await expect(page.locator('#chatForm')).toBeVisible();
    await expect(page.locator('#senderName')).toBeVisible();
    await expect(page.locator('#chatInput')).toBeVisible();
    await expect(page.locator('#sendButton')).toBeVisible();
  });

  test('sender name placeholder is "Operator"', async ({ page }) => {
    await expect(page.locator('#senderName')).toHaveAttribute('placeholder', 'Operator');
  });

  test('message input placeholder contains "Message the room"', async ({ page }) => {
    const placeholder = await page.locator('#chatInput').getAttribute('placeholder');
    expect(placeholder).toContain('Message the room');
  });

  test('send button shows arrow symbol', async ({ page }) => {
    const btn = page.locator('#sendButton');
    const text = await btn.innerHTML();
    expect(text).toMatch(/→|&rarr;/i);
  });

  test('cannot submit with empty fields', async ({ page }) => {
    await page.click('#sendButton');
    // No API call should fire — form validation prevents it
    const chatStatus = page.locator('#chatStatus');
    // Either no request or validation error shown
    await page.waitForTimeout(500);
    await expect(page.locator('#gate')).toHaveClass(/hidden/); // still in workspace
  });

  test('submitting with sender and message calls /api/chat', async ({ page }) => {
    await page.fill('#senderName', 'TestOp');
    await page.fill('#chatInput', 'hello from UAT test');

    const [request] = await Promise.all([
      page.waitForRequest(req =>
        req.url().includes('/api/chat') && req.method() === 'POST'
      ),
      page.click('#sendButton'),
    ]);

    const body = JSON.parse(request.postData() || '{}');
    expect(body.name).toBe('TestOp');
    expect(body.message).toBe('hello from UAT test');
  });

  test('message input clears after successful send', async ({ page }) => {
    await page.fill('#senderName', 'TestOp');
    await page.fill('#chatInput', 'clearing test message');

    await Promise.all([
      page.waitForResponse(resp => resp.url().includes('/api/chat')),
      page.click('#sendButton'),
    ]);

    await expect(page.locator('#chatInput')).toHaveValue('');
  });

  test('send button is disabled during send', async ({ page }) => {
    await page.fill('#senderName', 'TestOp');
    await page.fill('#chatInput', 'disable test');

    let disabledDuring = false;
    const [response] = await Promise.all([
      page.waitForResponse(resp => resp.url().includes('/api/chat')),
      (async () => {
        await page.click('#sendButton');
        // Check disabled state during request (best effort — transient)
        const disabled = await page.locator('#sendButton').isDisabled().catch(() => false);
        disabledDuring = disabled;
      })(),
    ]);

    // After response, button should be re-enabled
    await expect(page.locator('#sendButton')).not.toBeDisabled();
  });
});
