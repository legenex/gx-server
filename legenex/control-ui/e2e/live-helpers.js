// Shared helpers for the LIVE suites (real cluster, deployed UI on gx10-01).
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { expect } from '@playwright/test';

export function livePassword() {
  if (process.env.GX_UI_PASSWORD) return process.env.GX_UI_PASSWORD;
  const file = process.env.GX_UI_PASSWORD_FILE || '/srv/projects/gx-cluster/secrets/control-ui/initial-admin-password';
  return readFileSync(file, 'utf8').trim();
}

// A PNG with a red circle, a blue square and the digit 7, for vision checks.
export function makeVisionImage(path) {
  execFileSync('python3', ['-c', `
from PIL import Image, ImageDraw, ImageFont
im = Image.new('RGB', (360, 240), (255, 255, 255))
d = ImageDraw.Draw(im)
d.ellipse((20, 60, 130, 170), fill=(220, 20, 20))
d.rectangle((150, 60, 250, 160), fill=(20, 40, 220))
try:
    f = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 90)
except Exception:
    f = ImageFont.load_default()
d.text((270, 60), '7', fill=(0, 0, 0), font=f)
im.save(${JSON.stringify(path)})
`]);
  return path;
}

export async function playgroundChat(page, { model, prompt, maxTokens = 256, temperature = 0, tools = false, stream = false,
  image = null, confirmTakeover = false, timeout = 900_000 }) {
  await page.goto('/#/playground/chat');
  await expect(page.locator('#chat-form')).toBeVisible();
  await page.selectOption('#pg-model', model);
  await page.fill('#pg-prompt', prompt);
  await page.fill('#pg-max', String(maxTokens));
  await page.fill('#pg-temp', String(temperature));
  if (tools) await page.check('#pg-tools');
  if (stream) await page.check('#pg-stream');
  if (image) await page.setInputFiles('#pg-image', image);
  if (confirmTakeover) await page.check('#pg-max-confirm');
  const t0 = Date.now();
  await page.click('#pg-send');
  const out = page.locator('#chat-output');
  await expect(out.locator('h3', { hasText: 'Response' })).toBeVisible({ timeout });
  const answer = (await out.locator('.answer').count()) ? await out.locator('.answer').innerText() : '';
  const text = await out.innerText();
  return { answer, text, seconds: (Date.now() - t0) / 1000 };
}

export async function modelOperation(page, alias, op, phrase) {
  await page.goto(`/#/models/${alias}`);
  const card = page.locator(`#model-${alias}`);
  await expect(card).toBeVisible();
  const label = { load: 'Load', unload: 'Unload', restart: 'Restart', force_release: 'Force release' }[op];
  const btn = card.getByRole('button', { name: label, exact: true });
  await expect(btn).toBeEnabled({ timeout: 60_000 });
  await btn.click();
  const dialog = page.locator('#confirm-dialog');
  if (await dialog.isVisible()) {
    if (phrase) await page.fill('#confirm-phrase', phrase);
    await page.click('#confirm-ok');
  }
}

export async function apiGet(page, path) {
  return page.evaluate(async (p) => (await fetch(p, { credentials: 'same-origin' })).json(), path);
}

export async function waitModelState(page, alias, states, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    const data = await apiGet(page, '/api/models');
    last = data.models.find((m) => m.alias === alias);
    const busy = data.running_jobs.length > 0;
    if (states.includes(last.state) && !busy) return last;
    await page.waitForTimeout(5000);
  }
  throw new Error(`${alias} did not reach ${states} (last ${last && last.state}: ${last && last.state_detail})`);
}

export async function lastJob(page) {
  const data = await apiGet(page, '/api/actions');
  return data.jobs[0];
}

export async function waitJobDone(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const j = await lastJob(page);
    if (j && j.state !== 'running') return apiGet(page, `/api/actions/jobs/${j.id}`);
    if (Date.now() > deadline) throw new Error('operation did not finish in time');
    await page.waitForTimeout(5000);
  }
}
