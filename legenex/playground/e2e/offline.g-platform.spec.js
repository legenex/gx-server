// Platform (Build V3, PLT): grouped navigation, Models, Logs, Settings,
// preferences, the HTTPS helper and the phone menus.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, noHorizontalOverflow, watchPage } from './helpers.js';

test('navigation is grouped under Create, Realtime and Manage with headings', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const create = page.locator('.rail-group[data-group="create"]');
  const manage = page.locator('.rail-group[data-group="manage"]');
  await expect(create.getByRole('heading', { name: 'Create' })).toBeVisible();
  await expect(manage.getByRole('heading', { name: 'Manage' })).toBeVisible();
  for (const name of ['dashboard', 'images', 'video', 'music']) await expect(create.locator(`a[data-page="${name}"]`)).toBeVisible();
  for (const name of ['library', 'history', 'models', 'logs', 'settings']) await expect(manage.locator(`a[data-page="${name}"]`)).toBeVisible();
  // a group without pages is hidden, never an empty heading
  const realtime = page.locator('.rail-group[data-group="realtime"]');
  const realtimeLinks = await realtime.locator('.rail-link').count();
  if (realtimeLinks === 0) await expect(realtime).toBeHidden();
  else await expect(realtime.getByRole('heading', { name: 'Realtime' })).toBeVisible();
  await gotoPage(page, 'models');
  await expect(manage).toHaveClass(/has-current/);
  // the dashboard offers the new areas as real links only
  await gotoPage(page, 'dashboard');
  const explore = page.locator('.explore-groups');
  await expect(explore.getByRole('link', { name: /Models/ })).toHaveAttribute('href', '#/models');
  await expect(explore.getByRole('link', { name: /Settings/ })).toHaveAttribute('href', '#/settings');
  await explore.getByRole('link', { name: /Logs/ }).click();
  await expect(page.locator('#page-logs h1')).toHaveText('Logs');
  expect(problems).toEqual([]);
});

test('Models lists every alias with state, memory and repository, filters by group', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'models');
  const grid = page.locator('#model-grid');
  await expect(grid.locator('.model-card')).toHaveCount(11);
  for (const alias of ['gx-mini', 'gx-image', 'gx-video', 'gx-music', 'gx-voice', 'gx-call', 'gx-live']) {
    await expect(grid.getByRole('heading', { name: alias, exact: true })).toBeVisible();
  }
  const music = grid.locator('.model-card', { has: page.getByRole('heading', { name: 'gx-music', exact: true }) });
  await expect(music).toContainText('ACE-Step/acestep-v15-xl-turbo @ d4a0b288b83e');
  await expect(music.getByRole('link', { name: /Manage gx-music in the Control Center/ })).toHaveAttribute('target', '_blank');
  // services that were not measured never show an invented number
  const call = grid.locator('.model-card', { has: page.getByRole('heading', { name: 'gx-call', exact: true }) });
  const callText = await call.textContent();
  expect(callText).not.toMatch(/\d+(\.\d+)? GiB loaded/);
  const pageEl = page.locator('#page-models');
  await pageEl.getByRole('button', { name: 'Realtime', exact: true }).click();
  await expect(grid.locator('.model-card')).toHaveCount(2);
  await pageEl.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(grid.locator('.model-card')).toHaveCount(4);
  const image = grid.locator('.model-card', { has: page.getByRole('heading', { name: 'gx-image', exact: true }) });
  await image.getByText(/^Components/).click();
  // A card can show both "Model variants" and "Components", and both render a
  // .model-components list, so target this one by its kind rather than by order.
  await expect(image.locator('[data-kind="components"] li').first()).toBeVisible();
  // gx-image now also publishes its selectable variants (IMG, registry).
  await image.getByText(/^Model variants/).click();
  const variants = image.locator('[data-kind="variants"] li');
  await expect(variants.first()).toBeVisible();
  await expect(variants).toHaveCount(3);
  const text = await page.locator('#page-models').textContent();
  expect(text).not.toContain('/srv/');
  expect(text).not.toMatch(/Bearer|sk-[A-Za-z0-9]{8}/);
  await axeCheck(page, 'models');
  expect(problems).toEqual([]);
});

test('Logs shows this user\'s activity, filters it and downloads what is shown', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'logs');
  const list = page.locator('#log-list');
  const logs = page.locator('#page-logs');
  await expect(list.locator('.log-row').first()).toBeVisible();
  await expect(list.locator('.log-row', { hasText: 'login' }).first()).toBeVisible();
  await logs.getByRole('button', { name: 'Errors', exact: true }).click();
  await expect(list).toHaveAttribute('aria-busy', 'false');
  const statuses = await list.locator('.log-row').evaluateAll((rows) => rows.map((r) => r.dataset.status));
  expect(statuses.every((s) => s === 'failed')).toBe(true);
  await logs.getByRole('button', { name: 'All', exact: true }).click();
  await logs.getByLabel('Search', { exact: true }).fill('login');
  await expect(list.locator('.log-row').first()).toContainText('login');
  await logs.getByLabel('Source', { exact: true }).selectOption('account');
  await expect(list.locator('.log-row').first()).toHaveAttribute('data-kind', 'account');
  const download = page.waitForEvent('download');
  await logs.getByRole('button', { name: 'Download', exact: true }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^gx-playground-logs-.*\.json$/);
  await axeCheck(page, 'logs');
  expect(problems).toEqual([]);
});

