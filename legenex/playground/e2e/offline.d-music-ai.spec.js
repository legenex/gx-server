// Music (MUS, build V3): conditioning order, style-tag tokens, vocal rules,
// Build with AI, Improve My Prompt and Analyze Reference, against the offline
// fixture (real Control Center + real supervisor validation + gx-auto stand-in).
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, noHorizontalOverflow, watchPage, wavBuffer } from './helpers.js';

const postMusic = (page) => page.waitForRequest((r) => r.url().endsWith('/api/music/jobs') && r.method() === 'POST');

async function openCreate(page) {
  await login(page);
  await gotoPage(page, 'music');
  // The page replaces its skeleton once GET /api/music/model and /api/music/tags
  // answer; gotoPage only waits for the heading, so wait for the built form.
  const form = page.locator('#form-create');
  await expect(form.locator('#music-conditioning')).toBeAttached();
  return form;
}

test('create form follows the conditioning order and tags are real tokens', async ({ page }) => {
  const problems = watchPage(page);
  const form = await openCreate(page);
  const order = await page.evaluate(() => ['#music-ai-prompt', '#music-reference', '#music-description', '#music-tags',
    '#music-prompt', '.vocal-controls', 'textarea.lyrics-input', '#music-conditioning']
    .map((sel) => document.querySelector(`#form-create ${sel}`))
    .map((el) => (el ? el.getBoundingClientRect().top + window.scrollY : -1)));
  expect(order.every((v) => v >= 0)).toBe(true);
  expect([...order].sort((a, b) => a - b)).toEqual(order);
  await expect(form.getByText('Song description — what song should be created?')).toBeVisible();
  await expect(form.getByText('Style prompt — how should it sound?')).toBeVisible();

  const tags = form.locator('#music-tags');
  const input = tags.getByLabel('Add a style tag');
  for (const t of ['deep house', 'piano', 'dark']) {
    await input.fill(t);
    await input.press('Enter');
  }
  await input.fill('uplifting,');
  await expect(tags.locator('.token-text')).toHaveText(['deep house', 'piano', 'dark', 'uplifting']);
  // suggestions toggle and show their state
  const sug = tags.getByRole('group', { name: 'Suggested style tags' });
  await expect(sug.getByRole('button', { name: 'dark', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await sug.getByRole('button', { name: 'cinematic', exact: true }).click();
  await expect(tags.locator('.token-text').last()).toHaveText('cinematic');
  // keyboard: move, edit, delete
  const first = tags.getByRole('button', { name: 'deep house, tag 1 of 5' });
  await first.focus();
  await page.keyboard.press('Alt+ArrowRight');
  await expect(tags.locator('.token-text')).toHaveText(['piano', 'deep house', 'dark', 'uplifting', 'cinematic']);
  await expect(tags.locator('[aria-live="polite"]').first()).toContainText('Moved deep house to position 2 of 5');
  await page.keyboard.press('Enter');
  const edit = tags.getByLabel('Edit tag deep house');
  await edit.fill('afro house');
  await edit.press('Enter');
  await expect(tags.locator('.token-text').nth(1)).toHaveText('afro house');
  await tags.getByRole('button', { name: 'piano, tag 1 of 5' }).focus();
  await page.keyboard.press('Delete');
  await expect(tags.locator('.token-text')).toHaveText(['afro house', 'dark', 'uplifting', 'cinematic']);
  // pointer/touch: select a tag, then use the toolbar
  await tags.getByRole('button', { name: 'cinematic, tag 4 of 4' }).click();
  await tags.getByRole('button', { name: 'Move left' }).click();
  await expect(tags.locator('.token-text')).toHaveText(['afro house', 'dark', 'cinematic', 'uplifting']);
  await tags.getByRole('button', { name: 'Remove tag uplifting' }).click();

  // the tags really reach the caption ACE-Step receives
  await page.fill('#music-prompt', 'warm Rhodes, rolling percussion');
  const caption = page.locator('#music-conditioning pre[data-caption]');
  await expect(caption).toHaveText('warm Rhodes, rolling percussion, afro house, dark, cinematic, instrumental');
  await page.fill('#music-description', 'A night drive along the coast.');
  await expect(caption).toHaveText('warm Rhodes, rolling percussion, afro house, dark, cinematic, instrumental. A night drive along the coast.');
  await axeCheck(page, 'music create (tokens + preview)');
  expect(problems).toEqual([]);
});

test('vocal requests never silently become instrumental', async ({ page }) => {
  const problems = watchPage(page, { allow: [/status of 400/] });
  const form = await openCreate(page);
  await page.fill('#music-description', 'An emotional song about leaving Cape Town after the end of a relationship.');
  await form.locator('#music-tags').getByLabel('Add a style tag').fill('cinematic');
  await form.locator('#music-tags').getByLabel('Add a style tag').press('Enter');
  await page.fill('#music-prompt', 'Intimate close-mic vocal, soft piano opening');
  await form.getByRole('group', { name: 'Vocal type' }).getByRole('button', { name: 'Female' }).click();
  const status = form.locator('.vocal-status');
  await expect(status).toHaveText('Vocals need lyrics. Write them, pick “Write with AI”, or turn Instrumental on.');
  await expect(page.locator('#music-conditioning')).toContainText('This would be refused');
  await page.click('#music-submit');
  await expect(form.locator('.form-error')).toContainText('Vocals need lyrics');

  // Write with AI: gx-auto writes the words before the job is queued
  await form.locator('#music-lyrics-source').selectOption('assistant');
  await expect(status).toHaveText('Vocals: gx-auto writes the lyrics when you press Create.');
  await expect(page.locator('#music-conditioning')).toContainText('lyrics written by gx-auto');
  const [req] = await Promise.all([postMusic(page), page.click('#music-submit')]);
  expect(req.postDataJSON()).toMatchObject({ vocal_intent: 'female', lyrics_source: 'assistant', instrumental: false,
    style_tags: ['cinematic'] });
  expect(req.postDataJSON().lyrics).toBeUndefined();
  const res = await req.response();
  expect(res.status()).toBe(202);
  const job = await res.json();
  expect(job.request.lyrics).toContain('Written by the e2e lyricist');
  expect(job.request.conditioning.caption).toBe('Intimate close-mic vocal, soft piano opening, cinematic, female vocals. '
    + 'An emotional song about leaving Cape Town after the end of a relationship.');

  // Instrumental switches the vocal controls off
  await page.getByText('Instrumental (no vocals)').click();
  await expect(form.getByRole('group', { name: 'Vocal type' }).getByRole('button', { name: 'Female' })).toBeDisabled();
  await expect(status).toHaveText('Instrumental: no vocals will be rendered.');
  const [req2] = await Promise.all([postMusic(page), page.click('#music-submit')]);
  expect(req2.postDataJSON()).toMatchObject({ instrumental: true });
  expect(req2.postDataJSON().vocal_intent).toBeUndefined();
  expect(problems).toEqual([]);
});

test('Build with AI fills the form, respects locks and can be undone', async ({ page }) => {
  const problems = watchPage(page);
  const form = await openCreate(page);
  await form.getByLabel('BPM', { exact: true }).fill('100');
  await form.getByRole('button', { name: 'Keep BPM when using AI' }).click();
  await expect(form.getByRole('button', { name: 'Keep BPM when using AI' })).toHaveAttribute('aria-pressed', 'true');
  await page.fill('#music-ai-prompt', 'Make me a dark but uplifting Afro house track for a luxury travel ad, female vocal, around 122 BPM, African percussion, emotional chorus, no cheesy EDM drop.');
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/ai/build')),
    page.click('#music-ai-build'),
  ]);
  expect(req.postDataJSON()).toMatchObject({ locked: ['bpm'], write_lyrics: true });
  await expect(page.locator('.ai-status')).toContainText('Built with AI');
  await expect(page.locator('#music-description')).toHaveValue('A luxury travel anthem about crossing the savannah at dusk.');
  await expect(form.locator('#music-tags .token-text')).toHaveText(['afro house', 'female vocals', 'dark', 'uplifting', 'african percussion', 'deep bass']);
  await expect(page.locator('#music-prompt')).toHaveValue(/Soulful female lead/);
  await expect(form.getByLabel('BPM', { exact: true })).toHaveValue('100');
  await expect(form.getByLabel('Key', { exact: true })).toHaveValue('A minor');
  await expect(form.getByLabel('Time signature', { exact: true })).toHaveValue('4');
  await expect(form.getByLabel('Duration', { exact: true })).toHaveValue('150');
  await expect(form.locator('textarea.lyrics-input')).toHaveValue(/\[Verse\]/);
  await expect(form.getByRole('group', { name: 'Vocal type' }).getByRole('button', { name: 'Female' })).toHaveAttribute('aria-pressed', 'true');
  await page.locator('.ai-status summary', { hasText: 'What changed' }).click();
  await expect(page.locator('.ai-status .change-kept_locked')).toContainText('BPM');
  await expect(page.locator('#music-conditioning')).toContainText('Vocals (your lyrics are sung)');
  await axeCheck(page, 'music after Build with AI');
  // everything stays editable, and Undo restores the previous form
  await page.fill('#music-description', 'edited by hand');
  await page.click('[data-action="ai-undo"]');
  await expect(page.locator('#music-description')).toHaveValue('');
  await expect(form.locator('#music-tags .token-text')).toHaveCount(0);
  await expect(form.getByLabel('BPM', { exact: true })).toHaveValue('100');
  expect(problems).toEqual([]);
});

test('Improve My Prompt keeps locked fields and shows what changed', async ({ page }) => {
  // Improve is asked NOT to touch the lyrics (improve_lyrics: false) and proposes a
  // female vocal, so the form ends up "vocals requested, no lyrics". That is refused
  // by the conditioning preview on purpose (400) and shown as a callout: Improve must
  // not invent lyrics the user did not ask for, nor silently go instrumental.
  const problems = watchPage(page, { allow: [/status of 400/] });
  const form = await openCreate(page);
  await page.fill('#music-description', 'a song about rain');
  await page.fill('#music-prompt', 'my exact style words');
  await form.getByRole('button', { name: 'Keep Style prompt when using AI' }).click();
  const input = form.locator('#music-tags').getByLabel('Add a style tag');
  await input.fill('lo-fi');
  await input.press('Enter');
  await form.getByLabel('BPM', { exact: true }).fill('90');
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/ai/improve')),
    page.click('#music-ai-improve'),
  ]);
  expect(req.postDataJSON()).toMatchObject({ locked: ['style_prompt'], improve_lyrics: false,
    current: { description: 'a song about rain', style_prompt: 'my exact style words', style_tags: ['lo-fi'], bpm: 90 } });
  await expect(page.locator('.ai-status')).toContainText('Improved');
  await expect(page.locator('#music-description')).toHaveValue('A refined e2e description with more detail.');
  await expect(page.locator('#music-prompt')).toHaveValue('my exact style words');
  await expect(form.locator('#music-tags .token-text')).toHaveText(['lo-fi', 'deep house', 'warm pads', 'crisp drums']);
  await expect(form.getByLabel('BPM', { exact: true })).toHaveValue('90');
  await expect(form.getByLabel('Key', { exact: true })).toHaveValue('D minor');
  const changes = page.locator('.ai-status .change-list');
  await expect(changes.locator('.change-refined')).toContainText('a song about rain');
  await expect(changes.locator('.change-kept_locked')).toContainText('Style prompt');
  await expect(changes.locator('.change-suggested').filter({ hasText: 'BPM' })).toContainText('your value was kept');
  // the vocal contradiction Improve left behind is surfaced, not hidden
  await expect(form.locator('.vocal-status'))
    .toHaveText('Vocals need lyrics. Write them, pick “Write with AI”, or turn Instrumental on.');
  await expect(page.locator('#music-conditioning')).toContainText('This would be refused');
  await expect(page.locator('#music-conditioning')).toContainText('Vocals need lyrics');
  await axeCheck(page, 'music after Improve');
  expect(problems).toEqual([]);
});

