// LIVE: every page against the deployed UI and the real cluster. Read-only.
import { expect, test } from '@playwright/test';
import { PAGES, axeCheck, gotoPage, login, watchPage } from './helpers.js';
import { apiGet, livePassword } from './live-helpers.js';

test.describe.configure({ mode: 'serial' });

test('unauthenticated access is rejected on the deployed UI', async ({ request }) => {
  for (const path of ['/api/overview', '/api/models', '/api/logs/orchestrator', '/api/system', '/api/jobs']) {
    expect((await request.get(path)).status(), path).toBe(401);
  }
  expect((await request.post('/api/models/gx-max/load', { data: { confirm: 'gx-max' } })).status()).toBe(401);
  expect((await request.post('/api/actions/system.refresh', { data: {} })).status()).toBe(401);
  const health = await request.get('/api/health');
  expect(health.status()).toBe(200);
  const ready = await (await request.get('/api/ready')).json();
  expect(ready.ready).toBe(true);
});

test('all pages load real data with no console errors', async ({ page }) => {
  const problems = watchPage(page);
  await login(page, livePassword());
  for (const [name, title] of PAGES) {
    await gotoPage(page, name, title);
    await axeCheck(page, `live ${name}`);
  }
  expect(problems).toEqual([]);
});

test('dashboard and runtime reflect both real nodes', async ({ page }) => {
  await login(page, livePassword());
  const ov = await apiGet(page, '/api/overview');
  expect(ov.nodes.map((n) => n.hostname)).toEqual(['gx10-01', 'gx10-02']);
  for (const n of ov.nodes) {
    expect(n.reachable, n.name).toBe(true);
    expect(n.kernel).toBe('6.17.0-1032-nvidia');
    expect(n.mem_total_gib).toBeGreaterThan(110);
    expect(n.swap_total_gib).toBeGreaterThan(60);
  }
  expect(ov.rails.every((r) => r.ok)).toBe(true);
  expect(ov.tailscale.level).toBe('ok');
  expect(ov.models.map((m) => m.alias)).toEqual(['gx-mini', 'gx-fast', 'gx-reason', 'gx-max', 'gx-auto', 'gx-image', 'gx-video']);
  expect(ov.git.node2_push_disabled).toBe(true);
  expect(ov.git.node1_head).toMatch(/^[0-9a-f]{40}$/);
  expect(ov.git.node2_head).toMatch(/^[0-9a-f]{40}$/);
  expect(ov.git.origin_main).toMatch(/^[0-9a-f]{40}$/);
  console.log(`live overview: overall=${ov.overall} heads n1=${ov.git.node1_head.slice(0, 8)} gh=${ov.git.origin_main.slice(0, 8)} n2=${ov.git.node2_head.slice(0, 8)} match=${ov.git.match}`);

  await gotoPage(page, 'dashboard', 'Dashboard');
  await expect(page.locator('#page-dashboard').getByRole('heading', { name: 'gx10-02' })).toBeVisible();
  await gotoPage(page, 'runtime', 'Runtime');
  await expect(page.locator('#page-runtime')).toContainText('/swapfile-sglang');
  await expect(page.locator('#page-runtime')).toContainText('gx-media-router');
  await gotoPage(page, 'cluster', 'Cluster');
  await expect(page.locator('#page-cluster')).toContainText('rocep1s0f0');
  await expect(page.locator('#page-cluster')).toContainText('roceP2p1s0f0');
  await expect(page.locator('#page-cluster')).toContainText('/s tx', { timeout: 30_000 });
});

test('logs load safely from both nodes', async ({ page }) => {
  await login(page, livePassword());
  for (const id of ['orchestrator', 'litellm', 'swap-node1', 'swap-node2', 'media-router', 'comfyui',
    'git-autosync', 'git-reconcile', 'audit-node1', 'audit-node2', 'hostwatch-node1', 'hostwatch-node2',
    'rank1-deadman', 'rank0-watch']) {
    const data = await apiGet(page, `/api/logs/${id}?lines=50`);
    expect(data.error, id).toBe('');
    expect(data.count, id).toBeGreaterThan(0);
    expect(data.lines.join('\n'), id).not.toMatch(/sk-[A-Za-z0-9]{20,}/);
  }
  await gotoPage(page, 'logs', 'Logs');
  await page.locator('.stream-list button', { hasText: 'hostwatch (node 2)' }).click();
  await expect(page.locator('.log-view')).toContainText('check=');
});

test('settings shows versions, sync units and runs the read-only kernel verifier', async ({ page }) => {
  test.setTimeout(10 * 60_000);
  await login(page, livePassword());
  await gotoPage(page, 'settings', 'Settings / System');
  const body = page.locator('#page-settings');
  await expect(body).toContainText('6.17.0-1032-nvidia');
  await body.getByRole('button', { name: 'Run kernel-lock verifier (both nodes)' }).click();
  await expect(page.locator('.toast').last()).toContainText('succeeded', { timeout: 8 * 60_000 });
  const job = await (await page.evaluate(async () => (await fetch('/api/actions')).json())).jobs[0];
  const full = await apiGet(page, `/api/actions/jobs/${job.id}`);
  console.log('kernel verifier:', JSON.stringify(full.result));
  expect(full.result['gx10-01'].summary).toContain('13 passed, 0 warnings, 0 failed');
  expect(full.result['gx10-02'].summary).toContain('13 passed, 0 warnings, 0 failed');
});

test('settings: integrity audit and node-2 reconcile run from the UI', async ({ page }) => {
  test.setTimeout(10 * 60_000);
  await login(page, livePassword());
  const run = async (name, confirm) => page.evaluate(async ({ n, c }) => {
    const s = await (await fetch('/api/session')).json();
    const r = await fetch(`/api/actions/${n}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': s.csrf },
      body: JSON.stringify(c === undefined ? {} : { confirm: c }),
    });
    return r.json();
  }, { n: name, c: confirm });
  const wait = async (id) => {
    for (;;) {
      const j = await apiGet(page, `/api/actions/jobs/${id}`);
      if (j.state !== 'running') return j;
      await page.waitForTimeout(3000);
    }
  };
  const audit = await wait((await run('system.integrity_audit')).id);
  console.log('integrity audit:', JSON.stringify(audit.result));
  expect(audit.state, audit.output.slice(-20).join('\n')).toBe('succeeded');
  expect(audit.result['gx10-01'].summary).toMatch(/FAIL=0/);
  expect(audit.result['gx10-02'].summary).toMatch(/FAIL=0/);
  const rec = await wait((await run('system.reconcile_node2', true)).id);
  console.log('reconcile:', JSON.stringify(rec.result));
  expect(rec.state).toBe('succeeded');
  expect(rec.result.node2_head).toMatch(/^[0-9a-f]{40}$/);
});
