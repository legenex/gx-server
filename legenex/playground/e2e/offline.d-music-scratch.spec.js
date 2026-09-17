import { expect, test } from '@playwright/test';
import { gotoPage, login } from './helpers.js';

test('scratch improve 400', async ({ page }) => {
  page.on('response', async (r) => {
    if (r.status() >= 400) {
      let b = ''; try { b = (await r.text()).slice(0, 400); } catch { b = '?'; }
      let post = ''; try { post = r.request().postData() || ''; } catch { post = ''; }
      console.log(`SCRATCH400 ${r.status()} ${r.url()} POST=${post.slice(0, 500)} BODY=${b}`);
    }
  });
  await login(page);
  await gotoPage(page, 'music');
  const form = page.locator('#form-create');
  await expect(form.locator('#music-conditioning')).toBeAttached();
  await page.fill('#music-description', 'a song about rain');
  await page.fill('#music-prompt', 'my exact style words');
  await form.getByRole('button', { name: 'Keep Style prompt when using AI' }).click();
  const input = form.locator('#music-tags').getByLabel('Add a style tag');
  await input.fill('lo-fi');
  await input.press('Enter');
  await form.getByLabel('BPM', { exact: true }).fill('90');
  await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/ai/improve')),
    page.click('#music-ai-improve'),
  ]);
  await expect(page.locator('.ai-status')).toContainText('Improved');
  await page.waitForTimeout(4000);
});
