// LIVE: graceful gx-max release through the control UI, then verification
// that nothing is left behind and normal service is back.
import { writeFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { login } from './helpers.js';
import { apiGet, livePassword, modelOperation, playgroundChat, waitModelState } from './live-helpers.js';

test.describe.configure({ mode: 'serial' });
const OUT = process.env.GX_GXMAX_RELEASE_EVIDENCE || '/tmp/gx-ui-gxmax-release.json';
const evidence = {};

test.afterAll(() => {
  writeFileSync(OUT, JSON.stringify(evidence, null, 1));
  console.log('GX-MAX RELEASE EVIDENCE', JSON.stringify(evidence, null, 1));
});

test('gx-max graceful RELEASE through the UI leaves a clean cluster', async ({ page }) => {
  test.setTimeout(30 * 60_000);
  await login(page, livePassword());
  const pre = await apiGet(page, '/api/models');
  expect(pre.models.find((m) => m.alias === 'gx-max').state).toBe('loaded');

  const t0 = Date.now();
  await modelOperation(page, 'gx-max', 'unload');
  let job;
  for (;;) {
    const jobs = await apiGet(page, '/api/jobs');
    job = jobs.ui_jobs.find((j) => j.action === 'model.gx-max.unload');
    if (job && job.state !== 'running') break;
    if (Date.now() - t0 > 20 * 60_000) throw new Error('release did not finish');
    await page.waitForTimeout(5000);
  }
  const full = await apiGet(page, `/api/actions/jobs/${job.id}`);
  evidence.release_job = { state: full.state, elapsed_seconds: full.elapsed_seconds, output_tail: full.output.slice(-6) };
  expect(full.state, full.output.join('\n')).toBe('succeeded');

  const jobs = await apiGet(page, '/api/jobs');
  const hist = jobs.gxmax_history[0];
  evidence.orchestrator_release = { kind: hist.kind, outcome: hist.outcome, phases: hist.phases.map((p) => p.phase) };
  expect(hist.kind).toBe('release');
  expect(hist.outcome).toBe('released');

  // Fresh readings (the release job invalidated every cache).
  await page.waitForTimeout(10_000);
  const models = await apiGet(page, '/api/models');
  const gx = models.models.find((m) => m.alias === 'gx-max');
  expect(gx.state).toBe('unloaded');
  expect(gx.live.rank0).toBeFalsy();
  expect(gx.live.rank1).toBeFalsy();
  expect(gx.live.rank0_watcher.alive).toBe(false);
  expect(gx.live.rank1_deadman.alive).toBe(false);
  expect(gx.live.node1_lock).toBe('free');
  expect(gx.live.node2_lock).toBe('free');
  const ledger = gx.live.ledger;
  expect(Object.keys(ledger.node1 || {})).toEqual([]);
  expect(Object.keys(ledger.node2 || {})).toEqual([]);
  expect(gx.live.sglang_health.ok).toBeFalsy();
  evidence.after = { rank0: gx.live.rank0, rank1: gx.live.rank1, ledger, locks: [gx.live.node1_lock, gx.live.node2_lock] };

  // Memory returns on both nodes.
  const deadline = Date.now() + 5 * 60_000;
  let ov;
  for (;;) {
    ov = await apiGet(page, '/api/overview');
    const avail = Object.fromEntries(ov.nodes.map((n) => [n.key, n.mem_available_gib]));
    evidence.mem_available_after_gib = avail;
    evidence.swap_used_after_gib = Object.fromEntries(ov.nodes.map((n) => [n.key, n.swap_used_gib]));
    if (avail.node1 > 90 && avail.node2 > 90) break;
    if (Date.now() > deadline) throw new Error(`memory did not return: ${JSON.stringify(avail)}`);
    await page.waitForTimeout(10_000);
  }
  // Normal control planes are back.
  const byName = Object.fromEntries(ov.services.map((s) => [s.name, s]));
  for (const name of ['LiteLLM gateway', 'gx-orchestrator', 'llama-swap gx10-01', 'llama-swap gx10-02', 'media router (gx10-02)']) {
    expect(byName[name].ok, `${name} restored`).toBe(true);
  }
  evidence.services_after = ov.services.map((s) => `${s.name}: ${s.ok ? 'up' : 'down'}`);
});

test('normal workloads serve again after release', async ({ page }) => {
  test.setTimeout(15 * 60_000);
  await login(page, livePassword());
  await waitModelState(page, 'gx-image', ['ready'], 5 * 60_000);
  const r = await playgroundChat(page, { model: 'gx-mini', prompt: 'What is 12 plus 30? Reply with only the number.', maxTokens: 16 });
  expect(r.answer).toContain('42');
  const auto = await playgroundChat(page, { model: 'gx-auto', prompt: 'Name the largest planet in our solar system. One word.', maxTokens: 16 });
  expect(auto.answer).toMatch(/Jupiter/i);
  evidence.after_release_inference = { 'gx-mini': r.answer, 'gx-auto': auto.answer };
  const models = await apiGet(page, '/api/models');
  evidence.states_after = Object.fromEntries(models.models.map((m) => [m.alias, m.state]));
});
