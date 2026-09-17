// LIVE creative acceptance through the deployed GX-Playground: REAL generations
// on gx10-02 through browser -> gx10-01 Playground -> Control Center backend.
// Run deliberately on gx10-01:  npm run test:live
// Signs in with the loopback-only acceptance account (password read from the
// secrets store, never logged), creates, plays, edits and downloads, then
// deletes every asset it created. Evidence: $GX_EVIDENCE_DIR/playground-live.json
import { expect, test } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { axeCheck, gotoPage, watchPage } from './helpers.js';

const PASSWORD_FILE = '/srv/projects/gx-cluster/secrets/control-ui/acceptance-password';
// GX_LIVE_TAG re-runs only the Library test against an earlier run's assets.
const TAG = process.env.GX_LIVE_TAG || `live-accept-${Date.now()}`;
const EVIDENCE = process.env.GX_EVIDENCE_DIR;
const created = [];
const report = { tag: TAG, steps: [] };
const ALLOW = [/Failed to load resource.*(401|404|503)/];
const MUSIC_REPO = 'ACE-Step/acestep-v15-xl-turbo';
const MUSIC_REV = 'd4a0b288b83ebb7e25a8c0b32c573c22e134e8ee';

test.describe.configure({ mode: 'serial' });
test.use({ screenshot: 'off', video: 'off', trace: 'off' });

function note(step, data = {}) {
  report.steps.push({ at: new Date().toISOString(), step, ...data });
  console.log(`[live] ${step} ${JSON.stringify(data).slice(0, 400)}`);
  if (EVIDENCE) writeFileSync(join(EVIDENCE, 'playground-live.json'), JSON.stringify(report, null, 1));
}

async function signIn(page) {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', readFileSync(PASSWORD_FILE, 'utf8').trim());
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
  await expect(page.locator('#page-dashboard h1')).toHaveText('What will you create today?');
}

async function post(page, path, data) {
  const { csrf } = await (await page.request.get('/api/session')).json();
  const origin = new URL(page.url()).origin;
  const res = await page.request.post(path, { headers: { 'X-CSRF-Token': csrf, Origin: origin, Referer: `${origin}/` }, data });
  expect(res.status(), `${path}: ${await res.text()}`).toBeLessThan(300);
  return res.json();
}

const getJson = async (page, url) => (await page.request.get(url)).json();

async function waitMusic(page, id, timeout = 30 * 60_000) {
  const t0 = Date.now();
  let last = '';
  for (;;) {
    const job = await getJson(page, `/api/music/jobs/${id}`);
    const key = `${job.phase}|${job.phase_detail}`;
    if (key !== last) {
      note('music-phase', { id, phase: job.phase, detail: job.phase_detail, waiting: job.waiting && job.waiting.reason });
      last = key;
    }
    if (job.status === 'failed' || job.status === 'cancelled') throw new Error(`music job ${id} ${job.status}: ${JSON.stringify(job.error)}`);
    if (job.import_error) throw new Error(`music import failed: ${job.import_error}`);
    if (job.status === 'completed' && job.imported) return job;
    if (Date.now() - t0 > timeout) throw new Error(`music job ${id} timed out in ${job.phase}`);
    await page.waitForTimeout(3000);
  }
}

async function waitMedia(page, id, timeout = 40 * 60_000) {
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
    if (job.phase === 'ready') return job;
    if (Date.now() - t0 > timeout) throw new Error(`media job ${id} timed out in ${job.phase}`);
    await page.waitForTimeout(3000);
  }
}

async function fetchCheck(page, url, type, minBytes) {
  const res = await page.request.get(url);
  expect(res.status(), url).toBe(200);
  expect(res.headers()['content-type'], url).toContain(type);
  const body = await res.body();
  expect(body.length, url).toBeGreaterThan(minBytes);
  return body.length;
}

