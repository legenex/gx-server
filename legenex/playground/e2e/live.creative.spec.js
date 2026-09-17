// LIVE creative acceptance through the deployed GX-Playground (REAL generations
// on gx10-02). Run deliberately on gx10-01:  npx playwright test --project=live
// Signs in with the loopback-only acceptance account; creates, plays, edits,
// downloads and finally deletes its own assets. Evidence goes to
// GX_EVIDENCE_DIR when set.
import { expect, test } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { axeCheck, gotoPage, watchPage } from './helpers.js';

const PW = readFileSync('/srv/projects/gx-cluster/secrets/control-ui/acceptance-password', 'utf8').trim();
const TAG = `live-accept-${Date.now()}`;
const EVIDENCE = process.env.GX_EVIDENCE_DIR;
const created = [];
const report = { tag: TAG, steps: [] };

test.describe.configure({ mode: 'serial' });

function note(step, data) {
  report.steps.push({ at: new Date().toISOString(), step, ...data });
  if (EVIDENCE) writeFileSync(join(EVIDENCE, 'playground-live.json'), JSON.stringify(report, null, 1));
}

async function signIn(page) {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', PW);
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
}

async function waitMusic(page, id, timeout = 30 * 60_000) {
  const t0 = Date.now();
  let last = '';
  for (;;) {
    const job = await (await page.request.get(`/api/music/jobs/${id}`)).json();
    const key = `${job.status}|${job.detail}`;
    if (key !== last) { note('music-phase', { id, status: job.status, detail: job.detail, waiting: job.waiting }); last = key; }
    if (job.status === 'failed' || job.status === 'cancelled') throw new Error(`music job ${id} ${job.status}: ${JSON.stringify(job.error)}`);
    if (job.status === 'completed' && job.imported) return job;
    if (Date.now() - t0 > timeout) throw new Error(`music job ${id} timed out in ${job.status}`);
    await page.waitForTimeout(3000);
  }
}

async function waitMedia(page, id, timeout = 40 * 60_000) {
  const t0 = Date.now();
  let last = '';
  for (;;) {
    const job = await (await page.request.get(`/api/media/jobs/${id}`)).json();
    const key = `${job.phase}|${job.detail}`;
    if (key !== last) { note('media-phase', { id, phase: job.phase, detail: job.detail, waiting: job.waiting }); last = key; }
    if (job.phase === 'failed' || job.phase === 'cancelled') throw new Error(`media job ${id} ${job.phase}: ${job.error}`);
    if (job.phase === 'ready') return job;
    if (Date.now() - t0 > timeout) throw new Error(`media job ${id} timed out in ${job.phase}`);
    await page.waitForTimeout(3000);
  }
}

async function downloadCheck(page, url, type, minBytes) {
  const res = await page.request.get(url);
  expect(res.status(), url).toBe(200);
  expect(res.headers()['content-type']).toContain(type);
  const body = await res.body();
  expect(body.length, url).toBeGreaterThan(minBytes);
  return body.length;
}

