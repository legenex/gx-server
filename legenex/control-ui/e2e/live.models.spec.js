// LIVE: real model calls made through the deployed control UI.
// Order matters for node-2 memory: gx-reason is unloaded (through the UI)
// before image/video generation, exactly as the media interlock requires.
import { expect, test } from '@playwright/test';
import { login, watchPage } from './helpers.js';
import {
  apiGet, livePassword, makeVisionImage, modelOperation, playgroundChat, waitJobDone, waitModelState,
} from './live-helpers.js';

test.describe.configure({ mode: 'serial' });
const IMG = '/tmp/gx-ui-live-vision.png';
const results = {};

test.beforeEach(async ({ page }) => {
  await login(page, livePassword());
});

test.afterAll(() => {
  console.log('LIVE MODEL RESULTS', JSON.stringify(results, null, 1));
});

test('gx-mini: text and vision through the UI', async ({ page }) => {
  const problems = watchPage(page);
  const r = await playgroundChat(page, { model: 'gx-mini', prompt: 'What is 17 multiplied by 23? Reply with only the number.', maxTokens: 20 });
  expect(r.answer).toContain('391');
  results['gx-mini'] = { answer: r.answer, seconds: r.seconds };
  makeVisionImage(IMG);
  const v = await playgroundChat(page, {
    model: 'gx-mini', image: IMG, maxTokens: 120,
    prompt: 'List the shapes with their colours, and the digit, in this image. Be brief.',
  });
  const text = v.answer.toLowerCase();
  expect(text).toMatch(/red/);
  expect(text).toMatch(/circle/);
  expect(text).toMatch(/blue/);
  expect(text).toMatch(/square|rectangle/);
  expect(text).toMatch(/7|seven/);
  results['gx-mini vision'] = { answer: v.answer, seconds: v.seconds };
  const stream = await playgroundChat(page, { model: 'gx-mini', stream: true, prompt: 'Count from one to five in words.', maxTokens: 40 });
  expect(stream.answer.toLowerCase()).toContain('five');
  results['gx-mini stream'] = { answer: stream.answer };
  expect(problems).toEqual([]);
});

test('gx-fast: tool call through the UI, then UNLOAD and LOAD controls', async ({ page }) => {
  test.setTimeout(40 * 60_000);
  const r = await playgroundChat(page, {
    model: 'gx-fast', tools: true, maxTokens: 300, timeout: 20 * 60_000,
    prompt: 'What is the weather in Cape Town right now? Use the available tool.',
  });
  expect(r.text).toContain('Tool calls');
  expect(r.text).toContain('get_weather');
  expect(r.text).toMatch(/Cape Town/);
  results['gx-fast tools'] = { seconds: r.seconds };
  const plain = await playgroundChat(page, { model: 'gx-fast', prompt: 'What is the capital of Japan? One word.', maxTokens: 20 });
  expect(plain.answer).toMatch(/Tokyo/i);
  results['gx-fast'] = { answer: plain.answer, seconds: plain.seconds };

  await modelOperation(page, 'gx-fast', 'unload');
  const unload = await waitJobDone(page, 5 * 60_000);
  expect(unload.state, unload.output.join('\n')).toBe('succeeded');
  await waitModelState(page, 'gx-fast', ['unloaded'], 3 * 60_000);

  await modelOperation(page, 'gx-fast', 'load');
  const load = await waitJobDone(page, 30 * 60_000);
  expect(load.state, load.output.join('\n')).toBe('succeeded');
  results['gx-fast UI load'] = { seconds: load.result.load_seconds };
  await waitModelState(page, 'gx-fast', ['loaded'], 2 * 60_000);
});

test('gx-reason: real reasoning through the UI', async ({ page }) => {
  test.setTimeout(40 * 60_000);
  const r = await playgroundChat(page, {
    model: 'gx-reason', maxTokens: 4000, timeout: 30 * 60_000,
    prompt: 'A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost? Give the final answer in cents.',
  });
  expect(r.answer).toMatch(/\b5\b|0\.05|five cents/i);
  expect(r.answer).not.toMatch(/\b10 cents\b/);
  results['gx-reason'] = { answer: r.answer.slice(0, 200), seconds: r.seconds, reasoning: r.text.includes('Reasoning content') };
});

