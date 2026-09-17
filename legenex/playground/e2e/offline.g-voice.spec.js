// Voice Studio (Build V3 VOI): speak with takes, design -> saved voice, clone with
// consent, dialogue, Library save, playback, API panel, accessibility, phone layout.
// The backend is the real Control Center with the real gx-voice supervisor API
// (control-ui/e2e/voice_stub.py): takes are short sine-wave WAVs.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, noHorizontalOverflow, watchPage, wavBuffer } from './helpers.js';

const TABS = ['Speak', 'Design', 'Clone', 'Dialogue'];

async function waitForTakes(page, jobId, count) {
  const set = page.locator(`#voice-takes .take-set[data-job="${jobId}"]`);
  await expect(set.locator('.take')).toHaveCount(count, { timeout: 60_000 });
  return set;
}

async function submitAndGetJob(page, buttonName) {
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/voice/jobs') && r.request().method() === 'POST'),
    page.getByRole('button', { name: buttonName, exact: true }).click(),
  ]);
  expect(resp.status()).toBe(202);
  return (await resp.json()).id;
}

test('studio layout, tabs and accessibility', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'voice');
  await expect(page.locator('#page-voice h1')).toHaveText('Voice');
  const tabs = page.getByRole('tablist', { name: 'Voice mode' }).getByRole('tab');
  await expect(tabs).toHaveText(TABS);
  await expect(page.getByLabel('Voice', { exact: true })).toBeVisible();
  await expect(page.locator('#voice-select optgroup[label="Preset voices"] option')).toHaveCount(9);
  await axeCheck(page, 'voice speak');
  for (const name of TABS.slice(1)) {
    await page.getByRole('tab', { name }).click();
    await expect(page.locator(`#form-${name.toLowerCase()}`)).toBeVisible();
    await axeCheck(page, `voice ${name}`);
  }
  // API panel: examples with a placeholder, never a real key
  await page.locator('summary', { hasText: 'API access' }).click();
  const code = page.locator('.voice-code').first();
  await expect(code).toContainText('/v1/audio/speech');
  await expect(code).toContainText('$GX_API_KEY');
  expect(await page.locator('.voice-code').allTextContents()).not.toContain(expect.stringMatching(/sk-[A-Za-z0-9]{16,}/));
  await axeCheck(page, 'voice api panel');
  expect(problems).toEqual([]);
});

test('speak: two takes, playback, download and save to Library', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'voice');
  await page.getByRole('tab', { name: 'Speak' }).click();
  await page.locator('#voice-select').selectOption('preset:ryan');
  await expect(page.locator('#form-speak .voice-hint')).toContainText('Dynamic male voice');
  await page.fill('#voice-script', 'Welcome to the grand opening. Doors open at nine.\n\nSee you there!');
  await expect(page.locator('#form-speak')).toContainText('12 words');
  await page.locator('#form-speak').getByRole('button', { name: 'Advertising' }).click();
  await page.locator('#form-speak').getByRole('button', { name: 'excited' }).click();
  await page.locator('#form-speak').getByRole('group', { name: 'Takes' }).getByRole('button', { name: '2' }).click();
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/voice/jobs') && r.method() === 'POST'),
    page.locator('#voice-submit').click(),
  ]);
  const body = req.postDataJSON();
  expect(body).toMatchObject({ operation: 'tts', voice_id: 'preset:ryan', takes: 2 });
  expect(body.instructions).toContain('advertising');
  expect(body.instructions).toContain('excited tone');
  const jobId = (await (await req.response()).json()).id;
  const set = await waitForTakes(page, jobId, 2);
  await expect(page.locator(`#voice-jobs [data-job="${jobId}"] .phase-badge`)).toHaveAttribute('data-phase', 'COMPLETE');
  // play take 1 in the browser
  const take = set.locator('.take').first();
  await take.getByRole('button', { name: /^Play / }).click();
  await expect.poll(async () => take.locator('audio').evaluate((a) => a.currentTime > 0 || a.ended), { timeout: 20_000 }).toBe(true);
  await expect(take.getByRole('link', { name: /Download take 1 as WAV/ })).toHaveAttribute('href', /format=wav&download=1/);
  // save take 2
  const second = set.locator('.take').nth(1);
  await second.getByRole('button', { name: 'Save to Library' }).click();
  await expect(second.getByRole('button', { name: 'In Library' })).toBeVisible();
  const lib = await (await page.request.get('/api/media/assets?type=audio&operation=tts')).json();
  expect(lib.items.some((a) => a.source_ref === `${jobId}#1`)).toBe(true);
  await axeCheck(page, 'voice takes');
  expect(problems).toEqual([]);
});

