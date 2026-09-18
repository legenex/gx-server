// Hermetic browser tests: real backend code, synthetic cluster, stub upstreams.
import { expect, test } from '@playwright/test';
import { PAGES, axeCheck, gotoPage, login, watchPage } from './helpers.js';

const PASSWORD = process.env.GX_E2E_PASSWORD;

test.describe.configure({ mode: 'serial' });

test('login is required and wrong passwords are rejected', async ({ page, request }) => {
  const problems = watchPage(page);
  for (const path of ['/api/overview', '/api/models', '/api/logs/orchestrator', '/api/system']) {
    const res = await request.get(path);
    expect(res.status(), path).toBe(401);
  }
  const post = await request.post('/api/models/gx-max/load', { data: { confirm: 'gx-max' } });
  expect(post.status()).toBe(401);

  const res = await page.goto('/');
  const csp = res.headers()['content-security-policy'];
  expect(csp).toContain("script-src 'self'");
  expect(res.headers()['x-frame-options']).toBe('DENY');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-pass', 'definitely-not-the-password');
  await page.click('#login-submit');
  await expect(page.locator('#login-error')).toContainText('invalid username or password');
  await expect(page.locator('#app-view')).toBeHidden();
  // the only console noise allowed is the 401 of the failed login itself
  expect(problems.filter((p) => !p.includes('401'))).toEqual([]);
});

test('every page renders with real data, no console errors and no serious a11y violations', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  const counts = {};
  for (const [name, title] of PAGES) {
    await gotoPage(page, name, title);
    counts[name] = await axeCheck(page, name);
  }
  console.log('axe (minor/moderate) violation counts per page:', JSON.stringify(counts));
  expect(problems).toEqual([]);
});

test('dashboard shows both nodes, rails, the canonical aliases and git sync', async ({ page }) => {
  await login(page, PASSWORD);
  const main = page.locator('#page-dashboard');
  await expect(main.getByRole('heading', { name: 'gx10-01' })).toBeVisible();
  await expect(main.getByRole('heading', { name: 'gx10-02' })).toBeVisible();
  await expect(main.locator('.model-tile')).toHaveCount(11);
  await expect(main.locator('#dash-playground')).toHaveAttribute('href', /:8090\/$/);
  await expect(main.getByText('HEALTHY').first()).toBeVisible();
  for (const alias of ['gx-mini', 'gx-fast', 'gx-reason', 'gx-max', 'gx-auto', 'gx-image', 'gx-video',
    'gx-music', 'gx-voice', 'gx-call', 'gx-live']) {
    await expect(main.locator('.model-tile .model-name').getByText(alias, { exact: true })).toHaveCount(1);
  }
  await expect(main.getByText('All three HEADs match')).toBeVisible();
  await expect(main.getByText('Rail 1 (192.168.100.0/24)')).toBeVisible();
  await expect(main.getByText('Rail 2 (192.168.101.0/24)')).toBeVisible();
  await expect(main.getByText('there is no shared 256 GB pool', { exact: false })).toBeVisible();
  await expect(page.locator('#overall-status')).toContainText('Cluster:');
});

test('models page: sanctioned controls, gx-max details and typed confirmation', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'models', 'Models');
  await expect(page.locator('.model-card')).toHaveCount(11);
  const music = page.locator('#model-gx-music');
  await expect(music).toContainText('ACE-Step/acestep-v15-xl-turbo');
  await expect(music).toContainText('d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee');
  await expect(music).toContainText('music-generation');
  await expect(music).toContainText('extract');
  const gx = page.locator('#model-gx-max');
  await expect(gx).toContainText('nvidia/DeepSeek-V4-Flash-0731-NVFP4');
  await expect(gx).toContainText('TP=2 · nnodes=2 · rank 0 on gx10-01 · rank 1 on gx10-02');
  await expect(gx).toContainText('Startup transient');
  await expect(gx).toContainText('512 s');
  await expect(gx.getByRole('table', { name: 'RDMA counters' })).toContainText('rocep1s0f0');
  await expect(page.locator('#model-gx-auto')).toContainText('Routing alias');
  await expect(page.locator('#model-gx-mini').getByRole('button', { name: 'Load', exact: true })).toBeDisabled();

  await gx.getByRole('button', { name: 'Load', exact: true }).click();
  const dialog = page.locator('#confirm-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Drains gx-mini, gx-fast, gx-reason');
  const ok = page.locator('#confirm-ok');
  await expect(ok).toBeDisabled();
  await page.fill('#confirm-phrase', 'gx-ma');
  await expect(ok).toBeDisabled();
  await page.fill('#confirm-phrase', 'gx-max');
  await expect(ok).toBeEnabled();
  await page.click('#confirm-cancel');
  await expect(dialog).toBeHidden();

  // Force release is offered but refused server-side when nothing is running.
  await gx.getByRole('button', { name: 'Force release', exact: true }).click();
  await page.fill('#confirm-phrase', 'FORCE RELEASE');
  await page.click('#confirm-ok');
  await expect(page.locator('.toast').last()).toContainText('nothing to release');
});

