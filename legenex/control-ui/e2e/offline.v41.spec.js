// Hermetic V4.1 browser tests: the rebuilt single-model dashboard against
// fixture_server_v41.py (real backend code, stub upstreams). A few focused
// specs for the Overview, Model and Requests pages plus a whole-app smoke
// pass. Run: npx playwright test -c e2e/playwright.v41.config.js
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, watchPage } from './helpers.js';

const PASSWORD = process.env.GX_E2E_PASSWORD;

// helpers.js login() asserts the pre-V4.1 default page ('Dashboard'); this
// suite logs in itself because the rebuilt default page is Overview.
async function login(page, password) {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', 'admin');
  await page.fill('#login-pass', password);
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
  await expect(page.locator('.page-title')).toHaveText('Overview');
}

const PAGES_V41 = [
  ['overview', 'Overview'],
  ['model', 'Model'],
  ['performance', 'Performance'],
  ['requests', 'Requests'],
  ['agents', 'Agents'],
  ['tasks', 'Tasks'],
  ['projects', 'Projects'],
  ['files', 'Files'],
  ['storage', 'Storage'],
  ['logs', 'Logs'],
  ['network', 'Network'],
  ['updates', 'Updates'],
  ['settings', 'Settings / System'],
  ['recovery', 'Recovery'],
  ['jobs', 'Jobs / Actions'],
  ['keys', 'API Keys'],
];

test.describe.configure({ mode: 'serial' });

test('login gate still holds (auth endpoints unchanged)', async ({ page, request }) => {
  for (const path of ['/api/overview', '/api/models', '/api/requests', '/api/system']) {
    expect(await (await request.get(path)).status(), path).toBe(401);
  }
  await login(page, PASSWORD);
  await expect(page.locator('.page-title')).toHaveText('Overview');
});

test('every rebuilt page renders with real data and no console noise', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  for (const [name, title] of PAGES_V41) {
    await gotoPage(page, name, title);
  }
  // The only console noise tolerated anywhere in the suite: probes of the
  // deliberately-absent upstreams must never appear.
  expect(problems).toEqual([]);
});

test('overview: single-model facts, both nodes, queue and quick actions', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'overview', 'Overview');
  const main = page.locator('#page-overview');
  await expect(main.getByRole('heading', { name: 'gx10-01' })).toBeVisible();
  await expect(main.getByRole('heading', { name: 'gx10-02' })).toBeVisible();
  // model + alias tiles: the two packs and gx-max / gx-auto, nothing else
  await expect(main.locator('.model-tile .model-name')).toHaveCount(4);
  for (const name of ['dsv41-flash-exl3-stock', 'dsv41-flash-exl3-uncensored', 'gx-max', 'gx-auto']) {
    await expect(main.locator('.model-tile .model-name').getByText(name, { exact: true })).toHaveCount(1);
  }
  await expect(main.locator('.badge', { hasText: 'uncensored' }).first()).toBeVisible();
  await expect(main.locator('.badge', { hasText: 'production' }).first()).toBeVisible();
  // queue from the scheduler stub (1 queued, 1 active record)
  await expect(main.getByText(/queued 1 · active 1/)).toBeVisible();
  // both rails with registry fabric IPs
  await expect(main.getByText('192.168.100.10')).toBeVisible();
  await expect(main.getByText('192.168.101.11')).toBeVisible();
  // quick actions exist and are wired to the audited action set
  for (const label of ['Start gx-max (profile)', 'Restart gx-max (profile)', 'Stop gx-max (graceful release)',
    'Drain gx-max']) {
    await expect(main.getByRole('button', { name: label, exact: true })).toBeEnabled();
  }
  await axeCheck(page, 'overview');
});

test('model: registry packs, profiles with effective values, reasoning ladder, typed confirmation', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await gotoPage(page, 'model', 'Model');
  const main = page.locator('#page-model');
  await expect(main.getByText('dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw')).toBeVisible();
  await expect(main.getByText('Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw')).toBeVisible();
  await expect(main.getByText('served through gx-max')).toBeVisible();
  // reasoning ladder straight from the registry
  for (const level of ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']) {
    await expect(main.getByText(level, { exact: true }).first()).toBeVisible();
  }
  // profiles with effective values; the fixture registry has fast/balanced/swarm
  await expect(main.locator('.profile-btn')).toHaveCount(3);
  await expect(main.locator('.profile-btn[data-profile="fast"]')).toContainText('max_num_seqs 1');
  await expect(main.locator('.profile-btn[data-profile="swarm"]')).toContainText('reasoning low');
  // clicking a profile opens the typed gx-max confirmation; cancel it
  await main.locator('.profile-btn[data-profile="fast"]').click();
  const dialog = page.locator('#confirm-dialog');
  await expect(dialog).toBeVisible();
  const ok = page.locator('#confirm-ok');
  await expect(ok).toBeDisabled();
  await page.fill('#confirm-phrase', 'gx-ma');
  await expect(ok).toBeDisabled();
  await page.fill('#confirm-phrase', 'gx-max');
  await expect(ok).toBeEnabled();
  await page.click('#confirm-cancel');
  await expect(dialog).toBeHidden();
  expect(problems).toEqual([]);
});

test('requests: history rows, honest privacy note, filters, cancel/retry wiring', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'requests', 'Requests');
  const main = page.locator('#page-requests');
  // the scheduler stub relays real (stub) records
  await expect(main.getByText('req-done-0')).toBeVisible();
  await expect(main.getByText('req-queued-1')).toBeVisible();
  // privacy note is shown verbatim
  await expect(main).toContainText('prompt bodies are never stored');
  // a queued row offers Cancel; a done row does not
  await expect(main.locator('button[data-cancel="req-queued-1"]')).toBeEnabled();
  await expect(main.locator('button[data-cancel="req-done-0"]')).toHaveCount(0);
  // an errored row offers Retry
  await expect(main.locator('button[data-retry="req-err-1"]')).toBeEnabled();
  // state filter narrows the table
  await page.selectOption('#req-state', 'error');
  await page.getByRole('button', { name: 'Apply' }).click();
  await expect(main.getByText('req-err-1')).toBeVisible();
  await expect(main.getByText('req-done-0')).toHaveCount(0);
  await page.selectOption('#req-state', '');
  await page.getByRole('button', { name: 'Apply' }).click();
  await expect(main.getByText('req-done-0')).toBeVisible();
});

test('agents and tasks: coarse states only, no fabricated controls', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'agents', 'Agents');
  const main = page.locator('#page-agents');
  await expect(main.getByText('solver', { exact: true })).toBeVisible();
  await expect(main.getByText('reviewer', { exact: true })).toBeVisible();
  // scheduler attribution merged in
  await expect(main.getByText(/active 1 · queued 0/)).toBeVisible();
  // the upstream truth: no pause/resume/cancel anywhere
  for (const bad of ['Pause', 'Resume', /cancel/i]) {
    await expect(main.getByRole('button', { name: bad })).toHaveCount(0);
  }
  await expect(main.getByText('not supported by AgentOS', { exact: false })).toBeVisible();
  await gotoPage(page, 'tasks', 'Tasks');
  const tasks = page.locator('#page-tasks');
  await expect(tasks.getByText('Ship V4.1')).toBeVisible();
  await expect(tasks.getByText('Wire scheduler caps')).toBeVisible();
  await expect(tasks.locator('.kanban-card')).toHaveCount(3);
  await expect(tasks.getByText('req-active-1')).toBeVisible();
});
