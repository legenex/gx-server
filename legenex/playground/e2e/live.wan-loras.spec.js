// LIVE Wan 2.2 LoRA acceptance (Build V3 WAN) through the deployed GX-Playground:
// REAL text-to-video generations on gx10-02, one on the standard graph and one
// with the installed official LightX2V 4-step distillation LoRA pair applied
// through the new LoRA system (a distillation LoRA stacked on the built-in
// base LoRAs, at a deliberately low strength).
// Run deliberately on gx10-01:
//   GX_EVIDENCE_DIR=/srv/logs/acceptance/build-v3/wan/<run> npx playwright test --project=live e2e/live.wan-loras.spec.js
// The created videos are kept (they are the evidence) and titled "WAN acceptance".
import { expect, test } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { axeCheck, gotoPage, watchPage } from './helpers.js';

const PASSWORD_FILE = '/srv/projects/gx-cluster/secrets/control-ui/acceptance-password';
const EVIDENCE = process.env.GX_EVIDENCE_DIR;
const PROMPT = 'Photorealistic cinematic footage of two adult actors walking through a modern hotel lobby, realistic skin '
  + 'texture, natural body movement, soft cinematic lighting, handheld camera motion, shallow depth of field.';
const PAIR_HIGH = 'wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors';
const PAIR_LOW = 'wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors';
const STRENGTH = '0.3';
const SEED = 20260917;
const ALLOW = [/Failed to load resource.*(401|404|503)/];
const report = { steps: [], runs: [] };

test.describe.configure({ mode: 'serial' });
test.use({ screenshot: 'off', video: 'off', trace: 'off' });

function note(step, data = {}) {
  report.steps.push({ at: new Date().toISOString(), step, ...data });
  console.log(`[wan-live] ${step} ${JSON.stringify(data).slice(0, 500)}`);
  if (EVIDENCE) writeFileSync(join(EVIDENCE, 'wan-live.json'), JSON.stringify(report, null, 1));
}

async function signIn(page) {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', readFileSync(PASSWORD_FILE, 'utf8').trim());
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
}

const getJson = async (page, url) => (await page.request.get(url)).json();

async function waitJob(page, id, label) {
  let last = '';
  for (;;) {
    const job = await getJson(page, `/api/media/jobs/${id}`);
    const key = `${job.phase}|${job.detail}|${job.waiting && job.waiting.reason}`;
    if (key !== last) {
      note(`${label}-phase`, { id, phase: job.phase, detail: job.detail, waiting: job.waiting, cold: job.cold_start });
      last = key;
    }
    if (['ready', 'failed', 'cancelled'].includes(job.phase)) return job;
    await page.waitForTimeout(5000);
  }
}

async function playVideo(page) {
  const video = page.locator('#viewer video');
  await expect(video).toBeVisible({ timeout: 60_000 });
  const played = await video.evaluate(async (v) => {
    v.muted = true;
    if (v.readyState < 2) await new Promise((r) => { v.addEventListener('loadeddata', r, { once: true }); setTimeout(r, 20000); });
    await v.play();
    const t0 = v.currentTime;
    await new Promise((r) => setTimeout(r, 1500));
    const out = { readyState: v.readyState, duration: v.duration, advanced: v.currentTime - t0, width: v.videoWidth, height: v.videoHeight };
    v.pause();
    return out;
  });
  expect(played.advanced).toBeGreaterThan(0.2);
  expect(played.width).toBeGreaterThan(0);
  return played;
}

