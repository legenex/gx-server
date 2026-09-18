// Live page (Build V3 LIV): session setup, a real tunnelled WebSocket session
// against the real gx-live supervisor (stub engine), a typed turn, a tool call
// executed on the Control Center, the transcript opt-in, a clean shutdown, and
// accessibility on desktop and phone.
//
// The microphone and camera are Chromium's fake devices; 127.0.0.1 is a secure
// context, so getUserMedia works exactly as it does on the HTTPS listener.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, noHorizontalOverflow, watchPage } from './helpers.js';

test.use({
  permissions: ['microphone', 'camera'],
  launchOptions: {
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
      '--autoplay-policy=no-user-gesture-required'],
  },
});

let openSession = null;

test.afterEach(async ({ page }) => {
  // gx-live holds the model for one session at a time: never leave one open,
  // and do not start the next test until the supervisor has really released it.
  if (!openSession) return;
  const sid = openSession;
  openSession = null;
  await page.request.post(`/api/live/sessions/${sid}/end`, { data: { reason: 'abandoned' } }).catch(() => {});
  for (let i = 0; i < 20; i += 1) {
    const res = await page.request.get(`/api/live/sessions/${sid}?refresh=1`).catch(() => null);
    const body = res ? await res.json().catch(() => null) : null;
    if (!body || body.state === 'ended') break;
    await new Promise((r) => setTimeout(r, 500));
  }
});

async function startSession(page) {
  // gx-live runs one session at a time; a previous test's session may still be
  // closing on the supervisor, which answers 409 session_busy.
  let resp = null;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/live/sessions') && r.request().method() === 'POST'),
      page.locator('#live-start').click(),
    ]);
    if (resp.status() !== 409) break;
    await page.waitForTimeout(1000);
  }
  expect(resp.status()).toBe(201);
  const created = await resp.json();
  openSession = created.session_id;
  await expect(page.locator('#live-state')).toContainText('Listening', { timeout: 30_000 });
  return created.session_id;
}

async function say(page, text) {
  await page.fill('#live-text', text);
  await page.locator('#live-send').click();
}

async function endSession(page) {
  page.once('dialog', (d) => d.dismiss().catch(() => {}));
  await page.locator('#live-end').click();
  const confirm = page.getByRole('button', { name: 'End session', exact: true }).last();
  if (await confirm.isVisible().catch(() => false)) await confirm.click();
  await expect(page.locator('#live-state')).toContainText('Ended', { timeout: 20_000 });
  openSession = null;
}

test('live page: layout, session options and accessibility', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'live');
  await expect(page.locator('#page-live h1')).toHaveText('Live');
  await expect(page.locator('#live-state')).toContainText('Not started');
  await expect(page.getByLabel('Language')).toHaveValue('en');
  await expect(page.getByLabel('Personality and rules')).toBeVisible();
  await expect(page.getByRole('switch', { name: /tools/i })).toBeChecked();
  await expect(page.getByRole('switch', { name: /Speak the answers/i })).toBeChecked();
  await expect(page.locator('#live-transcript')).toContainText('Start the session');
  // controls that need a session are disabled until there is one
  await expect(page.locator('#live-mute')).toBeDisabled();
  await expect(page.locator('#live-camera')).toBeDisabled();
  await expect(page.locator('#live-interrupt')).toBeDisabled();
  await expect(page.locator('#live-end')).toBeHidden();
  // the four tools are named for the person, and gx-max is not among them
  const side = page.locator('.live-side');
  await expect(side).toContainText('Nothing has run yet');
  const body = await page.content();
  expect(body).not.toContain('gx-max ');
  await axeCheck(page, 'live idle');
  await noHorizontalOverflow(page);
  expect(problems).toEqual([]);
});

test('live page: a session answers a typed turn and ends cleanly', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'live');
  const sid = await startSession(page);
  // Both AudioWorklets registered: startMicCapture and createSpeaker await
  // audioWorklet.addModule(), and either failure would have stopped the start
  // or raised the "Audio playback is unavailable" toast.
  await expect(page.locator('.toast')).toHaveCount(0);
  expect(sid).toMatch(/^live_[0-9a-f]{32}$/);
  await expect(page.locator('#live-end')).toBeVisible();
  await expect(page.locator('#live-mute')).toBeEnabled();
  // the setup form is locked while the session runs
  await expect(page.getByLabel('Language')).toBeDisabled();

  await say(page, 'hello there');
  await expect(page.locator('#live-transcript .live-line-user').last()).toContainText('hello there');
  await expect(page.locator('#live-transcript .live-line-assistant').last()).toContainText('hello',
    { timeout: 30_000 });
  await expect(page.locator('#live-metrics')).toContainText('Turns');
  // the reply's 24 kHz PCM reached the browser and was queued for playback
  await expect(page.locator('#live-metrics')).toContainText('Assistant audio', { timeout: 30_000 });
  await expect(page.locator('#live-metrics')).toContainText('First audio (median)');
  // the turn timings reach the Control Center
  await expect.poll(async () => {
    const view = await (await page.request.get(`/api/live/sessions/${sid}`)).json();
    return (view.turn_log || []).length;
  }, { timeout: 30_000 }).toBeGreaterThan(0);

  // mute and camera are real switches
  await page.locator('#live-mute').click();
  await expect(page.locator('#live-mute')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#live-camera').click();
  await expect(page.locator('#live-camera')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#live-video-wrap')).toBeVisible();
  await expect.poll(() => page.locator('#live-video').evaluate((v) => v.videoWidth > 0),
    { timeout: 20_000 }).toBe(true);
  await axeCheck(page, 'live running');

  await endSession(page);
  const view = await (await page.request.get(`/api/live/sessions/${sid}`)).json();
  expect(view.state).toBe('ended');
  expect(view.end_reason).toBe('completed');
  expect(problems).toEqual([]);
});