test('runtime, cluster and jobs pages show live details', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'runtime', 'Runtime');
  await expect(page.locator('#page-runtime')).toContainText('/swapfile-sglang');
  await expect(page.getByRole('table', { name: 'Pressure stall information' }).first()).toContainText('avg10 %');
  await expect(page.getByRole('table', { name: 'llama-swap running' })).toContainText('gx-mini');
  await gotoPage(page, 'cluster', 'Cluster');
  await expect(page.locator('svg.topology')).toBeVisible();
  await expect(page.locator('#page-cluster')).toContainText('Two separate computers');
  await expect(page.locator('#page-cluster')).toContainText('Tailscale — management only');
  await gotoPage(page, 'jobs', 'Jobs / Queue');
  await expect(page.locator('.stepper').first()).toBeVisible();
  await expect(page.getByRole('table', { name: 'gx-max job history' })).toContainText('512 s');
  await expect(page.locator('.log-view')).toContainText('starting rank1 on node2');
});

test('logs page: predefined streams only, filter and download', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'logs', 'Logs');
  const buttons = page.locator('.stream-list button');
  expect(await buttons.count()).toBeGreaterThanOrEqual(20);
  await page.locator('.stream-list button', { hasText: 'gx-max safety samples' }).click();
  await expect(page.locator('#page-logs p[role=status]')).toContainText('gx-max safety samples');
  await page.fill('#log-query', 'nothing-matches-this');
  await page.keyboard.press('Enter');
  await expect(page.locator('#page-logs p[role=status]')).toContainText('0 line(s)');
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Download excerpt' }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^gx-.*\.log$/);
});

test('playground: chat, snippets, gx-max warning, image and video', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await gotoPage(page, 'playground', 'API Playground');
  await page.selectOption('#pg-model', 'gx-mini');
  await page.fill('#pg-prompt', 'What is 17*23?');
  await page.click('#pg-send');
  const out = page.locator('#chat-output');
  await expect(out.locator('.answer')).toHaveText('391');
  await expect(out).toContainText('prompt 12 · completion 3 · total 15');
  await expect(out).toContainText('Model used');
  await out.getByRole('tab', { name: 'Python' }).click();
  await expect(out.locator('.snippets pre')).toContainText('from openai import OpenAI');
  await out.getByRole('tab', { name: 'JavaScript' }).click();
  await expect(out.locator('.snippets pre')).toContainText('process.env.GX_API_KEY');
  await out.getByRole('tab', { name: 'curl' }).click();
  const curl = await out.locator('.snippets pre').innerText();
  expect(curl).toContain('http://100.105.214.61:4000/v1/chat/completions');
  expect(curl).toContain('$GX_API_KEY');
  expect(curl).not.toMatch(/sk-[A-Za-z0-9]{16,}/);

  await page.selectOption('#pg-model', 'gx-max');
  await expect(page.locator('#pg-max-warn')).toBeVisible();
  await expect(page.locator('#pg-image-row')).toBeHidden();
  await page.click('#pg-send');
  await expect(out).toContainText('Confirm the takeover');

  await page.click('#tab-image');
  await page.fill('#img-prompt', 'a red fox');
  await page.click('#img-send');
  await expect(page.locator('#gen-image-0')).toBeVisible();
  expect(await page.locator('#gen-image-0').evaluate((img) => img.naturalWidth)).toBe(64);

  await page.click('#tab-video');
  await page.fill('#vid-prompt', 'waves');
  await page.click('#vid-send');
  await expect(page.locator('#gen-video')).toBeVisible();
  expect(problems.filter((p) => !/409|Media|video/i.test(p))).toEqual([]);
});

