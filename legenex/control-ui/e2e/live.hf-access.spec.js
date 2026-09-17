// LIVE: the Model Manager tells the truth about the Hugging Face token and about
// per-repository FILE access (B-030, D-041).
//
// Metadata access and file access are gated separately: a gated repository
// answers 200 for /api/models/... and 403 for resolve/... until the account has
// been granted access. Collapsing those into one "access denied (a token with
// access is required)" message is what sent an earlier pass round in circles
// minting tokens for a gate no token can open. This asserts the deployed UI
// shows the live token state and, for the approved gx-reason target, names the
// real reason and the real human action.
//
//   npx playwright test --project=live e2e/live.hf-access.spec.js
//
// Read-only: it downloads nothing and changes no token.
import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

const REPO = 'iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070';
const REV = '91c3e3d4daf14f8e9389b95f43112410f06ed3d5';
const PASSWORD_FILE = '/srv/projects/gx-cluster/secrets/control-ui/acceptance-password';

test.describe.configure({ timeout: 180_000 });
test.use({ actionTimeout: 30_000 });

async function signIn(page) {
  await page.goto('/');
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', readFileSync(PASSWORD_FILE, 'utf8').trim());
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
}

test('the token panel shows live state and never the token', async ({ page }) => {
  await signIn(page);
  const state = await (await page.request.get('/api/manager/hf-token')).json();
  expect(state.configured, 'a token must be configured for this check').toBe(true);

  await page.goto('/#/manager');
  const panel = page.locator('#mm-token, [data-panel="hf-token"]').first();
  await expect(panel).toBeVisible();
  const text = await panel.innerText();

  // Live facts, not hard-coded sentences.
  expect(text).toMatch(/Configured and valid|Hugging Face rejected|Not configured/);
  if (state.valid) {
    expect(text).toContain(state.user);
    expect(text).toMatch(/Can read gated repos/i);
  }
  // The token itself is never sent to the browser.
  const body = await page.content();
  expect(body).not.toMatch(/hf_[A-Za-z0-9]{20,}/);
  // Replace/Remove are offered once a token exists.
  await expect(page.getByRole('button', { name: /Replace token|Save token/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Remove token' })).toBeVisible();
});

test('a gated repository reports the FILE gate, not a token problem', async ({ page }) => {
  await signIn(page);
  const { csrf } = await (await page.request.get('/api/session')).json();
  const origin = new URL(page.url()).origin;
  const res = await page.request.post('/api/manager/lookup', {
    headers: { 'X-CSRF-Token': csrf, Origin: origin, Referer: `${origin}/` },
    data: { reference: `${REPO}@${REV}` },
  });
  expect(res.status(), await res.text()).toBe(200);
  const info = await res.json();

  // Metadata succeeded, which proves nothing about the files.
  expect(info.revision).toBe(REV);
  expect(info.gated).toBeTruthy();
  expect(info.access, 'the lookup must carry a structured access verdict').toBeTruthy();

  if (info.access.ok) {
    // Access has been granted since this was written — that is the good outcome.
    expect(info.access.reason).toBe('granted');
    return;
  }
  expect(['gated_not_granted', 'unauthenticated', 'forbidden']).toContain(info.access.reason);
  expect(info.access.message, 'Hugging Face\'s own words are passed through').toBeTruthy();
  expect(info.access.action, 'the UI must state the exact human action').toBeTruthy();
  if (info.access.reason === 'gated_not_granted') {
    expect(info.access.http_status).toBe(403);
    expect(info.access.action).toMatch(/accept the model|request access/i);
    expect(info.access.action, 'must not send the operator to mint another token')
      .toContain('A new token cannot fix this');
  }
});
