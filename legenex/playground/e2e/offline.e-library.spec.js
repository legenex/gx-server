// Library: search, filters, sort, selection, bulk actions, ZIP, lineage, delete.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, pngBuffer, watchPage } from './helpers.js';

async function uploadTestImage(page, title) {
  const session = await (await page.request.get('/api/session')).json();
  const base = new URL(page.url()).origin;
  const res = await page.request.post('/api/media/upload', {
    headers: { 'Content-Type': 'image/png', 'X-CSRF-Token': session.csrf, 'X-Title': encodeURIComponent(title), Origin: base },
    data: pngBuffer(40, 40),
  });
  expect(res.status()).toBe(200);
  return res.json();
}

test('search, filter, sort, select, bulk favourite, ZIP, lineage and delete', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const asset = await uploadTestImage(page, 'E2E delete me');
  await gotoPage(page, 'library');
  const results = page.locator('#lib-results');
  await expect(results.locator('.lib-item').first()).toBeVisible();
  await expect(page.locator('#lib-counts')).toContainText(/images? · \d+ videos? · \d+ tracks?/);
  await axeCheck(page, 'library grid');

  // Search
  const [sreq] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/api/media/assets?') && r.url().includes('q=E2E+delete+me')),
    page.fill('#lib-search', 'E2E delete me'),
  ]);
  expect(sreq).toBeTruthy();
  await expect(results.locator('.lib-item')).toHaveCount(1);
  await expect(results.locator('.lib-title')).toHaveText('E2E delete me');
  await page.fill('#lib-search', '');

  // Filter: audio only
  await page.locator('.chips-type .chip[data-value="audio"]').click();
  await expect(results.locator('.lib-item').first()).toHaveClass(/lib-type-audio/);
  await expect(results.locator('.lib-item:not(.lib-type-audio)')).toHaveCount(0);
  await expect(results.locator('.lib-item audio').first()).toBeAttached();
  await page.locator('.chips-type .chip[data-value=""]').click();
  await expect(results.locator('.lib-item:not(.lib-type-audio)').first()).toBeVisible();

  // Sort
  const [sortReq] = await Promise.all([
    page.waitForRequest((r) => r.url().includes('/api/media/assets?') && r.url().includes('sort=title')),
    page.selectOption('#lib-sort', 'title'),
  ]);
  expect(sortReq).toBeTruthy();
  await page.selectOption('#lib-sort', 'newest');
  await expect(results.locator('.lib-item').first()).toBeVisible();

  // Selection: one, unselect, all, clear
  const items = results.locator('.lib-item');
  const count = await items.count();
  expect(count).toBeGreaterThan(1);
  const first = items.nth(0).locator('input.item-check');
  await first.check();
  await expect(page.locator('.sel-count')).toHaveText('1 item selected');
  await expect(page.locator('#bulk-delete')).toBeEnabled();
  await first.uncheck();
  await expect(page.locator('.sel-count')).toHaveText('Nothing selected');
  await expect(page.locator('#bulk-delete')).toBeDisabled();
  await page.locator('#lib-select-all').check();
  await expect(page.locator('.sel-count')).toHaveText(`${count} items selected`);
  await expect(results.locator('input.item-check:checked')).toHaveCount(count);
  await page.click('#bulk-clear');
  await expect(page.locator('.sel-count')).toHaveText('Nothing selected');
  await expect(results.locator('input.item-check:checked')).toHaveCount(0);

  // Bulk favourite two items (keyboard: Space toggles a checkbox)
  await items.nth(0).locator('input.item-check').focus();
  await page.keyboard.press('Space');
  await items.nth(1).locator('input.item-check').check();
  await expect(page.locator('.sel-count')).toHaveText('2 items selected');
  const ids = await Promise.all([0, 1].map((i) => items.nth(i).getAttribute('data-asset')));
  await page.click('#bulk-fav');
  for (const id of ids) {
    await expect(results.locator(`[data-asset="${id}"] [data-action="favourite"]`)).toHaveAttribute('aria-pressed', 'true');
    expect((await (await page.request.get(`/api/media/assets/${id}`)).json()).favourite).toBe(true);
  }
  // Favourites filter
  await page.getByText('Favourites only').click();
  await expect(results.locator('[data-action="favourite"][aria-pressed="false"]')).toHaveCount(0);
  await page.getByText('Favourites only').click();

  // Bulk ZIP
  const [zipRes] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/media/zip') && r.request().method() === 'POST'),
    page.click('#bulk-zip'),
  ]);
  expect(zipRes.status()).toBe(200);
  const zip = await zipRes.json();
  expect(zip.url).toMatch(/^\/api\/media\/zip\/[A-Za-z0-9_-]+$/);
  expect(zip.count).toBe(2);
  // Bulk unfavourite
  await page.click('#bulk-unfav');
  for (const id of ids) {
    await expect(results.locator(`[data-asset="${id}"] [data-action="favourite"]`)).toHaveAttribute('aria-pressed', 'false');
  }
  await page.click('#bulk-clear');

  // List view
  await page.getByRole('button', { name: 'List view' }).click();
  await expect(results.locator('.lib-list')).toBeVisible();
  await axeCheck(page, 'library list');
  await page.getByRole('button', { name: 'Grid view' }).click();

  // Lineage drawer
  const card = results.locator(`[data-asset="${asset.id}"]`);
  await card.locator('[data-action="lineage"]').click();
  const drawer = page.locator('dialog.drawer');
  await expect(drawer.getByRole('heading', { name: /Lineage/ })).toBeVisible();
  await expect(drawer).toContainText(/no parent|Upload/);
  await page.keyboard.press('Escape');
  await expect(page.locator('dialog[open]')).toHaveCount(0);

  // Delete via dialog (cancel first, then confirm)
  await card.locator('[data-action="delete"]').click();
  const dlg = page.locator('dialog[open]');
  await expect(dlg.getByRole('heading', { name: 'Delete this item?' })).toBeVisible();
  await axeCheck(page, 'delete dialog');
  await dlg.getByRole('button', { name: 'Cancel' }).click();
  await expect(card).toBeVisible();
  await card.locator('[data-action="delete"]').click();
  const [del] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/delete')),
    page.locator('dialog[open] [data-role="confirm-ok"]').click(),
  ]);
  expect(del.postDataJSON()).toEqual({ ids: [asset.id], confirm: true });
  await expect(card).toHaveCount(0);
  expect((await page.request.get(`/api/media/assets/${asset.id}`)).status()).toBe(404);
  expect(problems).toEqual([]);
});

test('bulk delete of selected test assets and open-in-workspace routing', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const a = await uploadTestImage(page, 'E2E bulk one');
  const b = await uploadTestImage(page, 'E2E bulk two');
  await gotoPage(page, 'library');
  await page.fill('#lib-search', 'E2E bulk');
  const results = page.locator('#lib-results');
  await expect(results.locator('.lib-item')).toHaveCount(2);
  await page.locator('#lib-select-all').check();
  await page.click('#bulk-delete');
  await expect(page.locator('dialog[open]').getByRole('heading', { name: 'Delete 2 items?' })).toBeVisible();
  await page.locator('dialog[open] [data-role="confirm-ok"]').click();
  await expect(results.getByText('Nothing matches these filters')).toBeVisible();
  for (const x of [a, b]) expect((await page.request.get(`/api/media/assets/${x.id}`)).status()).toBe(404);

  // Open a seeded track in the Music workspace.
  await page.fill('#lib-search', 'Seeded loop');
  await results.locator('[data-action="open"]').first().click();
  await expect(page.locator('#page-music h1')).toHaveText('Music');
  await expect(page.locator('.track-card.is-focus')).toContainText('Seeded loop');
  expect(problems).toEqual([]);
});