test('docs: every required section is present, searchable and copyable', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'docs', 'Docs');
  const toc = page.locator('.doc-toc');
  for (const s of ['Getting started', 'Architecture', 'Which model should I use?', 'gx-mini', 'gx-fast',
    'gx-reason', 'gx-max', 'gx-auto', 'gx-image', 'gx-video', 'API quickstart', 'curl examples',
    'Python examples', 'JavaScript examples', 'Vision input', 'Tool calling', 'Image generation',
    'Video generation', 'Model loading / unloading', 'gx-max explained', 'Resource safety', 'Queueing',
    'Errors / troubleshooting', 'Git sync', 'Remote access', 'Log locations', 'Admin / recovery', 'FAQ']) {
    await expect(toc.getByRole('link', { name: s, exact: true }).first()).toBeVisible();
  }
  await toc.getByRole('link', { name: 'curl examples', exact: true }).click();
  await expect(page.locator('#d-curl-examples')).toBeVisible();
  await expect(page.locator('.doc .code-wrap .copy-btn').first()).toBeVisible();
  await page.fill('#doc-search', 'deadman');
  await expect(page.locator('.doc-results li').first()).toBeVisible();
  await page.locator('.toc-page', { hasText: 'Models' }).click();
  await expect(page.locator('.doc table').first()).toContainText('gx-reason');
});

test('settings: read-only facts, safe action runs, no dangerous buttons', async ({ page }) => {
  await login(page, PASSWORD);
  await gotoPage(page, 'settings', 'Settings / System');
  const body = page.locator('#page-settings');
  await expect(body).toContainText('6.17.0-1032-nvidia');
  await expect(body).toContainText('https://github.com/legenex/gx-server.git');
  for (const bad of [/upgrade/i, /firmware/i, /kernel update/i, /shell/i]) {
    await expect(body.getByRole('button', { name: bad })).toHaveCount(0);
  }
  await body.getByRole('button', { name: 'Refresh health' }).click();
  await expect(page.locator('.toast').last()).toContainText('Refresh health: succeeded');
  await expect(body.locator('.job-output')).toContainText('caches cleared');
  await expect(body).toContainText('GX_MEDIA_API_KEY');
  expect(await body.innerText()).not.toMatch(/sk-[A-Za-z0-9]{16,}/);
});

