// Shell: sign-in/out, every page clean (console, CSP, axe), theme, links, phone layout.
import { expect, test } from '@playwright/test';
import { PAGES, axeCheck, gotoPage, login, noHorizontalOverflow, watchPage } from './helpers.js';

test('wrong password is refused, then sign-in and sign-out work', async ({ page }) => {
  const problems = watchPage(page, { allow: [/status of 401/] });
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await axeCheck(page, 'sign-in');
  await page.fill('#login-user', 'admin');
  await page.fill('#login-pass', 'definitely-wrong');
  await page.click('#login-submit');
  await expect(page.locator('#login-error')).toContainText(/invalid username or password/i);
  await expect(page.locator('#app-view')).toBeHidden();

  await login(page);
  await expect(page.locator('#user-name')).toHaveText('admin');
  await page.click('#logout-btn');
  await expect(page.locator('#login-view')).toBeVisible();
  await expect(page.locator('#login-error')).toContainText('signed out');
  // The session is really gone server-side.
  const s = await page.request.get('/api/session');
  expect((await s.json()).authenticated).toBe(false);
  expect(problems).toEqual([]);
});

test('every page renders without console errors, CSP violations or axe violations (dark and light)', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  for (const theme of ['dark', 'light']) {
    const current = await page.evaluate(() => document.documentElement.dataset.theme);
    if (current !== theme) await page.click('#theme-btn');
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    for (const [name, heading] of PAGES) {
      await gotoPage(page, name);
      await expect(page.locator(`#page-${name} h1`)).toHaveText(heading);
      await page.waitForTimeout(400);
      await axeCheck(page, `${name} (${theme})`);
    }
  }
  // Theme persists across reloads.
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.click('#theme-btn');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  expect(problems).toEqual([]);
});

test('top bar: Control Center link, resource pill, activity tray, command bar', async ({ page, request }) => {
  const problems = watchPage(page);
  await login(page);
  const cfg = await (await request.get('/pg/config')).json();
  const cc = page.locator('#cc-link');
  await expect(cc).toBeVisible();
  await expect(cc).toHaveAttribute('href', cfg.control_center_url);
  await expect(cc).toHaveAttribute('target', '_blank');
  await expect(cc).toHaveAttribute('rel', /noopener/);
  await expect(page.locator('#res-pill .res-profile')).toHaveText(/Auto|Text|Media|Music|Max/);

  await page.click('#tray-btn');
  await expect(page.locator('#page-history h1')).toHaveText('History');

  // Command bar: keyboard shortcut, navigation by typing.
  await page.keyboard.press('Control+k');
  const dlg = page.locator('dialog.dialog-command');
  await expect(dlg).toBeVisible();
  await axeCheck(page, 'command bar');
  await dlg.locator('input').fill('library');
  await expect(dlg.locator('[role="option"]').nth(3)).toContainText('Search Library');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(dlg.locator('[aria-selected="true"]')).toHaveText('Open Library');
  await page.keyboard.press('Enter');
  await expect(page.locator('#page-library h1')).toHaveText('Library');
  expect(problems).toEqual([]);
});

test('keyboard: skip link and tab order reach the main content', async ({ page }) => {
  await login(page);
  await page.reload();
  await expect(page.locator('#app-view')).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.locator('.skip-link')).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main')).toBeFocused();
  await gotoPage(page, 'images');
  await expect(page.locator('#page-images h1')).toBeFocused();
});

test.describe('phone viewport', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test('bottom navigation and workspaces fit without horizontal overflow', async ({ page }) => {
    const problems = watchPage(page);
    await login(page);
    const rail = page.locator('#rail');
    await expect(rail).toBeVisible();
    const box = await rail.boundingBox();
    expect(box.y).toBeGreaterThan(700); // docked to the bottom
    await expect(page.locator('#rail a[data-page="music"]')).toBeVisible();
    await noHorizontalOverflow(page);
    for (const name of ['images', 'video', 'music', 'library', 'history', 'dashboard']) {
      await gotoPage(page, name);
      await page.waitForTimeout(300);
      await noHorizontalOverflow(page);
    }
    await gotoPage(page, 'images');
    await expect(page.locator('#image-prompt')).toBeVisible();
    await axeCheck(page, 'images (phone)');
    expect(problems).toEqual([]);
  });
});
