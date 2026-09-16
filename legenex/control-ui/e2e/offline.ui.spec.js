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

test('dashboard shows both nodes, rails, seven aliases and git sync', async ({ page }) => {
  await login(page, PASSWORD);
  const main = page.locator('#page-dashboard');
  await expect(main.getByRole('heading', { name: 'gx10-01' })).toBeVisible();
  await expect(main.getByRole('heading', { name: 'gx10-02' })).toBeVisible();
  await expect(main.locator('.model-tile')).toHaveCount(7);
  for (const alias of ['gx-mini', 'gx-fast', 'gx-reason', 'gx-max', 'gx-auto', 'gx-image', 'gx-video']) {
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
  await expect(page.locator('.model-card')).toHaveCount(7);
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
