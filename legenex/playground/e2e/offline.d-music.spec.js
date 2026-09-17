// Music studio: capability-driven controls, lyrics helper, generate -> import -> track card, remix, repaint.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, watchPage, wavBuffer } from './helpers.js';

const postMusic = (page) => page.waitForRequest((r) => r.url().endsWith('/api/music/jobs') && r.method() === 'POST');

test('controls follow the model capabilities', async ({ page, request }) => {
  const problems = watchPage(page);
  await login(page);
  const model = await (await page.request.get('/api/music/model')).json();
  expect(model.capabilities.controls.guidance_scale).toBeUndefined();
  await gotoPage(page, 'music');
  const tabs = page.getByRole('tablist', { name: 'Music mode' }).getByRole('tab');
  await expect(tabs).toHaveText(['Create', 'Remix/Cover', 'Repaint', 'Extend']);
  await expect(page.getByRole('tab', { name: /extract|lego|complete/i })).toHaveCount(0);
  await expect(page.getByText(/DiT CFG|Music model guidance/)).toHaveCount(0);
  await expect(page.getByLabel(/guidance scale/i)).toHaveCount(0);
  // The LM planner CFG is labelled as such.
  await page.locator('#form-create summary', { hasText: 'Advanced' }).click();
  await expect(page.getByLabel('Planner CFG (language model)')).toBeVisible();
  // Time signature labels and values.
  const ts = page.getByLabel('Time signature', { exact: true });
  await expect(ts.locator('option')).toHaveText(['Auto', '2/4', '3/4', '4/4', '6/8']);
  expect(await ts.locator('option').evaluateAll((o) => o.map((x) => x.value))).toEqual(['', '2', '3', '4', '6']);
  // Description and the structured controls are used together (MUS build V3).
  await page.fill('#music-description', 'a happy birthday song');
  await expect(page.getByLabel('BPM', { exact: true })).toBeVisible();
  await expect(page.locator('#music-conditioning')).toContainText('the music planner');
  await page.fill('#music-description', '');
  await axeCheck(page, 'music create');
  for (const name of ['Remix/Cover', 'Repaint', 'Extend']) {
    await page.getByRole('tab', { name }).click();
    await axeCheck(page, `music ${name}`);
  }
  expect(problems).toEqual([]);
  expect(request).toBeTruthy();
});

test('lyrics helper inserts section tags at the cursor', async ({ page }) => {
  await login(page);
  await gotoPage(page, 'music');
  const form = page.locator('#form-create');
  const lyrics = form.locator('textarea.lyrics-input');
  await expect(form.locator('.section-btn')).toHaveText(['[Intro]', '[Verse]', '[Pre-Chorus]', '[Chorus]', '[Post-Chorus]', '[Bridge]', '[Hook]', '[Breakdown]', '[Drop]', '[Build]', '[Interlude]', '[Instrumental]', '[Solo]', '[Guitar Solo]', '[Outro]', '[Fade Out]']);
  await lyrics.fill('line one\nline two');
  await lyrics.evaluate((el) => el.setSelectionRange(9, 9));
  await form.locator('.section-btn[data-section="Chorus"]').click();
  await expect(lyrics).toHaveValue('line one\n[Chorus]\nline two');
  // In the middle of a line the tag starts on a new line.
  await lyrics.evaluate((el) => el.setSelectionRange(4, 4));
  await form.locator('.section-btn[data-section="Verse"]').click();
  await expect(lyrics).toHaveValue('line\n[Verse]\n one\n[Chorus]\nline two');
  // Instrumental disables the lyrics.
  await page.getByText('Instrumental (no vocals)').click();
  await expect(lyrics).toBeDisabled();
  await expect(form.locator('.section-btn').first()).toBeDisabled();
});