test('music: lyrics with vocals, style tags, BPM/key/time signature, play, downloads, remix and lineage', async ({ page }) => {
  test.setTimeout(60 * 60_000);
  const problems = watchPage(page, { allow: [/Failed to load resource.*(401|404)/] });
  await signIn(page);
  await gotoPage(page, 'music');
  await expect(page.getByRole('tab', { name: /Extract|Lego|Complete/ })).toHaveCount(0);
  await expect(page.getByLabel(/Guidance/)).toHaveCount(0);
  const form = page.locator('#form-create');
  await page.fill('#music-prompt', `${TAG} bright indie pop with a catchy hook`);
  const tag = form.getByLabel('Custom style tag');
  for (const t of ['indie pop', 'upbeat', 'female vocals']) {
    await tag.fill(t);
    await tag.press('Enter');
  }
  const lyrics = form.getByLabel('Lyrics', { exact: true });
  await lyrics.click();
  await form.getByRole('button', { name: '[Verse]' }).click();
  await lyrics.press('End');
  await lyrics.type('\nSun on the window, we are driving away\n');
  await form.getByRole('button', { name: '[Chorus]' }).click();
  await lyrics.press('End');
  await lyrics.type('\nHold on, hold on, the road is ours today\n');
  await form.getByLabel('Duration').fill('24');
  await form.getByLabel('BPM').fill('112');
  await form.getByLabel('Key', { exact: true }).fill('A minor');
  await form.getByLabel('Time signature').selectOption('4');
  await form.getByLabel('Title').fill(`${TAG} vocal`);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/jobs') && r.method() === 'POST'),
    page.click('#music-submit'),
  ]);
  const body = req.postDataJSON();
  expect(body.lyrics).toContain('[Chorus]');
  expect(body.style_tags).toEqual(expect.arrayContaining(['indie pop', 'upbeat', 'female vocals']));
  const job = await (await req.response()).json();
  note('music-submitted', { id: job.id, body });
  const card = page.locator(`#music-jobs .job-card[data-job="${job.id}"]`);
  await expect(card).toBeVisible();
  const done = await waitMusic(page, job.id);
  note('music-done', { id: job.id, timings: done.timings, tracks: done.tracks.map((t) => ({ bpm: t.bpm, key: t.key, ts: t.time_signature, seed: t.seed, duration: t.duration_s })) });
  const assetId = done.library_assets[0];
  created.push(assetId);
  const asset = await (await page.request.get(`/api/media/assets/${assetId}`)).json();
  expect(asset.type).toBe('audio');
  expect(asset.model_repo).toBe('ACE-Step/acestep-v15-xl-turbo');
  expect(asset.model_revision).toBe('d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee');
  expect(Object.keys(asset.downloads).sort()).toEqual(['flac', 'mp3', 'wav']);
  const sizes = {};
  sizes.wav = await downloadCheck(page, asset.downloads.wav, 'audio/wav', 100_000);
  sizes.flac = await downloadCheck(page, asset.downloads.flac, 'audio/flac', 50_000);
  sizes.mp3 = await downloadCheck(page, asset.downloads.mp3, 'audio/mpeg', 50_000);
  note('music-downloads', { assetId, sizes });

  // Playback in the browser: the audio element really advances.
  await page.reload();
  await expect(page.locator(`.track-card[data-asset="${assetId}"]`)).toBeVisible({ timeout: 30_000 });
  const track = page.locator(`.track-card[data-asset="${assetId}"]`);
  await track.getByRole('button', { name: /^Play / }).click();
  await expect.poll(async () => track.locator('audio').evaluate((a) => a.currentTime), { timeout: 30_000 }).toBeGreaterThan(0.5);
  const duration = await track.locator('audio').evaluate((a) => a.duration);
  expect(duration).toBeGreaterThan(20);
  await track.getByRole('button', { name: /^Pause / }).click();
  note('music-playback', { assetId, duration });
  await axeCheck(page, 'live music');

  // Remix from the finished track (child with lineage).
  await track.locator('[data-action="remix"]').click();
  await page.fill('#remix-prompt', 'acoustic guitar and strings, intimate');
  await page.locator('#form-remix').getByLabel('Title').fill(`${TAG} remix`);
  const [rreq] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/jobs') && r.method() === 'POST'),
    page.click('#music-submit'),
  ]);
  expect(rreq.postDataJSON()).toMatchObject({ operation: 'remix', source_asset_id: assetId });
  const rjob = await (await rreq.response()).json();
  const rdone = await waitMusic(page, rjob.id);
  const childId = rdone.library_assets[0];
  created.push(childId);
  const lineage = await (await page.request.get(`/api/media/assets/${childId}/lineage`)).json();
  expect(lineage.asset.parent_id).toBe(assetId);
  expect(lineage.root).toBe(assetId);
  note('music-remix', { id: rjob.id, childId, parent: lineage.asset.parent_id, timings: rdone.timings });
  expect(problems).toEqual([]);
});

