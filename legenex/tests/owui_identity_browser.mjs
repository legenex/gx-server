// Real-browser check of production Open WebUI (https://chat.legenex.co), D-038.
// Driven by owui_identity_browser.py, which creates and removes the disposable
// account; credentials arrive in the environment and are never printed.
//   GX_OWUI_EMAIL, GX_OWUI_PASSWORD, GX_OWUI_SHOT (screenshot path)
import { createRequire } from 'node:module';

const require = createRequire(new URL('../control-ui/package.json', import.meta.url));
const { chromium } = require('playwright');
const BASE = process.env.GX_OWUI_URL || 'https://chat.legenex.co';
const QUESTION = 'What model are you? Give the alias and your full underlying model name.';

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const result = { steps: [] };
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/auth`, { waitUntil: 'domcontentloaded' });
  await page.locator('input[type="email"]').first().fill(process.env.GX_OWUI_EMAIL);
  await page.locator('input[type="password"]').first().fill(process.env.GX_OWUI_PASSWORD);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL((u) => !u.pathname.startsWith('/auth'), { timeout: 60_000 });
  result.steps.push('signed in through the public login page');
  await page.keyboard.press('Escape'); // a first-login "what's new" dialog, if any

  await page.goto(`${BASE}/?models=gx-mini&q=${encodeURIComponent(QUESTION)}`, { waitUntil: 'domcontentloaded' });
  result.steps.push('opened a new chat with gx-mini and the question');
  // the answer is streamed into the page; wait until it names the underlying model and stops changing
  let last = '';
  let stable = 0;
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(2000);
    const text = await page.locator('body').innerText();
    const idx = text.lastIndexOf(QUESTION);
    const tail = idx >= 0 ? text.slice(idx + QUESTION.length) : '';
    if (tail && tail === last && /HauhauCS/i.test(tail)) {
      if (++stable >= 2) break;
    } else {
      stable = 0;
    }
    last = tail;
  }
  result.answer_region = last.slice(0, 1200);
  result.url = page.url();
  result.model_button = (await page.locator('button:has-text("gx-mini")').first().innerText().catch(() => '')).slice(0, 60);
  await page.screenshot({ path: process.env.GX_OWUI_SHOT, fullPage: false });
  result.steps.push('answer captured');
} finally {
  await browser.close();
}
console.log(JSON.stringify(result));