test('music: vocal track with lyrics and tags, playback, WAV/FLAC/MP3, remix, repaint, extend, lineage', async ({ page }) => {
  test.setTimeout(75 * 60_000);
  const problems = watchPage(page, { allow: ALLOW });
  await signIn(page);
  await gotoPage(page, 'music');
  await expect(page.getByRole('tablist', { name: 'Music mode' }).getByRole('tab')).toHaveText(['Create', 'Remix/Cover', 'Repaint', 'Extend']);
  const form = page.locator('#form-create');
  await page.fill('#music-prompt', `${TAG} bright indie pop with a catchy chorus`);
  const tag = form.getByLabel('Custom style tag');
  for (const t of ['indie pop', 'upbeat', 'female vocals']) {
    await tag.fill(t);
    await tag.press('Enter');
  }
  const lyrics = form.locator('textarea.lyrics-input');
  await lyrics.fill('Sun on the window, we are driving away\nCity lights fading in the grey\n\nHold on, hold on, the road is ours today\nHold on, hold on, we will find our way');
  await lyrics.evaluate((el) => el.setSelectionRange(0, 0));
  await form.locator('.section-btn[data-section="Verse"]').click();
  const chorusAt = (await lyrics.inputValue()).indexOf('Hold on');
  await lyrics.evaluate((el, at) => el.setSelectionRange(at, at), chorusAt);
  await form.locator('.section-btn[data-section="Chorus"]').click();
  await form.getByLabel('Duration').fill('30');
  await form.getByLabel('BPM').fill('112');
  await form.getByLabel('Key', { exact: true }).fill('A minor');
  await form.getByLabel('Time signature').selectOption('4');
  await form.getByRole('group', { name: 'Tracks per run' }).getByRole('button', { name: '1' }).click();
  const title = `${TAG} vocal`;
  await form.getByLabel('Title').fill(title);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/jobs') && r.method() === 'POST'),
    page.click('#music-submit'),
  ]);
  const body = req.postDataJSON();
  expect(body).toMatchObject({ operation: 'generate', bpm: 112, key: 'A minor', time_signature: '4', duration: 30, batch_size: 1 });
  expect(body.lyrics).toContain('[Verse]');
  expect(body.lyrics).toContain('[Chorus]');
  expect(body.style_tags).toEqual(expect.arrayContaining(['indie pop', 'upbeat', 'female vocals']));
  const res = await req.response();
  expect(res.status()).toBe(202);
  const job = await res.json();
  note('music-submitted', { id: job.id, lyrics_chars: body.lyrics.length, tags: body.style_tags });
  await expect(page.locator(`#music-jobs .job-card[data-job="${job.id}"]`)).toBeVisible();
  const done = await waitMusic(page, job.id);
  note('music-done', { id: job.id, timings: done.timings, model: done.model,
    tracks: done.tracks.map((t) => ({ ...t, files: Object.keys(t.files || {}) })) });
  expect(done.library_assets.length).toBe(1);
  const assetId = done.library_assets[0];
  created.push(assetId);
  const asset = await getJson(page, `/api/media/assets/${assetId}`);
  expect(asset).toMatchObject({ type: 'audio', model_alias: 'gx-music', model_repo: MUSIC_REPO, model_revision: MUSIC_REV });
  expect(Object.keys(asset.downloads).sort()).toEqual(['flac', 'mp3', 'wav']);
  const sizes = {
    wav: await fetchCheck(page, asset.downloads.wav, 'audio/wav', 500_000),
    flac: await fetchCheck(page, asset.downloads.flac, 'audio/flac', 200_000),
    mp3: await fetchCheck(page, asset.downloads.mp3, 'audio/mpeg', 100_000),
  };
  note('music-downloads', { assetId, sizes, duration: asset.duration, bpm: asset.bpm, key: asset.music_key, ts: asset.time_signature });

  // Real browser playback: the <audio> element advances.
  const track = page.locator(`.track-card[data-asset="${assetId}"]`);
  await expect(track).toBeVisible({ timeout: 60_000 });
  await expect(track.locator('canvas.wave-canvas')).toBeVisible();
  await track.getByRole('button', { name: `Play ${title}` }).click();
  await expect.poll(() => track.locator('audio').evaluate((a) => a.currentTime), { timeout: 30_000 }).toBeGreaterThan(0.5);
  const duration = await track.locator('audio').evaluate((a) => a.duration);
  expect(duration).toBeGreaterThan(20);
  await track.getByRole('button', { name: `Pause ${title}` }).click();
  note('music-playback', { assetId, duration });
  await axeCheck(page, 'live music with a real track');

  // Remix from the finished track in the UI: child asset with lineage.
  await track.locator('[data-action="remix"]').click();
  await expect(page.locator('#form-remix .source-chip')).toContainText(TAG);
  await page.fill('#remix-prompt', 'acoustic guitar and strings, intimate');
  const [rreq] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/jobs') && r.method() === 'POST'),
    page.click('#music-submit'),
  ]);
  expect(rreq.postDataJSON()).toMatchObject({ operation: 'remix', source_asset_id: assetId });
  const rjob = await (await rreq.response()).json();
  const rdone = await waitMusic(page, rjob.id);
  const childId = rdone.library_assets[0];
  created.push(childId);
  const lineage = await getJson(page, `/api/media/assets/${childId}/lineage`);
  expect(lineage.asset.parent_id).toBe(assetId);
  expect(lineage.root).toBe(assetId);
  note('music-remix', { id: rjob.id, childId, parent: lineage.asset.parent_id, timings: rdone.timings });

  // Repaint and extend (same session, same application API).
  const rp = await post(page, '/api/music/jobs', { operation: 'edit', source_asset_id: assetId, start: 4, end: 10, mode: 'balanced',
    prompt: 'bright indie pop with a catchy chorus', title: `${TAG} repaint` });
  const rpdone = await waitMusic(page, rp.id);
  created.push(rpdone.library_assets[0]);
  note('music-repaint', { id: rp.id, asset: rpdone.library_assets[0], timings: rpdone.timings });
  const ex = await post(page, '/api/music/jobs', { operation: 'extend', source_asset_id: assetId, seconds: 15, direction: 'end',
    prompt: 'bright indie pop outro', title: `${TAG} extend` });
  const exdone = await waitMusic(page, ex.id);
  created.push(exdone.library_assets[0]);
  const exAsset = await getJson(page, `/api/media/assets/${exdone.library_assets[0]}`);
  expect(exAsset.duration).toBeGreaterThan(asset.duration);
  expect(exAsset.parent_id).toBe(assetId);
  note('music-extend', { id: ex.id, asset: exAsset.id, duration: exAsset.duration, parent: exAsset.parent_id });
  expect(problems).toEqual([]);
});