test('gx-auto: routes a simple and a hard prompt through the UI', async ({ page }) => {
  test.setTimeout(20 * 60_000);
  const simple = await playgroundChat(page, { model: 'gx-auto', prompt: 'Say hello in French. One word.', maxTokens: 20 });
  expect(simple.answer).toMatch(/bonjour|salut/i);
  const used = (simple.text.match(/Model used\s*\n?\s*(\S+)/) || [])[1];
  results['gx-auto simple'] = { answer: simple.answer, model_used: used, seconds: simple.seconds };
  const hard = await playgroundChat(page, {
    model: 'gx-auto', maxTokens: 3000, timeout: 15 * 60_000,
    prompt: 'Prove step by step that the square root of 2 is irrational, then state the conclusion in one sentence.',
  });
  expect(hard.answer.toLowerCase()).toContain('irrational');
  results['gx-auto hard'] = { model_used: (hard.text.match(/Model used\s*\n?\s*(\S+)/) || [])[1], seconds: hard.seconds };
});

test('gx-reason UNLOAD through the UI frees node 2 before media', async ({ page }) => {
  test.setTimeout(10 * 60_000);
  const before = await apiGet(page, '/api/models');
  const reason = before.models.find((m) => m.alias === 'gx-reason');
  if (reason.state !== 'unloaded') {
    await modelOperation(page, 'gx-reason', 'unload');
    const job = await waitJobDone(page, 5 * 60_000);
    expect(job.state, job.output.join('\n')).toBe('succeeded');
  }
  await waitModelState(page, 'gx-reason', ['unloaded'], 3 * 60_000);
  results['gx-reason UI unload'] = 'ok';
});

test('gx-image: real generation through the UI', async ({ page }) => {
  test.setTimeout(20 * 60_000);
  await page.goto('/#/playground/image');
  await page.fill('#img-prompt', 'a single red apple on a wooden table, soft daylight, photograph');
  await page.selectOption('#img-size', '1024x1024');
  const t0 = Date.now();
  await page.click('#img-send');
  const img = page.locator('#gen-image-0');
  await expect(img).toBeVisible({ timeout: 15 * 60_000 });
  const dims = await img.evaluate((el) => [el.naturalWidth, el.naturalHeight]);
  expect(dims).toEqual([1024, 1024]);
  // a real picture has many distinct colours
  const colours = await img.evaluate((el) => {
    const c = document.createElement('canvas');
    c.width = 64; c.height = 64;
    const ctx = c.getContext('2d');
    ctx.drawImage(el, 0, 0, 64, 64);
    const d = ctx.getImageData(0, 0, 64, 64).data;
    const set = new Set();
    for (let i = 0; i < d.length; i += 4) set.add(`${d[i] >> 3},${d[i + 1] >> 3},${d[i + 2] >> 3}`);
    return set.size;
  });
  expect(colours).toBeGreaterThan(100);
  results['gx-image'] = { dims, distinct_colours_64px: colours, seconds: (Date.now() - t0) / 1000 };
});

test('gx-video: real generation through the UI with distinct frames', async ({ page }) => {
  test.setTimeout(30 * 60_000);
  await page.goto('/#/playground/video');
  await page.fill('#vid-prompt', 'a paper boat drifting down a small stream, gentle camera pan, afternoon light');
  await page.fill('#vid-seconds', '2');
  const t0 = Date.now();
  await page.click('#vid-send');
  await expect(page.locator('#gen-video')).toBeVisible({ timeout: 25 * 60_000 });
  const check = page.locator('#frame-check');
  await expect(check).toHaveAttribute('data-distinct', /\d+/, { timeout: 120_000 });
  const distinct = Number(await check.getAttribute('data-distinct'));
  const summary = await check.innerText();
  expect(distinct).toBeGreaterThanOrEqual(3);
  results['gx-video'] = { frame_check: summary, seconds: (Date.now() - t0) / 1000 };
});

test('media UNLOAD through the UI and the recorded results are visible', async ({ page }) => {
  test.setTimeout(10 * 60_000);
  await modelOperation(page, 'gx-image', 'unload');
  const job = await waitJobDone(page, 5 * 60_000);
  expect(job.state, job.output.join('\n')).toBe('succeeded');
  await page.goto('/#/models');
  for (const alias of ['gx-mini', 'gx-fast', 'gx-reason', 'gx-auto', 'gx-image', 'gx-video']) {
    await expect(page.locator(`#model-${alias}`)).toContainText('Last real inference');
  }
  const data = await apiGet(page, '/api/models');
  for (const alias of ['gx-mini', 'gx-fast', 'gx-reason', 'gx-auto', 'gx-image', 'gx-video']) {
    const m = data.models.find((x) => x.alias === alias);
    expect(m.results.inference && m.results.inference.ok, `${alias} last inference`).toBe(true);
  }
});