test('Settings saves preferences on the server and applies them', async ({ page, request }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'settings');
  await expect(page.getByRole('heading', { name: 'Secure connection (HTTPS)' })).toBeVisible();
  // 127.0.0.1 is a secure context for browsers
  await expect(page.locator('#https')).toContainText('This page is a secure context');
  await expect(page.getByRole('heading', { name: 'API access' })).toBeVisible();
  await expect(page.locator('#page-settings')).not.toContainText(/sk-[A-Za-z0-9]{8}/);
  await page.getByRole('group', { name: 'Theme' }).getByLabel('Light').check();
  await page.getByRole('group', { name: 'Motion' }).getByLabel('Reduce motion').check();
  await page.getByLabel('Video size').selectOption({ index: 1 });
  const videoSize = await page.getByLabel('Video size').inputValue();
  await page.click('#settings-save');
  await expect(page.locator('#settings-status')).toHaveText('Settings saved.');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await expect(page.locator('html')).toHaveAttribute('data-motion', 'reduce');
  await axeCheck(page, 'settings (light)');
  // stored per user on the server, applied again after a reload
  await page.reload();
  await expect(page.locator('#app-view')).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  const prefs = await page.evaluate(async () => (await (await fetch('/api/preferences')).json()).preferences);
  expect(prefs).toMatchObject({ theme: 'light', reduced_motion: 'reduce', default_video_size: videoSize });
  // and used by the Video page as its starting size
  await gotoPage(page, 'video');
  await expect(page.locator('.chips-size [aria-pressed="true"]').first()).toContainText(videoSize.replace('x', '×'));
  // nothing changed -> nothing sent
  await gotoPage(page, 'settings');
  await page.click('#settings-save');
  await expect(page.locator('#settings-status')).toHaveText('Nothing changed.');
  // restore
  await page.getByRole('group', { name: 'Theme' }).getByLabel('Dark').check();
  await page.getByRole('group', { name: 'Motion' }).getByLabel('Match this device').check();
  await page.getByLabel('Video size').selectOption('');
  await page.click('#settings-save');
  await expect(page.locator('#settings-status')).toHaveText('Settings saved.');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  // invalid values are refused by the server
  const bad = await request.post('/api/preferences', { data: { preferences: { theme: 'neon' } } });
  expect(bad.status()).toBe(403); // no CSRF token from a bare request
  expect(problems).toEqual([]);
});

test('the HTTPS help link focuses the secure connection section', async ({ page }) => {
  await login(page);
  await page.goto('/#/settings?focus=https');
  await expect(page.getByRole('heading', { name: 'Secure connection (HTTPS)' })).toBeFocused();
});

test('security headers: app document may use the microphone, API responses may not', async ({ request }) => {
  const doc = await request.get('/');
  expect(doc.headers()['permissions-policy']).toBe('camera=(self), microphone=(self), geolocation=(), payment=(), usb=()');
  expect(doc.headers()['content-security-policy']).toContain("connect-src 'self'");
  const api = await request.get('/api/session');
  expect(api.headers()['permissions-policy']).toBe('camera=(), microphone=(), geolocation=(), payment=(), usb=()');
  const cfg = await (await request.get('/pg/config')).json();
  expect(cfg.realtime.enabled).toBe(true);
  expect(cfg).not.toHaveProperty('authorization');
});

test.describe('phone viewport', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test('group menus open from the bottom bar, work with the keyboard and fit the screen', async ({ page }) => {
    const problems = watchPage(page);
    await login(page);
    const manageBtn = page.locator('.rail-group[data-group="manage"] .rail-group-btn');
    await expect(manageBtn).toHaveAttribute('aria-expanded', 'false');
    await manageBtn.click();
    await expect(manageBtn).toHaveAttribute('aria-expanded', 'true');
    const models = page.locator('#rail a[data-page="models"]');
    await expect(models).toBeVisible();
    // no page of this group is open, so focus goes to its first link
    await expect(page.locator('#rail a[data-page="library"]')).toBeFocused();
    await axeCheck(page, 'manage menu (phone)');
    await page.keyboard.press('Escape');
    await expect(manageBtn).toHaveAttribute('aria-expanded', 'false');
    await expect(manageBtn).toBeFocused();
    await expect(models).toBeHidden();
    for (const name of ['models', 'logs', 'settings']) {
      await gotoPage(page, name);
      await expect(page.locator(`#rail a[data-page="${name}"]`)).toBeHidden();
      await expect(manageBtn).toHaveAttribute('aria-expanded', 'false');
      await page.waitForTimeout(300);
      await noHorizontalOverflow(page);
      await axeCheck(page, `${name} (phone)`);
    }
    // clicking outside closes an open menu
    const createBtn = page.locator('.rail-group[data-group="create"] .rail-group-btn');
    await createBtn.click();
    await expect(page.locator('#rail a[data-page="images"]')).toBeVisible();
    await page.locator('#page-settings h1').click();
    await expect(createBtn).toHaveAttribute('aria-expanded', 'false');
    expect(problems).toEqual([]);
  });
});
