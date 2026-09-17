// LIVE image acceptance (Build V3, IMG) through the deployed GX-Playground:
// REAL generations and edits on gx10-02, driven through the browser UI.
//   GX_EVIDENCE_DIR=/srv/logs/acceptance/build-v3/img/live GX_IMG_SOURCE=/path/source.png \
//     npx playwright test --project=live e2e/live.images-models.spec.js
// Signs in with the loopback-only acceptance account. 1) generates with Qwen
// Image 2512, 2) switches the model in the UI and generates with
// VisionmasterPro_V3, 3) uploads a known source and applies three different
// edits (background, style, clothing) plus a masked edit, 4) checks that the
// Library records the model of every result. Results are KEPT in the Library
// (titles start with "IMG acceptance") and downloaded to the evidence dir;
// image_eval.py computes the before/after metrics afterwards.
import { expect, test } from '@playwright/test';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { axeCheck, gotoPage, watchPage } from './helpers.js';

const PASSWORD_FILE = '/srv/projects/gx-cluster/secrets/control-ui/acceptance-password';
const EVIDENCE = process.env.GX_EVIDENCE_DIR || 'test-results/img-live';
const SOURCE = process.env.GX_IMG_SOURCE;
const report = { started: new Date().toISOString(), steps: [], results: {} };
const ALLOW = [/Failed to load resource.*(401|404|503)/];

test.describe.configure({ mode: 'serial' });
test.use({ screenshot: 'off', video: 'off', trace: 'off', viewport: { width: 1440, height: 1000 } });
mkdirSync(EVIDENCE, { recursive: true });

function note(step, data = {}) {
  report.steps.push({ at: new Date().toISOString(), step, ...data });
  console.log(`[img-live] ${step} ${JSON.stringify(data).slice(0, 300)}`);
  writeFileSync(join(EVIDENCE, 'images-live.json'), JSON.stringify(report, null, 1));
}

async function signIn(page) {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', readFileSync(PASSWORD_FILE, 'utf8').trim());
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
}

const getJson = async (page, url) => (await page.request.get(url)).json();

async function waitMedia(page, id, timeout = 45 * 60_000) {
  const t0 = Date.now();
  let last = '';
  for (;;) {
    const job = await getJson(page, `/api/media/jobs/${id}`);
    const key = `${job.phase}|${job.detail}`;
    if (key !== last) {
      note('media-phase', { id, phase: job.phase, detail: job.detail, cold: job.cold_start });
      last = key;
    }
    if (job.phase === 'failed' || job.phase === 'cancelled') throw new Error(`media job ${id} ${job.phase}: ${job.error}`);
    if (job.phase === 'ready') return { job, seconds: (Date.now() - t0) / 1000 };
    if (Date.now() - t0 > timeout) throw new Error(`media job ${id} timed out in ${job.phase}`);
    await page.waitForTimeout(3000);
  }
}

async function submit(page) {
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#generate-btn'),
  ]);
  const res = await req.response();
  expect(res.status(), await res.text()).toBe(202);
  return { body: req.postDataJSON(), job: await res.json() };
}

async function keep(page, name, assetId) {
  const asset = await getJson(page, `/api/media/assets/${assetId}`);
  const file = await page.request.get(`/api/media/assets/${assetId}/file`);
  expect(file.status()).toBe(200);
  writeFileSync(join(EVIDENCE, `${name}.png`), await file.body());
  report.results[name] = {
    asset: assetId, model_alias: asset.model_alias, model_repo: asset.model_repo, model_revision: asset.model_revision,
    workflow: asset.workflow, image_model: asset.settings.image_model, image_model_label: asset.settings.image_model_label,
    edit: asset.settings.edit, mask: asset.settings.mask, seed: asset.seed, strength: asset.strength,
    width: asset.width, height: asset.height, parent_id: asset.parent_id,
    elapsed_generation: asset.settings.router && asset.settings.router.elapsed_seconds,
  };
  note('kept', { name, ...report.results[name] });
  return asset;
}