test('images: generate, edit, download and history', async ({ page }) => {
  test.setTimeout(45 * 60_000);
  const problems = watchPage(page, { allow: [/Failed to load resource.*(401|404)/] });
  await signIn(page);
  await gotoPage(page, 'images');
  await page.fill('#image-prompt', `${TAG} a red vintage bicycle leaning on a blue wall, film photo`);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#image-submit'),
  ]);
  const job = await (await req.response()).json();
  const done = await waitMedia(page, job.id);
  const imageId = done.assets[0];
  created.push(imageId);
  note('image-done', { id: job.id, asset: imageId, elapsed: done.elapsed_seconds });
  const edit = await (await page.request.post('/api/media/jobs', {
    headers: { 'X-CSRF-Token': (await (await page.request.get('/api/session')).json()).csrf, Origin: new URL(page.url()).origin },
    data: { kind: 'edit', source_id: imageId, prompt: 'make the bicycle bright yellow', title: `${TAG} edit` },
  })).json();
  const edone = await waitMedia(page, edit.id);
  created.push(edone.assets[0]);
  const child = await (await page.request.get(`/api/media/assets/${edone.assets[0]}`)).json();
  expect(child.parent_id).toBe(imageId);
  const size = await downloadCheck(page, child.download_url, 'image/', 10_000);
  note('image-edit', { id: edit.id, asset: child.id, parent: child.parent_id, bytes: size });
  await page.reload();
  await expect(page.locator(`[data-asset="${child.id}"]`).first()).toBeVisible({ timeout: 30_000 });
  expect(problems).toEqual([]);
});

test('video: text to video completes, plays and downloads (waits honestly if gx10-02 is busy)', async ({ page }) => {
  test.setTimeout(60 * 60_000);
  const problems = watchPage(page, { allow: [/Failed to load resource.*(401|404)/] });
  await signIn(page);
  await gotoPage(page, 'video');
  await page.fill('#video-prompt', `${TAG} ocean waves rolling onto a beach at sunset, slow camera pan`);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#video-submit'),
  ]);
  const job = await (await req.response()).json();
  const done = await waitMedia(page, job.id, 50 * 60_000);
  const videoId = done.assets[0];
  created.push(videoId);
  const asset = await (await page.request.get(`/api/media/assets/${videoId}`)).json();
  const bytes = await downloadCheck(page, asset.download_url, 'video/', 20_000);
  note('video-done', { id: job.id, asset: videoId, frames: asset.frame_count, distinct: asset.distinct_frames, bytes });
  expect(asset.distinct_frames).toBeGreaterThan(2);
  await page.goto(`/#/video/${videoId}`);
  const video = page.locator('video').first();
  await expect(video).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => video.evaluate((v) => v.readyState), { timeout: 30_000 }).toBeGreaterThanOrEqual(2);
  expect(problems).toEqual([]);
});

test('library: search, filter, select, bulk ZIP, lineage, cross-links, then delete the test assets', async ({ page }) => {
  test.setTimeout(10 * 60_000);
  const problems = watchPage(page, { allow: [/Failed to load resource.*(401|404)/] });
  await signIn(page);
  await gotoPage(page, 'library');
  await page.getByRole('searchbox').first().fill(TAG);
  await expect.poll(async () => page.locator('[data-asset]').count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(created.length);
  const zip = await (await page.request.post('/api/media/zip', {
    headers: { 'X-CSRF-Token': (await (await page.request.get('/api/session')).json()).csrf, Origin: new URL(page.url()).origin },
    data: { ids: created },
  })).json();
  const zipRes = await page.request.get(zip.url);
  expect(zipRes.status()).toBe(200);
  note('library-zip', { count: zip.count, bytes: zip.bytes });
  // Cross-links: Playground -> Control Center, Control Center -> Playground.
  const cc = page.getByRole('link', { name: /Control Center/ }).first();
  await expect(cc).toHaveAttribute('href', /:8088/);
  const ccPage = await page.context().newPage();
  await ccPage.goto('http://127.0.0.1:8088/#/dashboard');
  await expect(ccPage.locator('#app-view')).toBeVisible();   // same session: no second sign-in
  await expect(ccPage.locator('#nav-playground')).toHaveAttribute('href', 'http://127.0.0.1:8090/');
  await ccPage.close();
  // Clean up: delete the assets this test created (children first).
  const csrf = (await (await page.request.get('/api/session')).json()).csrf;
  const del = await (await page.request.post('/api/media/delete', {
    headers: { 'X-CSRF-Token': csrf, Origin: new URL(page.url()).origin },
    data: { ids: [...created].reverse(), confirm: true },
  })).json();
  expect(del.deleted.length).toBe(created.length);
  note('library-cleanup', { deleted: del.deleted.length });
  expect(problems).toEqual([]);
});