test('images: generate in the UI, edit and variation, download', async ({ page }) => {
  test.setTimeout(45 * 60_000);
  const problems = watchPage(page, { allow: ALLOW });
  await signIn(page);
  await gotoPage(page, 'images');
  await page.fill('#image-prompt', `${TAG} a red vintage bicycle leaning on a blue wall, film photo`);
  await page.locator('.chips-size .chip[data-value="1024x1024"]').click();
  await page.locator('[aria-label="Number of images"] .chip[data-value="1"]').click();
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#generate-btn'),
  ]);
  const job = await (await req.response()).json();
  const done = await waitMedia(page, job.id);
  const imageId = done.assets[0];
  created.push(imageId);
  note('image-done', { id: job.id, asset: imageId, elapsed: done.elapsed_seconds, cold: done.cold_start });
  const shown = page.locator('#viewer img.media-img');
  await expect(shown).toBeVisible({ timeout: 60_000 });
  await expect.poll(() => shown.evaluate((el) => el.naturalWidth)).toBeGreaterThan(0);
  const img = await getJson(page, `/api/media/assets/${imageId}`);
  expect(img).toMatchObject({ type: 'image', width: 1024, height: 1024, model_alias: 'gx-image' });
  note('image-download', { asset: imageId, bytes: await fetchCheck(page, img.download_url, 'image/', 50_000) });

  // Edit via the UI.
  await page.locator('#viewer [data-action="edit"]').click();
  await page.fill('#image-prompt', 'make the bicycle bright yellow');
  const [ereq] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#generate-btn'),
  ]);
  expect(ereq.postDataJSON()).toMatchObject({ kind: 'edit', source_id: imageId });
  const edone = await waitMedia(page, (await (await ereq.response()).json()).id);
  created.push(edone.assets[0]);
  const child = await getJson(page, `/api/media/assets/${edone.assets[0]}`);
  expect(child.parent_id).toBe(imageId);
  note('image-edit', { asset: child.id, parent: child.parent_id, elapsed: edone.elapsed_seconds });

  // Variation.
  const v = await post(page, '/api/media/jobs', { kind: 'variation', source_id: imageId, n: 1 });
  const vdone = await waitMedia(page, v.id);
  created.push(vdone.assets[0]);
  note('image-variation', { asset: vdone.assets[0], elapsed: vdone.elapsed_seconds });
  expect(problems).toEqual([]);
});

