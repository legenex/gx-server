// LIVE: gx-max LOAD through the control UI (sanctioned orchestrator acquire),
// then real inference through the UI playground. Leaves gx-max READY for the
// CLI inference check; live.gxmax-2-release.spec.js releases it.
import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { login, watchPage } from './helpers.js';
import { apiGet, livePassword, modelOperation, playgroundChat } from './live-helpers.js';

test.describe.configure({ mode: 'serial' });
const OUT = process.env.GX_GXMAX_EVIDENCE || '/tmp/gx-ui-gxmax-evidence.json';
const evidence = {};

function rdmaByDevice(models) {
  const gx = models.models.find((m) => m.alias === 'gx-max');
  const out = {};
  for (const node of ['node1', 'node2']) {
    for (const r of gx.live.rdma[node] || []) {
      if ((r.state || '').includes('ACTIVE')) out[`${node}:${r.device}`] = (r.xmit_bytes || 0) + (r.rcv_bytes || 0);
    }
  }
  return out;
}

test.afterAll(() => {
  writeFileSync(OUT, JSON.stringify(evidence, null, 1));
  console.log('GX-MAX UI EVIDENCE', JSON.stringify(evidence, null, 1));
});

test('gx-max LOAD through the UI follows the sanctioned lifecycle', async ({ page }) => {
  test.setTimeout(40 * 60_000);
  const problems = watchPage(page);
  await login(page, livePassword());
  const before = await apiGet(page, '/api/models');
  const gx0 = before.models.find((m) => m.alias === 'gx-max');
  expect(gx0.state, gx0.state_detail).toBe('unloaded');
  evidence.rdma_before = rdmaByDevice(before);

  // Direct playground request without confirmation is refused (no silent takeover).
  const refused = await page.evaluate(async () => {
    const s = await (await fetch('/api/session')).json();
    const r = await fetch('/api/playground/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': s.csrf },
      body: JSON.stringify({ model: 'gx-max', prompt: 'hi' }),
    });
    return r.status;
  });
  expect(refused).toBe(409);

  const t0 = Date.now();
  await modelOperation(page, 'gx-max', 'load', 'gx-max');
  await expect(page.locator('.toast').last()).toContainText('Started: Load gx-max', { timeout: 30_000 });

  // Watch the lifecycle on the Jobs page data while it loads.
  const phases = [];
  let minAvail = { node1: 1e9, node2: 1e9 };
  let maxSwap = { node1: 0, node2: 0 };
  let job;
  for (;;) {
    const jobs = await apiGet(page, '/api/jobs');
    const ph = jobs.gxmax.phase;
    if (ph && phases[phases.length - 1] !== ph) phases.push(ph);
    const ov = await apiGet(page, '/api/overview');
    for (const n of ov.nodes) {
      if (!n.reachable) continue;
      minAvail[n.key] = Math.min(minAvail[n.key], n.mem_available_gib);
      maxSwap[n.key] = Math.max(maxSwap[n.key], n.swap_used_gib);
    }
    job = jobs.ui_jobs.find((j) => j.action === 'model.gx-max.load');
    if (job && job.state !== 'running') break;
    if (Date.now() - t0 > 35 * 60_000) throw new Error('gx-max load did not finish in 35 minutes');
    await page.waitForTimeout(10_000);
  }
  const full = await apiGet(page, `/api/actions/jobs/${job.id}`);
  evidence.load_job = { state: full.state, result: full.result, elapsed_seconds: full.elapsed_seconds };
  evidence.phases_seen_by_ui = phases;
  evidence.ui_sampled_min_mem_available_gib = minAvail;
  evidence.ui_sampled_max_swap_used_gib = maxSwap;
  expect(full.state, full.output.join('\n')).toBe('succeeded');
  // UI polling (10 s) can miss short phases; the orchestrator history below is authoritative.
  expect(phases.length).toBeGreaterThan(1);

  const jobs = await apiGet(page, '/api/jobs');
  const hist = jobs.gxmax_history[0];
  evidence.orchestrator_job = { kind: hist.kind, outcome: hist.outcome, startup_seconds: hist.startup_seconds,
    phases: hist.phases.map((p) => p.phase) };
  expect(hist.outcome).toBe('ready');
  const ph = hist.phases.map((p) => p.phase);
  expect(ph.indexOf('loading_rank1')).toBeLessThan(ph.indexOf('loading_rank0'));

  const after = await apiGet(page, '/api/models');
  const gx = after.models.find((m) => m.alias === 'gx-max');
  expect(gx.state).toBe('loaded');
  expect(gx.live.rank0.state).toBe('running');
  expect(gx.live.rank1.state).toBe('running');
  expect(gx.live.sglang_health.ok).toBe(true);
  expect(gx.live.rank0_watcher.alive).toBe(true);
  expect(gx.live.rank1_deadman.alive).toBe(true);
  evidence.live_engine = {
    served_models: (gx.live.sglang_models || []).map((m) => m.id),
    server_info: gx.live.sglang_info,
    rank0: gx.live.rank0.status, rank1: gx.live.rank1.status,
    ledger: gx.live.ledger,
  };
  for (const alias of ['gx-mini', 'gx-fast', 'gx-reason', 'gx-image', 'gx-video']) {
    const m = after.models.find((x) => x.alias === alias);
    expect(m.state, `${alias} must be drained`).toBe('unavailable');
  }
  // The UI shows it too.
  await page.goto('/#/models/gx-max');
  await expect(page.locator('#model-gx-max')).toContainText('healthy');
  expect(problems.filter((p) => !p.includes('409'))).toEqual([]);
});