test('mobile layout: navigation drawer, no page-level horizontal scroll', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const page = await context.newPage();
  await login(page, PASSWORD);
  await expect(page.locator('#nav-toggle')).toBeVisible();
  await expect(page.locator('#sidenav')).not.toBeInViewport();
  for (const [name, title] of PAGES) {
    await page.click('#nav-toggle');
    await expect(page.locator('#sidenav')).toBeInViewport();
    await page.click(`#sidenav a[data-page="${name}"]`);
    await expect(page.locator('.page-title')).toHaveText(title);
    await expect(page.locator('.page .loading')).toHaveCount(0, { timeout: 60_000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${name} horizontal overflow`).toBeLessThanOrEqual(1);
  }
  await context.close();
});

test('theme toggle, keyboard skip link and logout', async ({ page }) => {
  await login(page, PASSWORD);
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.click('#theme-btn');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await axeCheck(page, 'dashboard light theme');
  await page.click('#theme-btn');
  await page.reload();
  await expect(page.locator('.page-title')).toHaveText('Dashboard');
  await page.keyboard.press('Tab');
  await expect(page.locator('.skip-link')).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main')).toBeFocused();
  await page.click('#logout-btn');
  await expect(page.locator('#login-view')).toBeVisible();
  const status = await page.evaluate(async () => (await fetch('/api/overview')).status);
  expect(status).toBe(401);
});

test('creative work links to GX-Playground (no second creative app here)', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await expect(page.locator('#nav-playground')).toHaveAttribute('href', /^http:\/\/127\.0\.0\.1:8090\/$/);
  await page.goto('/#/create');
  await expect(page.locator('.page-title')).toHaveText('GX-Playground');
  await expect(page.locator('#open-playground')).toHaveAttribute('href', 'http://127.0.0.1:8090/');
  await page.goto('/#/library');
  await expect(page.locator('#open-playground')).toBeVisible();
  expect(problems).toEqual([]);
});

test('resource control: profiles, live map, admission, compatibility, pin and maintenance', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await gotoPage(page, 'resources', 'Resource Control');
  await expect(page.locator('#active-profile')).toHaveText('Auto');
  for (const alias of ['gx-mini', 'gx-fast', 'gx-reason', 'gx-image', 'gx-video', 'gx-music']) {
    await expect(page.locator(`.rt-tile[data-alias="${alias}"]`).first()).toBeVisible();
  }
  await expect(page.getByRole('heading', { name: 'gx10-02' })).toBeVisible();
  // compatibility explains itself
  await page.locator('[data-pair="gx-reason|gx-video"]').click();
  await expect(page.locator('.compat-detail')).toContainText('gx-reason + gx-video');
  await expect(page.locator('.compat-detail')).toContainText('needs 102 GiB');
  await page.locator('[data-pair="gx-mini|gx-reason"]').click();
  await expect(page.locator('.compat-detail')).toContainText('different nodes');
  // harmless switch: no dialog
  await page.locator('[data-profile="text"]').click();
  await expect(page.locator('#active-profile')).toHaveText('Text / Agent', { timeout: 15_000 });
  // pin gx-music (node-2 guard write is stubbed by the fixture)
  await page.locator('[data-control="gx-music:pin"]').click();
  await expect(page.locator('[data-control="gx-music:unpin"]')).toBeVisible({ timeout: 15_000 });
  await page.locator('[data-control="gx-music:unpin"]').click();
  await expect(page.locator('[data-control="gx-music:pin"]')).toBeVisible({ timeout: 15_000 });
  // Max needs a typed confirmation: cancel it
  await page.locator('[data-profile="max"]').click();
  await expect(page.locator('#confirm-dialog')).toBeVisible();
  await expect(page.locator('#confirm-ok')).toBeDisabled();
  await page.click('#confirm-cancel');
  await expect(page.locator('#active-profile')).toHaveText('Text / Agent');
  // Maintenance: confirm, then end it
  await page.click('#enter-maintenance');
  await expect(page.locator('#confirm-dialog')).toBeVisible();
  await page.click('#confirm-ok');
  await expect(page.locator('#active-profile')).toHaveText('Maintenance', { timeout: 15_000 });
  await expect(page.locator('#exit-maintenance')).toBeVisible();
  await axeCheck(page, 'resources-maintenance');
  await page.click('#exit-maintenance');
  await expect(page.locator('#active-profile')).toHaveText('Auto', { timeout: 15_000 });
  expect(problems).toEqual([]);
});

test('storage: live health, scan with progress, safe cleanup with dry run, protected items locked', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await gotoPage(page, 'storage', 'Storage & Cleanup');
  await expect(page.locator('[data-health="node1"]')).toHaveText('HEALTHY');
  await expect(page.locator('[data-health="node2"]')).toHaveText('HEALTHY');
  await page.click('#scan-storage');
  await expect(page.locator('#scan-state')).toHaveText('done', { timeout: 30_000 });
  await expect(page.getByRole('heading', { name: 'SAFE TO CLEAN' })).toBeVisible();
  await page.click('#select-safe');
  await expect(page.locator('#clean-selected')).toBeEnabled();
  await page.click('#clean-selected');
  await expect(page.locator('#confirm-body')).toContainText('Dry run');
  await page.click('#confirm-ok');
  await expect(page.locator('#last-freed')).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: 'Show' }).click();
  await expect(page.locator('table').filter({ hasText: 'Why it is protected' })).toContainText('gx-max');
  await expect(page.locator('input[data-candidate]').filter({ has: page.locator('xpath=ancestor::table[.//th[text()="Why it is protected"]]') })).toHaveCount(0);
  await axeCheck(page, 'storage');
  expect(problems).toEqual([]);
});

test('setup: kilo, open webui and generic pages with live values and a real test', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await gotoPage(page, 'setup', 'Setup');
  await expect(page.locator('#kilo-base-url')).toHaveText('http://100.105.214.61:4000/v1');
  await expect(page.locator('#kilo-model')).toHaveText('gx-auto');
  await expect(page.getByText('"npm": "@ai-sdk/openai-compatible"')).toBeVisible();
  await expect(page.getByText('YOUR_GX_API_KEY')).toHaveCount(0);
  await page.fill('#test-key-kilo', 'not-a-key');
  await page.click('#test-kilo');
  await expect(page.locator('#test-result-kilo')).toContainText('paste a gateway key');
  await page.getByRole('tab', { name: 'Open WebUI' }).click();
  await expect(page.locator('#owui-url')).toHaveText('http://100.105.214.61:4000/v1');
  await expect(page.getByText('Manage OpenAI API Connections')).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'OpenAI-compatible clients' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('export GX_API_KEY=YOUR_GX_API_KEY')).toBeVisible();
  await page.fill('#test-key-generic', `sk-e2e-${'y'.repeat(30)}`);
  await page.click('#test-generic');
  await expect(page.locator('#test-verdict-generic')).toHaveText('CONNECTED');
  await page.getByRole('tab', { name: 'Kilo Code' }).click();
  await page.click('#create-key-kilo');
  await expect(page.locator('.page-title')).toHaveText('API Keys');
  await expect(page.locator('#key-name')).toHaveValue('kilo-code');
  await expect(page.locator('input[name=models][value="gx-auto"]')).toBeChecked();
  await expect(page.locator('input[name=models][value="gx-max"]')).not.toBeChecked();
  await expect(page.locator('input[name=models][value="gx-music"]')).not.toBeChecked();
  // the only console error is the deliberate 400 of the malformed key above
  expect(problems.filter((x) => !x.includes('400 (Bad Request)'))).toEqual([]);
  expect(problems.filter((x) => x.includes('400 (Bad Request)'))).toHaveLength(1);
});

test('api keys: create shows the secret once, list is masked, revoke needs the name', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await gotoPage(page, 'keys', 'API Keys');
  await expect(page.getByText('kilo-code')).toBeVisible();
  await page.click('#key-create');
  await expect(page.locator('#key-form .form-error')).toBeVisible();
  await page.fill('#key-name', 'e2e-client');
  await page.click('#key-create');
  await expect(page.locator('#new-key-secret')).toHaveValue(/^sk-e2e-/);
  await expect(page.getByText('cannot be shown again')).toBeVisible();
  const table = page.locator('table');
  await expect(table).toContainText('e2e-client');
  await expect(table).not.toContainText('sk-e2e-');
  await page.getByRole('button', { name: 'I have copied it: hide' }).click();
  await expect(page.locator('#new-key-secret')).toHaveCount(0);
  await page.locator('[data-revoke="e2e-client"]').click();
  await page.fill('#confirm-phrase', 'e2e-client');
  await page.click('#confirm-ok');
  await expect(page.locator('.toast').last()).toContainText('Revoked e2e-client');
  await expect(table).not.toContainText('e2e-client');
  expect(problems).toEqual([]);
});

test('model manager: inventory, alias bindings and delete protection', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, PASSWORD);
  await gotoPage(page, 'manager', 'Model Manager');
  await expect(page.locator('caption', { hasText: 'Alias bindings' })).toBeVisible();
  await expect(page.locator('table').first()).toContainText('dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4');
  const live = page.locator('tr', { hasText: 'Qwen3.5-4B-Uncensored-HauhauCS-Aggressive' }).filter({ hasText: 'gx10-01' }).last();
  await expect(live.getByRole('button', { name: 'Delete' })).toBeDisabled();
  const unused = page.locator('tr', { hasText: 'unused-e2e' });
  await expect(unused.getByRole('button', { name: 'Delete' })).toBeEnabled();
  await expect(page.getByText('Not configured')).toBeVisible();
  expect(problems).toEqual([]);
});