async function generate(page, label, withLora) {
  await gotoPage(page, 'video');
  await page.getByRole('tab', { name: 'Text to Video' }).click();
  // start from a clean stack
  for (;;) {
    const remove = page.locator('.wan-stack [data-action="remove"]').first();
    if (!(await remove.count())) break;
    await remove.click();
  }
  if (withLora) {
    await page.locator('[data-action="add-lora"]').click();
    const drawer = page.getByRole('dialog', { name: 'Add a LoRA' });
    await expect(drawer.locator('.wan-entry').first()).toBeVisible({ timeout: 60_000 });
    await drawer.getByRole('searchbox', { name: 'Search LoRAs' }).fill('lightx2v_4steps');
    const card = drawer.locator('.wan-entry').filter({ hasText: PAIR_HIGH });
    await expect(card).toHaveCount(1);
    await expect(card.locator('.badge', { hasText: 'Paired' })).toBeVisible();
    await expect(card.locator('.badge', { hasText: /^Compatible$/ })).toBeVisible();
    await expect(card).toContainText(PAIR_LOW);
    await card.getByRole('button', { name: 'Show details for' }).click();
    const details = await card.locator('.wan-entry-details').innerText();
    note('library-entry', { details: details.slice(0, 1500) });
    await axeCheck(page, 'live LoRA library');
    await card.getByRole('button', { name: /^Add / }).click();
    await page.keyboard.press('Escape');
    const name = await page.locator('.wan-stack .wan-name').first().innerText();
    await page.getByRole('slider', { name: `${name}: high-noise strength` }).fill(STRENGTH);
    await page.getByRole('slider', { name: `${name}: low-noise strength` }).fill(STRENGTH);
    // preview the exact graph before running it
    await page.locator('#video-advanced > summary').click();
    await page.fill('#video-prompt', PROMPT);
    await page.locator('[data-action="preview-workflow"]').click();
    const adv = page.getByRole('dialog', { name: 'Advanced view' });
    await expect(adv.locator('.wan-table')).toContainText(PAIR_HIGH);
    note('preview', { table: (await adv.locator('.wan-table').innerText()).slice(0, 1200) });
    await page.keyboard.press('Escape');
  }
  await page.fill('#video-prompt', PROMPT);
  await page.getByRole('spinbutton', { name: 'Seed' }).fill(String(SEED));
  await page.getByLabel('Title', { exact: true }).fill(`WAN acceptance ${label}`);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/video/generate') && r.method() === 'POST'),
    page.click('#generate-btn')]);
  const job = await (await req.response()).json();
  note(`${label}-submitted`, { job: job.id, loras: req.postDataJSON().loras, router: job.params.wan.router });
  const done = await waitJob(page, job.id, label);
  expect(done.phase, JSON.stringify(done)).toBe('ready');
  await expect(page.locator(`#ws-jobs [data-job="${job.id}"] .phase-badge`)).toHaveAttribute('data-phase', 'COMPLETE', { timeout: 120_000 });
  const played = await playVideo(page);
  const gen = await getJson(page, `/api/video/generations/${job.id}`);
  const asset = await getJson(page, `/api/media/assets/${gen.asset_id}`);
  const wf = await page.request.get(`/api/video/generations/${job.id}/workflow`);
  expect(wf.status()).toBe(200);
  const wfText = await wf.text();
  expect(wfText).not.toMatch(/\/srv\/|192\.168\.|gx10-0|Bearer|\bsk-[A-Za-z0-9]{8}/);
  if (EVIDENCE) writeFileSync(join(EVIDENCE, `workflow-${label}.json`), wfText);
  const run = {
    label, job: job.id, played, status: gen.status, seed: gen.seed, frames: gen.frames, fps: gen.fps, size: gen.size,
    duration_seconds: gen.duration_seconds, elapsed_seconds: done.elapsed_seconds, cold_start: done.cold_start,
    workflow_version: gen.workflow_version, comfy_prompt_id: gen.comfy_prompt_id, chains: gen.chains,
    high_model: gen.high_model, low_model: gen.low_model, loras: gen.loras, asset_id: gen.asset_id,
    output_path: gen.output_path, asset: { file_size: asset.file_size, sha256: asset.sha256, width: asset.width,
      height: asset.height, frame_count: asset.frame_count, distinct_frames: asset.distinct_frames, fps: asset.fps,
      duration: asset.duration, wan: asset.settings.wan },
  };
  report.runs.push(run);
  note(`${label}-done`, run);
  expect(gen.comfy_prompt_id).toBeTruthy();
  expect(gen.high_model).toBe('wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors');
  expect(gen.low_model).toBe('wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors');
  expect(asset.frame_count).toBe(gen.frames);
  const user = (gen.chains.high || []).filter((c) => !c.base);
  const userLow = (gen.chains.low || []).filter((c) => !c.base);
  if (withLora) {
    expect(user).toEqual([{ node: '1000', lora_name: PAIR_HIGH, strength: 0.3, base: false }]);
    expect(userLow).toEqual([{ node: '2000', lora_name: PAIR_LOW, strength: 0.3, base: false }]);
    expect(asset.settings.wan.loras[0]).toMatchObject({ high_file: PAIR_HIGH, low_file: PAIR_LOW, strength_high: 0.3, strength_low: 0.3 });
  } else {
    expect(user).toEqual([]);
    expect(userLow).toEqual([]);
  }
  // history lists it; its details open with the workflow
  const hist = page.locator('.wan-history .wan-gen', { has: page.locator(`text=WAN acceptance ${label}`) }).first();
  await expect(hist).toBeVisible({ timeout: 30_000 });
  await hist.getByRole('button', { name: 'Details, workflow and metadata' }).click();
  const dlg = page.getByRole('dialog', { name: 'Video generation' });
  await expect(dlg.locator('video')).toBeVisible();
  await dlg.locator('summary', { hasText: 'Advanced view' }).click();
  await expect(dlg.locator('.wan-advanced')).toContainText(gen.comfy_prompt_id);
  await page.keyboard.press('Escape');
  return run;
}

test('Wan 2.2 text-to-video: standard graph, then the LightX2V pair through the LoRA system', async ({ page }) => {
  test.setTimeout(90 * 60_000);
  const problems = watchPage(page, { allow: ALLOW });
  await signIn(page);
  await gotoPage(page, 'video');
  const lib = await (await page.request.get('/api/video/loras?refresh=1')).json();
  note('catalogue', { roots: lib.roots, scanned_at: lib.scanned_at, comfy: lib.comfy,
    entries: lib.entries.map((e) => ({ name: e.display_name, state: e.pair_state, compat: e.compatibility, high: e.high_file, low: e.low_file, usable: e.usable })) });
  const base = await generate(page, 'no-lora', false);
  const lora = await generate(page, 'lightx2v-pair', true);
  note('compare', { same_seed: base.seed === lora.seed, sha_differs: base.asset.sha256 !== lora.asset.sha256 });
  expect(base.asset.sha256).not.toBe(lora.asset.sha256);
  expect(problems).toEqual([]);
});
