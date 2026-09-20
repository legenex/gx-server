// Real-browser Open WebUI interactive user_input / ask_user acceptance.
// Credentials via env (never logged). Driven by owui_user_input_acceptance.py.
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';

const require = createRequire(new URL('../control-ui/package.json', import.meta.url));
const { chromium } = require('playwright');

// Prefer loopback: Cloudflare on chat.legenex.co blocks some automated clients (1010).
const BASE = process.env.GX_OWUI_URL || 'http://127.0.0.1:3000';
const MODEL = process.env.GX_OWUI_MODEL || 'gx-fast';
const OUT = process.env.GX_OWUI_OUT || '/tmp/owui-user-input.json';
const SHOT = process.env.GX_OWUI_SHOT || '/tmp/owui-user-input.png';

// Force the model to call ask_user before answering.
const PROMPT = [
  'You MUST use the ask_user tool before giving any final answer.',
  'Call ask_user with exactly one question:',
  'id=color_pref, header=Color, question="Which color should I use in the final reply?"',
  'options: [{label:"Blue", description:"Reply using the word Blue"},',
  '{label:"Green", description:"Reply using the word Green"}].',
  'After the user answers, reply with exactly: USER_INPUT_OK <chosen-label>',
  'Do not answer without calling ask_user first.',
].join(' ');

const result = {
  steps: [],
  model: MODEL,
  url: null,
  pending_seen: false,
  answered: false,
  final_text: '',
  error: null,
};

function note(step, extra = {}) {
  result.steps.push({ at: new Date().toISOString(), step, ...extra });
}

async function bodyText(page) {
  return page.locator('body').innerText();
}

const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  await page.goto(`${BASE}/auth`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[type="email"]').first().fill(process.env.GX_OWUI_EMAIL);
  await page.locator('input[type="password"]').first().fill(process.env.GX_OWUI_PASSWORD);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL((u) => !u.pathname.startsWith('/auth'), { timeout: 60_000 });
  note('signed_in');
  await page.keyboard.press('Escape').catch(() => {});

  await page.goto(`${BASE}/?models=${encodeURIComponent(MODEL)}`, { waitUntil: 'domcontentloaded' });
  note('chat_opened', { model: MODEL });

  // Prefer the chat composer textarea.
  const composer = page.locator('textarea').last();
  await expectVisible(composer, 30_000);
  await composer.fill(PROMPT);
  await composer.press('Enter');
  note('prompt_sent');

  // Wait for ask_user UI: option buttons or a user-input dialog.
  const deadline = Date.now() + 240_000;
  let clicked = false;
  while (Date.now() < deadline && !clicked) {
    const text = await bodyText(page);
    if (/ask_user|Which color should I use|Color|Blue|user input|clarif/i.test(text)) {
      result.pending_seen = true;
      note('pending_ui_seen', { snippet: text.slice(-500) });
    }
    // Click Blue option if present
    const blue = page.getByRole('button', { name: /^Blue$/i }).first()
      .or(page.locator('button:has-text("Blue")').first());
    if (await blue.isVisible().catch(() => false)) {
      await blue.click();
      note('clicked_blue_option');
      // Confirm / submit if needed
      const submit = page.getByRole('button', { name: /submit|confirm|send|continue|ok/i }).first();
      if (await submit.isVisible().catch(() => false)) {
        await submit.click();
        note('clicked_submit');
      }
      clicked = true;
      result.answered = true;
      break;
    }
    // Radio / option cards
    const opt = page.locator('[data-option], .option, label:has-text("Blue")').first();
    if (await opt.isVisible().catch(() => false)) {
      await opt.click();
      note('clicked_option_card');
      const submit = page.getByRole('button', { name: /submit|confirm|send|continue|ok/i }).first();
      if (await submit.isVisible().catch(() => false)) await submit.click();
      clicked = true;
      result.answered = true;
      break;
    }
    await page.waitForTimeout(1500);
  }
  if (!clicked) {
    result.error = 'ask_user UI never became clickable';
    note('pending_timeout', { body: (await bodyText(page)).slice(-1500) });
  }

  // Wait for resumed completion containing USER_INPUT_OK Blue
  const doneBy = Date.now() + 240_000;
  let last = '';
  let stable = 0;
  while (Date.now() < doneBy) {
    await page.waitForTimeout(2000);
    const text = await bodyText(page);
    const tail = text.slice(-2000);
    if (/USER_INPUT_OK\s+Blue/i.test(tail)) {
      result.final_text = tail;
      note('final_ok');
      break;
    }
    if (tail && tail === last) {
      if (++stable >= 4 && /USER_INPUT_OK|Blue/i.test(tail)) {
        result.final_text = tail;
        note('final_stable');
        break;
      }
    } else stable = 0;
    last = tail;
  }
  if (!result.final_text) result.final_text = last;
  result.url = page.url();
  await page.screenshot({ path: SHOT, fullPage: false });
  note('screenshot', { path: SHOT });
} catch (err) {
  result.error = String(err && err.stack ? err.stack : err).slice(0, 1200);
  note('crash', { error: result.error });
} finally {
  await browser.close();
}

writeFileSync(OUT, JSON.stringify(result, null, 2));
console.log(JSON.stringify(result));

async function expectVisible(locator, timeout) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await locator.isVisible().catch(() => false)) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('composer not visible');
}
