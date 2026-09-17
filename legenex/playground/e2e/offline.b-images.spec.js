// Images workspace: generate -> COMPLETE -> actions, edit/variation submits, upload, compare.
import { expect, test } from '@playwright/test';
import { axeCheck, expectPhase, gotoPage, login, pngBuffer, watchPage } from './helpers.js';

test('generate an image and use every result action', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'images');
  await page.fill('#image-prompt', 'e2e playwright red fox in snow');
  await page.locator('.chips-size .chip[data-value="512x512"]').click();
  await page.locator('[aria-label="Number of images"] .chip[data-value="2"]').click();
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#generate-btn'),
  ]);
  const body = req.postDataJSON();
  expect(body).toMatchObject({ kind: 't2i', prompt: 'e2e playwright red fox in snow', size: '512x512', n: 2, quality: 'standard', uncensored: true });
  expect(typeof body.seed).toBe('number');
  expect((await req.response()).status()).toBe(202);

  const jobs = page.locator('#ws-jobs');
  await expectPhase(jobs, 'COMPLETE');
  const viewer = page.locator('#viewer');
  const img = viewer.locator('img.media-img');
  await expect(img).toBeVisible();
  await expect.poll(() => img.evaluate((el) => el.naturalWidth)).toBeGreaterThan(0);
  const assetId = await page.locator('#session-results .result-tile.is-selected').getAttribute('data-asset');
  expect(assetId).toMatch(/^a_[0-9a-f]{24}$/);
  await axeCheck(page, 'images with a result');

  // Favourite
  await viewer.locator('[data-action="favourite"]').click();
  await expect(viewer.locator('[data-action="favourite"]')).toHaveAttribute('aria-pressed', 'true');
  // Rename
  await viewer.locator('[data-action="rename"]').click();
  const dlg = page.locator('dialog[open]');
  await dlg.locator('input').fill('E2E renamed fox');
  await dlg.getByRole('button', { name: 'Save' }).click();
  await expect(page.locator('#viewer-title')).toHaveText('E2E renamed fox');
  const saved = await (await page.request.get(`/api/media/assets/${assetId}`)).json();
  expect(saved).toMatchObject({ title: 'E2E renamed fox', favourite: true });
  // Download
  const dl = viewer.locator('a[download]').first();
  await expect(dl).toHaveAttribute('href', `/api/media/assets/${assetId}/file?download=1`);
  const file = await page.request.get(`/api/media/assets/${assetId}/file?download=1`);
  expect(file.status()).toBe(200);
  expect(file.headers()['content-type']).toContain('image/png');
  // Fullscreen (keyboard close)
  await viewer.locator('[data-action="fullscreen"]').click();
  await expect(page.locator('dialog.dialog-lightbox')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('dialog.dialog-lightbox')).toHaveCount(0);
  // Details drawer with recipe and lineage
  await viewer.locator('[data-action="details"]').click();
  const drawer = page.locator('dialog.drawer');
  await expect(drawer.getByRole('heading', { name: 'Recipe' })).toBeVisible();
  await expect(drawer).toContainText('e2e playwright red fox in snow');
  await expect(drawer.getByRole('heading', { name: 'Lineage' })).toBeVisible();
  await axeCheck(page, 'details drawer');
  await page.keyboard.press('Escape');
  // Reuse seed locks it
  await viewer.locator('[data-action="reuse-seed"]').click();
  await expect(page.getByRole('button', { name: 'Lock seed' })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Lock seed' }).click();

  // Variation submit (source = this image)
  await viewer.locator('[data-action="variation"]').click();
  await expect(page.getByRole('tab', { name: 'Variation' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#source-section .source-chip')).toContainText('E2E renamed fox');
  const [vreq] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#generate-btn'),
  ]);
  expect(vreq.postDataJSON()).toMatchObject({ kind: 'variation', source_id: assetId });
  expect((await vreq.response()).status()).toBe(202);

  // Edit submit: requires an instruction
  await viewer.locator('[data-action="edit"]').click();
  await expect(page.getByRole('tab', { name: 'Edit' })).toHaveAttribute('aria-selected', 'true');
  await page.click('#generate-btn');
  await expect(page.locator('.panel-foot .form-error')).toHaveText('Describe the edit you want.');
  await page.fill('#image-prompt', 'make the fox blue');
  const [ereq] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#generate-btn'),
  ]);
  const ebody = ereq.postDataJSON();
  expect(ebody).toMatchObject({ kind: 'edit', source_id: assetId, prompt: 'make the fox blue' });
  expect(ebody.strength).toBeGreaterThan(0);
  expect((await ereq.response()).status()).toBe(202);
  // The job reaches a terminal phase; a failure is shown in friendly words, never raw exception text.
  const editCard = page.locator(`#ws-jobs .job-card[data-job="${(await (await ereq.response()).json()).id}"]`);
  await expect(editCard.locator('.phase-badge')).toHaveAttribute('data-phase', /COMPLETE|FAILED/, { timeout: 60_000 });
  if (await editCard.locator('.phase-badge[data-phase="FAILED"]').count()) {
    await expect(editCard.locator('.callout-title')).not.toContainText(/Error:|Traceback/);
    await expect(editCard.getByRole('button', { name: 'Retry' })).toBeVisible();
  }

  // Re-prompt goes back to Generate with the recipe
  await page.locator(`#filmstrip [data-asset="${assetId}"] button`).click();
  await viewer.locator('[data-action="reprompt"]').click();
  await expect(page.getByRole('tab', { name: 'Generate' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#image-prompt')).toHaveValue('e2e playwright red fox in snow');
  await expect(page.locator('.chips-size .chip[data-value="512x512"]')).toHaveAttribute('aria-pressed', 'true');
  expect(problems).toEqual([]);
});

test('upload a source image, compare two images, open lineage', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'images');
  await page.getByRole('tab', { name: 'Edit' }).click();
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/media/upload')),
    page.locator('#source-section input[type="file"]').setInputFiles({ name: 'e2e-upload.png', mimeType: 'image/png', buffer: pngBuffer() }),
  ]);
  expect(res.status()).toBe(200);
  const up = await res.json();
  expect(up).toMatchObject({ type: 'image', operation: 'upload', title: 'e2e-upload' });
  expect(res.request().headers()['x-csrf-token']).toBeTruthy();
  await expect(page.locator('#source-section .source-chip')).toContainText('e2e-upload');
  await expect(page.locator('#viewer-title')).toHaveText('e2e-upload');

  // Compare: mark the upload, select another image, compare.
  const viewer = page.locator('#viewer');
  await viewer.locator('[data-action="compare"]').click();
  await expect(viewer.locator('[data-action="compare"]')).toHaveAttribute('aria-pressed', 'true');
  const other = page.locator(`#filmstrip .result-tile:not([data-asset="${up.id}"]) button`).first();
  await other.click();
  await viewer.locator('[data-action="compare"]').click();
  const cmp = page.locator('dialog[open]');
  await expect(cmp.getByRole('heading', { name: 'Compare' })).toBeVisible();
  await cmp.getByRole('slider', { name: 'Compare position' }).fill('20');
  await cmp.getByRole('button', { name: 'Side by side' }).click();
  await expect(cmp.locator('.compare-side')).toBeVisible();
  await axeCheck(page, 'compare dialog');
  await page.keyboard.press('Escape');
  await expect(page.locator('dialog[open]')).toHaveCount(0);

  // Library picker for the source
  await page.getByRole('button', { name: 'Choose from Library' }).click();
  const picker = page.locator('dialog[open]');
  await expect(picker.locator('.pick-tile').first()).toBeVisible();
  await picker.locator('.pick-tile').first().click();
  await expect(page.locator('#source-section .source-chip')).toBeVisible();
  expect(problems).toEqual([]);
});