test('gx-max real inference through the UI (factual, reasoning, coding, long, streaming)', async ({ page }) => {
  test.setTimeout(30 * 60_000);
  await login(page, livePassword());
  const rdma0 = rdmaByDevice(await apiGet(page, '/api/models'));

  const factual = await playgroundChat(page, { model: 'gx-max', prompt: 'What is the capital city of Australia? Answer in one word.', maxTokens: 64 });
  expect(factual.text).toMatch(/Canberra/);

  const reasoning = await playgroundChat(page, {
    model: 'gx-max', maxTokens: 1024,
    prompt: 'A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost? Reply with the final amount.',
  });
  expect(reasoning.text).toMatch(/0\.05|5 cents|five cents/i);

  const coding = await playgroundChat(page, {
    model: 'gx-max', maxTokens: 700,
    prompt: 'Write a Python function is_prime(n) that returns True if n is prime. Only output the code in one fenced block.',
  });
  const m = coding.answer.match(/```(?:python)?\n([\s\S]*?)```/);
  const code = m ? m[1] : coding.answer;
  const verdict = execFileSync('python3', ['-c', `
import sys
ns = {}
exec(sys.stdin.read(), ns)
f = ns["is_prime"]
assert [n for n in range(40) if f(n)] == [2,3,5,7,11,13,17,19,23,29,31,37]
print("ok")
`], { input: code }).toString().trim();
  expect(verdict).toBe('ok');

  const t0 = Date.now();
  const long = await playgroundChat(page, {
    model: 'gx-max', maxTokens: 1200,
    prompt: 'Write a detailed, multi-paragraph explanation of how tensor parallelism splits a transformer layer across two GPUs, including attention and MLP blocks.',
  });
  const usage = long.text.match(/completion (\d+)/);
  const completion = usage ? Number(usage[1]) : 0;
  const latency = long.text.match(/Latency\s*\n?\s*(\d+) ms/);
  const ms = latency ? Number(latency[1]) : Date.now() - t0;
  expect(completion).toBeGreaterThan(500);

  const stream = await playgroundChat(page, { model: 'gx-max', stream: true, prompt: 'Say hello and name one planet.', maxTokens: 64 });
  expect(stream.answer.length).toBeGreaterThan(3);

  const rdma1 = rdmaByDevice(await apiGet(page, '/api/models'));
  const deltas = Object.fromEntries(Object.keys(rdma1).map((k) => [k, Math.round((rdma1[k] - (rdma0[k] || 0)) / 2 ** 20)]));
  evidence.inference = {
    factual: factual.answer.slice(0, 80),
    reasoning_tail: reasoning.answer.slice(-120),
    coding: 'is_prime executed and correct for 0..39',
    long_completion_tokens: completion,
    long_latency_ms: ms,
    long_tok_per_s_wall: Math.round((completion / (ms / 1000)) * 10) / 10,
    stream_answer: stream.answer.slice(0, 80),
    rdma_delta_mib_during_inference: deltas,
  };
  // both rails, on both nodes, carried traffic during generation
  for (const key of ['node1:rocep1s0f0', 'node1:roceP2p1s0f0', 'node2:rocep1s0f0', 'node2:roceP2p1s0f0']) {
    expect(deltas[key], `RDMA traffic on ${key}`).toBeGreaterThan(1);
  }
  const loadDeltas = Object.fromEntries(Object.keys(rdma1).map((k) => [k, Math.round((rdma1[k] - (evidence.rdma_before?.[k] || 0)) / 2 ** 30 * 10) / 10]));
  evidence.rdma_delta_gib_since_before_load = loadDeltas;
});