test('Analyze Reference: measured upload and metadata-only YouTube', async ({ page }) => {
  test.setTimeout(120_000);
  const problems = watchPage(page);
  const form = await openCreate(page);
  await form.locator('summary', { hasText: 'Analyze Reference' }).click();
  const panel = page.locator('#music-reference');
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/music/upload')),
    panel.locator('input[type="file"]').setInputFiles({ name: 'ref-e2e.wav', mimeType: 'audio/wav', buffer: wavBuffer(2) }),
  ]);
  await expect(panel).toContainText('Chosen: ref-e2e');
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/reference/analyze')),
    page.click('#music-ref-analyze'),
  ]);
  expect(req.postDataJSON()).toMatchObject({ source: { kind: 'asset' }, understand: true, suggest: true });
  const measured = panel.locator('[data-block="measured"]');
  await expect(measured).toContainText('122 BPM', { timeout: 30_000 });
  await expect(measured).toContainText('A minor');
  await expect(measured).toContainText('Measured from the audio');
  await expect(panel.locator('[data-block="model"]')).toContainText('yes (en)');
  await expect(panel.locator('[data-block="suggestions"]')).toBeVisible({ timeout: 30_000 });
  await expect(panel.locator('[data-block="suggestions"]')).toContainText('Measured');
  await expect(panel).not.toContainText('hold the light');
  await axeCheck(page, 'music reference (audio)');
  await page.click('#music-ref-use');
  await expect(form.getByLabel('BPM', { exact: true })).toHaveValue('122');
  await expect(form.getByLabel('Key', { exact: true })).toHaveValue('A minor');
  await expect(page.locator('#music-description')).toHaveValue(/luxury travel anthem/);

  // YouTube: only public metadata, clearly labelled
  await panel.getByRole('group', { name: 'Reference source' }).getByRole('button', { name: 'YouTube' }).click();
  await panel.getByLabel('Link').fill('https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  await page.click('#music-ref-analyze');
  await expect(panel).toContainText('Metadata only — no audio analysed', { timeout: 30_000 });
  await expect(panel).toContainText('E2E Reference Session (Live)');
  await expect(panel).toContainText('not downloaded or analysed');
  await expect(panel.locator('[data-block="measured"]')).toHaveCount(0);
  await expect(panel.locator('[data-block="suggestions"]')).toContainText('Inferred by gx-auto');
  // a link to somewhere else is refused
  await panel.getByLabel('Link').fill('http://127.0.0.1:8088/api/keys');
  await page.click('#music-ref-analyze');
  await expect(panel.locator('.callout-danger')).toContainText('YouTube');
  expect(problems.filter((p) => !/status of 400/.test(p))).toEqual([]);
});

test('music create works at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const problems = watchPage(page);
  await openCreate(page);
  await noHorizontalOverflow(page);
  await page.locator('#form-create summary', { hasText: 'Analyze Reference' }).click();
  await noHorizontalOverflow(page);
  await expect(page.locator('#music-ai-build')).toBeVisible();
  await axeCheck(page, 'music create (phone)');
  expect(problems).toEqual([]);
});
