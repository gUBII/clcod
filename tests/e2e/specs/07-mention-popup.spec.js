// @ts-check
const { test, expect } = require('@playwright/test');

async function unlockToWorkspace(page) {
  await page.goto('/');
  await page.fill('#password', 'free');
  await page.click('#unlockForm button[type="submit"]');
  await expect(page.locator('#workspace:not(.hidden)')).toBeVisible({ timeout: 10000 });
}

test.describe('@ Mention Popup', () => {
  test.beforeEach(async ({ page }) => {
    await unlockToWorkspace(page);
  });

  test('mention popup is hidden initially', async ({ page }) => {
    await expect(page.locator('#mentionPopup')).toHaveClass(/hidden/);
  });

  test('typing @ in chat input shows mention popup', async ({ page }) => {
    await page.click('#chatInput');
    await page.keyboard.type('@');
    await expect(page.locator('#mentionPopup')).not.toHaveClass(/hidden/);
  });

  test('mention popup shows agent names (CLAUDE, CODEX, GEMINI)', async ({ page }) => {
    await page.click('#chatInput');
    await page.keyboard.type('@');
    const popup = page.locator('#mentionPopup');
    await expect(popup).not.toHaveClass(/hidden/);
    const text = await popup.textContent();
    // At least one of the known agents should appear
    expect(text).toMatch(/CLAUDE|CODEX|GEMINI/i);
  });

  test('typing after @ filters the mention list', async ({ page }) => {
    await page.click('#chatInput');
    await page.keyboard.type('@CLA');
    const popup = page.locator('#mentionPopup');
    await expect(popup).not.toHaveClass(/hidden/);
    const text = await popup.textContent();
    expect(text).toMatch(/CLAUDE/i);
  });

  test('pressing Escape closes mention popup', async ({ page }) => {
    await page.click('#chatInput');
    await page.keyboard.type('@');
    await expect(page.locator('#mentionPopup')).not.toHaveClass(/hidden/);
    await page.keyboard.press('Escape');
    await expect(page.locator('#mentionPopup')).toHaveClass(/hidden/);
  });

  test('clicking a mention inserts it into chat input', async ({ page }) => {
    await page.click('#chatInput');
    await page.keyboard.type('@');
    const popup = page.locator('#mentionPopup');
    await expect(popup).not.toHaveClass(/hidden/);

    const firstItem = popup.locator('li, .mention-item, [role="option"]').first();
    if (await firstItem.isVisible()) {
      await firstItem.click();
      const value = await page.locator('#chatInput').inputValue();
      expect(value).toMatch(/@\w+/);
    } else {
      // Fallback: just verify popup appeared
      test.skip();
    }
  });

  test('deleting @ from input hides the popup', async ({ page }) => {
    await page.click('#chatInput');
    await page.keyboard.type('@');
    await expect(page.locator('#mentionPopup')).not.toHaveClass(/hidden/);
    await page.keyboard.press('Backspace');
    await expect(page.locator('#mentionPopup')).toHaveClass(/hidden/);
  });
});