test('generate an instrumental track, then remix and repaint it', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'music');
  const form = page.locator('#form-create');
  await page.fill('#music-prompt', 'e2e warm synthwave groove');
  const tagInput = form.getByLabel('Add a style tag');
  await tagInput.fill('synthwave');
  await tagInput.press('Enter');
  await form.locator('summary', { hasText: 'More tags from the model vocabulary' }).click();
  await form.getByRole('group', { name: 'Genre' }).getByRole('button', { name: 'jazz' }).click();
  await expect(form.locator('#music-tags .token-text')).toHaveText(['synthwave', 'jazz']);
  await form.getByRole('button', { name: 'Remove tag synthwave' }).click();
  await tagInput.fill('synthwave');
  await form.getByRole('button', { name: 'Add', exact: true }).click();
  await page.getByText('Instrumental (no vocals)').click();
  await form.getByLabel('Duration', { exact: true }).fill('30');
  await form.getByLabel('BPM', { exact: true }).fill('100');
  await form.getByLabel('Key', { exact: true }).fill('C major');
  await form.getByLabel('Time signature', { exact: true }).selectOption('3');
  await form.getByRole('group', { name: 'Tracks per run' }).getByRole('button', { name: '1' }).click();
  await form.getByLabel('Title', { exact: true }).fill('E2E Groove');
  const [req] = await Promise.all([postMusic(page), page.click('#music-submit')]);
  const body = req.postDataJSON();
  expect(body).toMatchObject({
    operation: 'generate', prompt: 'e2e warm synthwave groove', style_tags: ['jazz', 'synthwave'], instrumental: true,
    duration: 30, bpm: 100, key: 'C major', time_signature: '3', batch_size: 1, title: 'E2E Groove',
  });
  expect(body.lyrics).toBeUndefined();
  expect(body.guidance_scale).toBeUndefined();
  expect(typeof body.seed).toBe('number');
  const res = await req.response();
  expect(res.status()).toBe(202);
  const job = await res.json();

  const card = page.locator(`#music-jobs .job-card[data-job="${job.id}"]`);
  await expect(card.locator('.phase-badge')).toHaveAttribute('data-phase', 'COMPLETE', { timeout: 90_000 });
  const done = await (await page.request.get(`/api/music/jobs/${job.id}`)).json();
  expect(done.imported).toBe(true);
  expect(done.library_assets.length).toBe(1);
  const assetId = done.library_assets[0];

  const track = page.locator(`.track-card[data-asset="${assetId}"]`);
  await expect(track.getByRole('heading', { name: 'E2E Groove' })).toBeVisible();
  await expect(track.locator('canvas.wave-canvas')).toBeVisible();
  await expect(track.locator('audio')).toHaveAttribute('src', /\/api\/media\/assets\/a_[0-9a-f]{24}\/file/);
  await expect(track.getByRole('slider', { name: /Seek in/ })).toBeVisible();
  const wav = track.locator('a[data-format="wav"]');
  await expect(wav).toHaveAttribute('download', '');
  const wavRes = await page.request.get(await wav.getAttribute('href'));
  expect(wavRes.status()).toBe(200);
  expect(wavRes.headers()['content-type']).toContain('audio/wav');
  // Play works (the fixture renders a real WAV).
  await track.getByRole('button', { name: 'Play E2E Groove' }).click();
  await expect(track.getByRole('button', { name: 'Pause E2E Groove' })).toBeVisible();
  await track.getByRole('button', { name: 'Pause E2E Groove' }).click();
  await track.locator('summary', { hasText: 'Details, lyrics and lineage' }).click();
  await expect(track.locator('.kv')).toContainText('ACE-Step/acestep-v15-xl-turbo');
  await axeCheck(page, 'music with a track');

  // Remix from the finished track.
  await track.locator('[data-action="remix"]').click();
  const remix = page.locator('#form-remix');
  await expect(remix).toBeVisible();
  await expect(remix.locator('.source-chip')).toContainText('E2E Groove');
  await page.fill('#remix-prompt', 'as a string quartet');
  const [rreq] = await Promise.all([postMusic(page), page.click('#music-submit')]);
  const rbody = rreq.postDataJSON();
  expect(rbody).toMatchObject({ operation: 'remix', source_asset_id: assetId, prompt: 'as a string quartet', strength: 0.5 });
  expect((await rreq.response()).status()).toBe(202);

  // Repaint a section of the same track.
  await page.locator(`.track-card[data-asset="${assetId}"] [data-action="repaint"]`).click();
  const repaint = page.locator('#form-repaint');
  await expect(repaint.getByRole('slider', { name: /Seek in/ })).toBeVisible();
  await repaint.getByLabel('Start (s)').fill('0.2');
  await repaint.getByLabel('End (s)').fill('1.0');
  await repaint.getByRole('button', { name: 'Aggressive' }).click();
  const [preq] = await Promise.all([postMusic(page), page.click('#music-submit')]);
  expect(preq.postDataJSON()).toMatchObject({ operation: 'edit', source_asset_id: assetId, start: 0.2, end: 1, mode: 'aggressive' });
  expect((await preq.response()).status()).toBe(202);

  // Extend validates and submits.
  await page.locator(`.track-card[data-asset="${assetId}"] [data-action="extend"]`).first().click();
  const [xreq] = await Promise.all([postMusic(page), page.click('#music-submit')]);
  expect(xreq.postDataJSON()).toMatchObject({ operation: 'extend', source_asset_id: assetId, seconds: 30, direction: 'end' });
  expect(problems).toEqual([]);
});

test('upload a reference track', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'music');
  const ref = page.locator('#form-create .panel-section', { hasText: 'Reference track' });
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/music/upload')),
    ref.locator('input[type="file"]').setInputFiles({ name: 'e2e-ref.wav', mimeType: 'audio/wav', buffer: wavBuffer(1.2) }),
  ]);
  expect(res.status()).toBe(200);
  const asset = await res.json();
  expect(asset).toMatchObject({ type: 'audio', operation: 'upload' });
  expect(res.request().headers()['x-filename']).toBe('e2e-ref.wav');
  await expect(ref.locator('.source-chip')).toContainText('e2e-ref');
  await page.fill('#music-prompt', 'in the style of the reference');
  const [req] = await Promise.all([postMusic(page), page.click('#music-submit')]);
  expect(req.postDataJSON()).toMatchObject({ reference_asset_id: asset.id });
  expect((await req.response()).status()).toBe(202);
  expect(problems).toEqual([]);
});
