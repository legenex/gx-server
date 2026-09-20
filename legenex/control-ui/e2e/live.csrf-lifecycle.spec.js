import { expect, test } from '@playwright/test';
import { login, watchPage } from './helpers.js';
import { apiGet, livePassword, modelOperation, waitJobDone, waitModelState } from './live-helpers.js';

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ page }) => {
  await login(page, livePassword(), process.env.GX_UI_USER || 'acceptance');
});

async function csrfToken(page) {
  const s = await apiGet(page, '/api/session');
  expect(s.authenticated).toBe(true);
  expect(s.csrf).toBeTruthy();
  return s.csrf;
}

async function postAction(page, name, body, { csrf, origin } = {}) {
  return page.evaluate(async ({ n, b, c, o }) => {
    const headers = { 'Content-Type': 'application/json' };
    if (c) headers['X-CSRF-Token'] = c;
    if (o) {
      headers.Origin = o;
      headers.Referer = `${o}/`;
    }
    const r = await fetch(`/api/actions/${n}`, {
      method: 'POST', headers, body: JSON.stringify(b || {}), credentials: 'same-origin',
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* raw */ }
    return { status: r.status, json, text: text.slice(0, 400) };
  }, { n: name, b: body, c: csrf, o: origin });
}

test('CSRF: missing and invalid tokens are rejected; valid lifecycle works in browser', async ({ page }) => {
  test.setTimeout(25 * 60_000);
  const problems = watchPage(page);
  const csrf403 = [];
  page.on('response', async (res) => {
    if (res.url().includes('/api/') && res.status() === 403) {
      const body = await res.text().catch(() => '');
      csrf403.push(`csrf-403 ${res.url()} ${body.slice(0, 200)}`);
    }
  });

  const origin = new URL(page.url()).origin;
  const csrf = await csrfToken(page);

  // Missing CSRF → 403
  const missing = await postAction(page, 'model.gx-mini.restart', {}, { origin });
  expect(missing.status, missing.text).toBe(403);
  expect(String(missing.json?.error?.code || missing.text)).toMatch(/csrf/i);

  // Invalid CSRF → 403
  const invalid = await postAction(page, 'model.gx-mini.restart', {}, {
    csrf: 'x'.repeat(43), origin,
  });
  expect(invalid.status, invalid.text).toBe(403);
  expect(String(invalid.json?.error?.code || invalid.text)).toMatch(/csrf/i);

  // Ensure gx-mini is in a restartable UI state before button ops
  await page.goto('/#/models/gx-mini');
  await expect(page.locator('#model-gx-mini')).toBeVisible();
  const before = await waitModelState(page, 'gx-mini',
    ['loaded', 'unloaded', 'ready', 'degraded', 'error'], 3 * 60_000);
  expect(['loaded', 'unloaded', 'ready', 'degraded', 'error']).toContain(before.state);

  // Valid CSRF via real UI: Restart
  await modelOperation(page, 'gx-mini', 'restart');
  const restart = await waitJobDone(page, 12 * 60_000);
  expect(restart.state, restart.output?.join?.('\n') || JSON.stringify(restart)).toBe('succeeded');
  await waitModelState(page, 'gx-mini', ['loaded'], 10 * 60_000);
  const afterRestart = (await apiGet(page, '/api/models')).models.find((m) => m.alias === 'gx-mini');
  expect(afterRestart.state).toBe('loaded');

  // Unload via UI
  await modelOperation(page, 'gx-mini', 'unload');
  const unload = await waitJobDone(page, 8 * 60_000);
  expect(unload.state, unload.output?.join?.('\n') || JSON.stringify(unload)).toBe('succeeded');
  await waitModelState(page, 'gx-mini', ['unloaded'], 8 * 60_000);
  const afterUnload = (await apiGet(page, '/api/models')).models.find((m) => m.alias === 'gx-mini');
  expect(afterUnload.state).toBe('unloaded');
  await expect(page.locator('#model-gx-mini')).toContainText(/unloaded|starts on the next request/i);

  // Load via UI
  await modelOperation(page, 'gx-mini', 'load');
  const load = await waitJobDone(page, 10 * 60_000);
  expect(load.state, load.output?.join?.('\n') || JSON.stringify(load)).toBe('succeeded');
  await waitModelState(page, 'gx-mini', ['loaded'], 10 * 60_000);
  const afterLoad = (await apiGet(page, '/api/models')).models.find((m) => m.alias === 'gx-mini');
  expect(afterLoad.state).toBe('loaded');

  // Deliberate missing/invalid CSRF probes above produce expected 403 console
  // noise. Only fail on CSRF 403s from the real UI button lifecycle path.
  const uiCsrf = csrf403.filter((p) => !/\/api\/actions\/model\.gx-mini\./.test(p));
  expect(uiCsrf, uiCsrf.join('\n')).toEqual([]);
  const fatal = problems.filter((p) => /pageerror:|CSP violation/i.test(p));
  expect(fatal, fatal.join('\n')).toEqual([]);
});