test('video: text to video completes, plays and downloads; image to video completes', async ({ page }) => {
  test.setTimeout(100 * 60_000);
  const problems = watchPage(page, { allow: ALLOW });
  await signIn(page);
  await gotoPage(page, 'video');
  await page.fill('#video-prompt', `${TAG} ocean waves rolling onto a beach at sunset, slow camera pan`);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST'),
    page.click('#generate-btn'),
  ]);
  const job = await (await req.response()).json();
  note('video-submitted', { id: job.id, params: req.postDataJSON() });
  const done = await waitMedia(page, job.id, 50 * 60_000);
  const videoId = done.assets[0];
  created.push(videoId);
  const asset = await getJson(page, `/api/media/assets/${videoId}`);
  const bytes = await fetchCheck(page, asset.download_url, 'video/', 20_000);
  note('video-done', { id: job.id, asset: videoId, frames: asset.frame_count, distinct: asset.distinct_frames, bytes, elapsed: done.elapsed_seconds, cold: done.cold_start });
  expect(asset.distinct_frames).toBeGreaterThan(2);
  const video = page.locator('#viewer video');
  await expect(video).toBeVisible({ timeout: 60_000 });
  await expect.poll(() => video.evaluate((v) => v.readyState), { timeout: 30_000 }).toBeGreaterThanOrEqual(2);
  await video.evaluate((v) => { v.muted = true; return v.play(); });
  await expect.poll(() => video.evaluate((v) => v.currentTime), { timeout: 30_000 }).toBeGreaterThan(0.2);
  await video.evaluate((v) => v.pause());
  note('video-playback', { asset: videoId });

  // Image to video from the image this run created.
  let imageId = null;
  for (const id of created) {
    if ((await getJson(page, `/api/media/assets/${id}`)).type === 'image') { imageId = id; break; }
  }
  expect(imageId, 'the images test created an image').toBeTruthy();
  const i2v = await post(page, '/api/media/jobs', { kind: 'i2v', source_id: imageId, prompt: 'the bicycle slowly rolls forward', seconds: 2 });
  const idone = await waitMedia(page, i2v.id, 50 * 60_000);
  created.push(idone.assets[0]);
  const iasset = await getJson(page, `/api/media/assets/${idone.assets[0]}`);
  expect(iasset.parent_id).toBe(imageId);
  note('video-i2v', { asset: iasset.id, parent: iasset.parent_id, distinct: iasset.distinct_frames, elapsed: idone.elapsed_seconds, cold: idone.cold_start });
  expect(problems).toEqual([]);
});

// Every asset of a run: the tagged roots plus all of their descendants.
async function runAssets(page) {
  const ids = new Set();
  const walk = (nodes) => nodes.forEach((n) => { ids.add(n.id); walk(n.children || []); });
  const roots = await getJson(page, `/api/media/assets?q=${encodeURIComponent(TAG)}&limit=200`);
  for (const a of roots.items) {
    ids.add(a.id);
    walk((await getJson(page, `/api/media/assets/${a.id}/lineage`)).tree);
  }
  return [...ids];
}

test('library: search, filter, sort, bulk ZIP, cross-links, then delete the test assets', async ({ page, context }) => {
  test.setTimeout(10 * 60_000);
  const problems = watchPage(page, { allow: ALLOW });
  await signIn(page);
  if (!created.length) created.push(...await runAssets(page));
  expect(created.length, `assets tagged ${TAG}`).toBeGreaterThan(0);
  await gotoPage(page, 'library');
  await page.fill('#lib-search', TAG);
  await expect.poll(() => page.locator('.item-check').count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(1);
  const found = await getJson(page, `/api/media/assets?q=${encodeURIComponent(TAG)}&limit=100`);
  const audio = await getJson(page, `/api/media/assets?q=${encodeURIComponent(TAG)}&type=audio`);
  expect(audio.items.length).toBeGreaterThan(0);
  expect(audio.items.every((a) => a.type === 'audio')).toBe(true);
  note('library-search', { found: found.total, audio: audio.items.length, created: created.length });
  await page.locator('#lib-sort').selectOption({ index: 1 });
  await axeCheck(page, 'live library');
  const zip = await post(page, '/api/media/zip', { ids: created });
  const zipRes = await page.request.get(zip.url);
  expect(zipRes.status()).toBe(200);
  expect(zipRes.headers()['content-type']).toContain('application/zip');
  note('library-zip', { count: zip.count, bytes: zip.bytes });

  // Cross-links: Playground -> Control Center (same sign-in) -> Playground.
  const cc = page.locator('#cc-link');
  await expect(cc).toBeVisible();
  const ccHref = await cc.getAttribute('href');
  const ccPage = await context.newPage();
  await ccPage.goto(ccHref);
  await expect(ccPage.locator('#app-view')).toBeVisible({ timeout: 30_000 });
  await expect(ccPage.locator('#nav-playground')).toHaveAttribute('href', /:8090\/$/);
  await ccPage.close();
  note('cross-links', { control_center: ccHref });

  const del = await post(page, '/api/media/delete', { ids: [...created].reverse(), confirm: true });
  expect(del.deleted.length).toBe(created.length);
  note('library-cleanup', { deleted: del.deleted.length });
  expect(problems).toEqual([]);
});