test('live page: a tool call runs on the Control Center and is shown', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'live');
  const sid = await startSession(page);
  await say(page, 'use a tool');
  const tool = page.locator('#live-tools .live-tool').first();
  await expect(tool).toContainText('delegate_to_gx', { timeout: 40_000 });
  await expect(tool).toHaveAttribute('data-state', 'ok', { timeout: 60_000 });
  await expect(tool).toContainText('gx-fast');
  await expect.poll(async () => {
    const view = await (await page.request.get(`/api/live/sessions/${sid}`)).json();
    return view.tool_calls;
  }, { timeout: 30_000 }).toBe(1);
  const view = await (await page.request.get(`/api/live/sessions/${sid}`)).json();
  expect(view.tools[0].name).toBe('delegate_to_gx');
  expect(view.tools[0].arguments.sort()).toEqual(['model', 'task']); // names only, never the task text
  await axeCheck(page, 'live tools');
  await endSession(page);
  expect(problems).toEqual([]);
});

test('live page: the transcript is saved only when asked', async ({ page }) => {
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'live');
  const sid = await startSession(page);
  await say(page, 'remember this');
  await expect(page.locator('#live-transcript .live-line-assistant').last()).toContainText('hello',
    { timeout: 30_000 });
  let stored = await (await page.request.get(`/api/live/sessions/${sid}/transcript`)).json();
  expect(stored.saved).toBe(false);
  expect(stored.entries).toEqual([]);
  await page.locator('#live-save').click();
  await expect(page.locator('.toast')).toContainText('Transcript saved');
  stored = await (await page.request.get(`/api/live/sessions/${sid}/transcript`)).json();
  expect(stored.saved).toBe(true);
  expect(stored.entries.map((e) => e.speaker)).toContain('user');
  expect(stored.entries.some((e) => e.text.includes('remember this'))).toBe(true);
  await endSession(page);
  expect(problems).toEqual([]);
});

test('live page: a late speech transcript is put in front of its answer (B-LIV-6)', async ({ page }) => {
  // A SPOKEN turn's transcript only arrives once the reply is already running
  // (PROTOCOL.md section 4), so appending it makes the conversation read
  // backwards. This drives that exact protocol order through the stub engine --
  // response.started, then transcript.user(source=speech) -- and asserts the
  // ordering in BOTH the DOM and the saved transcript. Typed turns cannot
  // reproduce it, which is why the fix went unverified.
  test.setTimeout(180_000);
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'live');
  const sid = await startSession(page);

  await say(page, 'late transcript');
  await expect(page.locator('#live-transcript .live-line-user')).toContainText('USER QUESTION',
    { timeout: 30_000 });
  await expect(page.locator('#live-transcript .live-line-assistant')).toContainText('ASSISTANT RESPONSE',
    { timeout: 30_000 });

  // DOM order: the question must come before the answer it belongs to.
  const speakers = await page.locator('#live-transcript .live-line').evaluateAll(
    (nodes) => nodes.map((n) => `${n.dataset.speaker}:${n.textContent}`));
  const userAt = speakers.findIndex((t) => t.startsWith('user:') && t.includes('USER QUESTION'));
  const botAt = speakers.findIndex((t) => t.startsWith('assistant:') && t.includes('ASSISTANT RESPONSE'));
  expect(userAt).toBeGreaterThanOrEqual(0);
  expect(botAt).toBeGreaterThanOrEqual(0);
  expect(userAt).toBeLessThan(botAt);

  // ...and the persisted transcript must agree with what was on screen.
  await page.locator('#live-save').click();
  await expect(page.locator('.toast')).toContainText('Transcript saved');
  const stored = await (await page.request.get(`/api/live/sessions/${sid}/transcript`)).json();
  expect(stored.saved).toBe(true);
  const sUser = stored.entries.findIndex((e) => e.speaker === 'user' && e.text.includes('USER QUESTION'));
  const sBot = stored.entries.findIndex((e) => e.speaker === 'assistant' && e.text.includes('ASSISTANT RESPONSE'));
  expect(sUser).toBeGreaterThanOrEqual(0);
  expect(sBot).toBeGreaterThanOrEqual(0);
  expect(sUser).toBeLessThan(sBot);

  await endSession(page);
  expect(problems).toEqual([]);
});

test('live page: phone layout and accessibility', async ({ page }) => {
  const problems = watchPage(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await gotoPage(page, 'live');
  await expect(page.locator('#page-live h1')).toHaveText('Live');
  await noHorizontalOverflow(page);
  await axeCheck(page, 'live phone');
  expect(problems).toEqual([]);
});