test('generate with both models, switching in the UI', async ({ page }) => {
  const problems = watchPage(page, { allow: ALLOW });
  await signIn(page);
  await gotoPage(page, 'images');
  const model = page.getByLabel('Model', { exact: true });
  await model.selectOption('qwen-image-2512');
  await page.fill('#image-prompt', 'IMG acceptance: a red lighthouse on a rocky coast at golden hour, photograph');
  await page.locator('.chips-size .chip[data-value="1024x1024"]').click();
  await page.getByRole('group', { name: 'Quality' }).getByRole('button', { name: 'Fast' }).click();
  await page.getByLabel('Title').fill('IMG acceptance qwen-2512');
  const q = await submit(page);
  expect(q.body.image_model).toBe('qwen-image-2512');
  const qd = await waitMedia(page, q.job.id);
  const qa = await keep(page, 'gen-qwen-image-2512', qd.job.assets[0]);
  expect(qa.settings.image_model).toBe('qwen-image-2512');
  expect(qa.workflow).toMatch(/^qwen-image-2512/);
  report.results['gen-qwen-image-2512'].wall_seconds = qd.seconds;

  // switch the model in the UI
  await model.selectOption('visionmaster-pro-v3');
  await expect(page.locator('#image-model-hint')).toContainText('SDXL');
  await expect(page.getByRole('group', { name: 'Quality' })).toBeHidden();
  await page.screenshot({ path: join(EVIDENCE, 'ui-model-switched.png') });
  await axeCheck(page, 'live images with VisionmasterPro_V3');
  await page.fill('#image-prompt', 'IMG acceptance: portrait photo of a woman in a red coat on a rainy city street at night, bokeh lights');
  await page.locator('.chips-size .chip[data-value="832x1216"]').click();
  await page.getByLabel('Title').fill('IMG acceptance visionmaster-pro-v3');
  const v = await submit(page);
  expect(v.body).toMatchObject({ image_model: 'visionmaster-pro-v3', size: '832x1216' });
  const vd = await waitMedia(page, v.job.id);
  const va = await keep(page, 'gen-visionmaster-pro-v3', vd.job.assets[0]);
  expect(va.settings.image_model).toBe('visionmaster-pro-v3');
  expect(va.settings.image_model_label).toBe('VisionmasterPro_V3');
  expect(va.model_repo).toBe('votepurchase/pornmasterPro_noobV3VAE');
  expect(va.model_revision).toBe('75f59d136b165d48f3e678bb057af99f7cf1a71e');
  expect(va.workflow).toBe('sdxl-visionmaster-pro-v3');
  expect([va.width, va.height]).toEqual([832, 1216]);
  report.results['gen-visionmaster-pro-v3'].wall_seconds = vd.seconds;
  await expect(page.locator('#viewer img.media-img')).toBeVisible({ timeout: 60_000 });
  await page.screenshot({ path: join(EVIDENCE, 'ui-visionmaster-result.png') });
  expect(problems).toEqual([]);
});

test('three different edits and a masked edit of a known source', async ({ page }) => {
  test.skip(!SOURCE, 'GX_IMG_SOURCE is not set');
  const problems = watchPage(page, { allow: ALLOW });
  await signIn(page);
  await gotoPage(page, 'images');
  await page.getByRole('tab', { name: 'Edit' }).click();
  const [up] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/media/upload')),
    page.locator('#source-section input[type="file"]').setInputFiles({ name: 'img-acceptance-source.png', mimeType: 'image/png', buffer: readFileSync(SOURCE) }),
  ]);
  expect(up.status()).toBe(200);
  const source = await up.json();
  note('source', { asset: source.id, width: source.width, height: source.height });
  await expect(page.getByLabel('Model', { exact: true })).toHaveValue('qwen-image-edit-2511');
  const modes = page.getByRole('group', { name: 'Edit mode' });
  const edits = [
    ['edit-background', 'Background', 'a sandy tropical beach at sunset with palm trees and the ocean'],
    ['edit-style', 'Restyle', 'a Van Gogh oil painting with thick, swirling brush strokes'],
    ['edit-clothing', 'Subject', 'she wears a black leather biker jacket instead of the white t-shirt, and a red baseball cap'],
  ];
  for (const [name, mode, text] of edits) {
    await modes.getByRole('button', { name: mode, exact: true }).click();
    await page.fill('#image-prompt', text);
    await page.getByLabel('Title').fill(`IMG acceptance ${name}`);
    await page.getByRole('button', { name: 'Lock seed' }).evaluate((b) => b.getAttribute('aria-pressed'));
    const e = await submit(page);
    expect(e.body).toMatchObject({ kind: 'edit', source_id: source.id, image_model: 'qwen-image-edit-2511' });
    expect(e.body.strength).toBeUndefined();
    const done = await waitMedia(page, e.job.id);
    const asset = await keep(page, name, done.job.assets[0]);
    expect(asset.parent_id).toBe(source.id);
    expect(asset.settings.edit.denoise).toBe(1);
    report.results[name].wall_seconds = done.seconds;
  }
  // masked edit: only the right half (the street) may change
  await modes.getByRole('button', { name: 'Change / replace', exact: true }).click();
  await page.locator('#mask-section summary').click();
  await page.getByLabel('Left', { exact: true }).fill('55');
  await page.getByLabel('Top', { exact: true }).fill('0');
  await page.getByLabel('Width', { exact: true }).fill('45');
  await page.getByLabel('Height', { exact: true }).fill('100');
  await page.getByRole('button', { name: 'Add rectangle' }).click();
  await expect(page.locator('.mask-status')).toContainText('of the image is selected');
  await page.fill('#image-prompt', 'turn the street into a flooded canal with water and a small wooden boat');
  await page.getByLabel('Title').fill('IMG acceptance edit-masked');
  await page.screenshot({ path: join(EVIDENCE, 'ui-mask-editor.png') });
  const m = await submit(page);
  expect(m.body.mask).toMatch(/^data:image\/png;base64,/);
  const md = await waitMedia(page, m.job.id);
  const ma = await keep(page, 'edit-masked', md.job.assets[0]);
  expect(ma.settings.mask.source).toBe('rectangles');
  expect(ma.settings.edit.masked).toBe(true);
  report.results['edit-masked'].wall_seconds = md.seconds;
  // History lists the jobs with their model
  await gotoPage(page, 'history');
  await page.screenshot({ path: join(EVIDENCE, 'ui-history.png') });
  report.finished = new Date().toISOString();
  note('done');
  expect(problems).toEqual([]);
});