test('design a voice, save it, and speak with it', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'voice');
  await page.getByRole('tab', { name: 'Design' }).click();
  await page.locator('#form-design').getByRole('button', { name: /Gravelly old sea captain/ }).click();
  await expect(page.locator('#voice-description')).toHaveValue(/sea captain/);
  const jobId = await submitAndGetJob(page, 'Design voice');
  const set = await waitForTakes(page, jobId, 2);
  await set.locator('.take').nth(1).getByRole('button', { name: 'Save as voice' }).click();
  const dialog = page.getByRole('dialog', { name: /Save take 2 as a voice/ });
  await dialog.getByRole('button', { name: 'Save voice' }).click();
  await expect(dialog.getByRole('alert')).toContainText('Give the voice a name');
  await dialog.getByLabel('Voice name').fill('E2E Captain');
  await dialog.getByRole('button', { name: 'Save voice' }).click();
  await expect(dialog).toBeHidden();
  const card = page.locator('#voice-library .voice-card', { hasText: 'E2E Captain' });
  await expect(card).toBeVisible();
  await expect(card).toContainText('Designed');
  // the designed voice is selected in Speak; style controls give way to the note
  await expect(page.locator('#voice-select option:checked')).toHaveText(/E2E Captain/);
  await page.getByRole('tab', { name: 'Speak' }).click();
  await expect(page.getByText('Style follows the voice')).toBeVisible();
  await page.fill('#voice-script', 'Hoist the sails, we leave at dawn.');
  const speakId = await submitAndGetJob(page, 'Generate');
  await waitForTakes(page, speakId, 1);
  // edit -> new version
  await card.getByRole('button', { name: 'Edit E2E Captain' }).click();
  const edit = page.getByRole('dialog', { name: 'Edit E2E Captain' });
  await edit.getByLabel('Description').fill('A weathered captain for audiobooks');
  await edit.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('#voice-library .voice-card', { hasText: 'E2E Captain' })).toContainText('v2');
  await page.locator('#voice-library .voice-card', { hasText: 'E2E Captain' }).getByRole('button', { name: 'Versions of E2E Captain' }).click();
  await expect(page.getByRole('dialog', { name: /Versions/ }).locator('.version')).toHaveCount(2);
  await page.keyboard.press('Escape');
  expect(problems).toEqual([]);
});

test('clone requires permission, then test and save the clone', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page, { allow: [/status of 403/] });
  await login(page);
  await gotoPage(page, 'voice');
  await page.getByRole('tab', { name: 'Clone' }).click();
  const form = page.locator('#form-clone');
  await form.locator('input[type=file]').setInputFiles({ name: 'my-voice.wav', mimeType: 'audio/wav', buffer: wavBuffer(3) });
  await expect(form.locator('#clone-ref .source-chip')).toBeVisible();
  await form.getByLabel('Transcript of the clip').fill('A short sample of my own voice.');
  await page.locator('#voice-submit').click();
  await expect(form.getByRole('alert')).toContainText('permission');
  await form.getByLabel(/Permission to clone/).check();
  const jobId = await submitAndGetJob(page, 'Test clone');
  await waitForTakes(page, jobId, 1);
  await form.getByLabel('Voice name').fill('E2E Clone');
  await form.getByRole('button', { name: 'Save as voice' }).click();
  await expect(page.locator('#voice-library .voice-card', { hasText: 'E2E Clone' })).toContainText('Cloned');
  // a clone is never re-run from history without a fresh confirmation
  await expect(page.locator(`#voice-takes .take-set[data-job="${jobId}"]`).getByRole('button', { name: 'Use settings' })).toHaveCount(0);
  await axeCheck(page, 'voice clone');
  expect(problems).toEqual([]);
});

test('dialogue with two speakers and History integration', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'voice');
  await page.getByRole('tab', { name: 'Dialogue' }).click();
  const lines = page.locator('#dialogue-lines > li');
  await expect(lines).toHaveCount(2);
  await page.locator('#dialogue-add').click();
  await expect(lines).toHaveCount(3);
  await lines.nth(2).getByLabel('Line', { exact: true }).fill('And that is a wrap!');
  await lines.nth(2).getByRole('button', { name: 'Move line up' }).click();
  await expect(lines.nth(1).getByLabel('Line', { exact: true })).toHaveValue('And that is a wrap!');
  await lines.nth(1).getByRole('button', { name: 'Remove line' }).click();
  await expect(lines).toHaveCount(2);
  const jobId = await submitAndGetJob(page, 'Generate dialogue');
  await waitForTakes(page, jobId, 1);
  await gotoPage(page, 'history');
  await page.getByRole('group', { name: 'Kind' }).getByRole('button', { name: 'Voice' }).click();
  await expect(page.locator(`#history-list [data-job="${jobId}"]`)).toContainText('Dialogue');
  expect(problems).toEqual([]);
});

test('phone layout: no horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await gotoPage(page, 'voice');
  await noHorizontalOverflow(page);
  for (const name of TABS.slice(1)) {
    await page.getByRole('tab', { name }).click();
    await noHorizontalOverflow(page);
  }
  await axeCheck(page, 'voice phone');
});
