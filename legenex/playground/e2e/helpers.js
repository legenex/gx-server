// Shared helpers for the GX-Playground offline browser tests.
import AxeBuilder from '@axe-core/playwright';
import { expect } from '@playwright/test';
import { deflateSync } from 'node:zlib';

export const PASSWORD = () => process.env.GX_E2E_PASSWORD;
export const USER = 'admin';

export const PAGES = [
  ['dashboard', 'What will you create today?'],
  ['images', 'Images'],
  ['video', 'Video'],
  ['music', 'Music'],
  ['library', 'Library'],
  ['history', 'History'],
];

// Collects console errors, page errors, CSP violations and failed requests.
export function watchPage(page, { allow = [] } = {}) {
  const problems = [];
  const allowed = (text) => allow.some((rx) => rx.test(text));
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !allowed(msg.text())) problems.push(`console: ${msg.text()}`);
  });
  page.on('pageerror', (err) => problems.push(`pageerror: ${err.message}`));
  page.on('requestfailed', (req) => {
    const failure = req.failure();
    // Aborted requests (navigation, media element range probes) are expected.
    if (failure && !/ERR_ABORTED|NS_BINDING_ABORTED/.test(failure.errorText)) {
      problems.push(`requestfailed: ${req.url()} ${failure.errorText}`);
    }
  });
  page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => {
      console.error(`CSP violation: ${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  return problems;
}

export async function login(page, { password = PASSWORD(), username = USER } = {}) {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', username);
  await page.fill('#login-pass', password);
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
  await expect(page.locator('#page-dashboard h1')).toHaveText('What will you create today?');
}

export async function gotoPage(page, name) {
  const link = page.locator(`#rail a[data-page="${name}"]`);
  await link.click();
  await expect(link).toHaveAttribute('aria-current', 'page');
  await expect(page.locator(`#page-${name} h1`)).toBeVisible();
  await expect(page.locator(`#page-${name} .loading`)).toHaveCount(0, { timeout: 30_000 });
}

export async function axeCheck(page, label) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}`);
  expect(summary, `${label}: axe WCAG 2.2 AA violations`).toEqual([]);
}

// Waits until a job card in `scope` shows the given phase.
export async function expectPhase(scope, phase, timeout = 60_000) {
  await expect(scope.locator('.phase-badge').first()).toHaveAttribute('data-phase', phase, { timeout });
}

function crc32(buf) {
  let c = ~0;
  for (const b of buf) {
    c ^= b;
    for (let k = 0; k < 8; k += 1) c = (c >>> 1) ^ (0xEDB88320 & -(c & 1));
  }
  return ~c >>> 0;
}

// A valid RGB PNG gradient (w x h), built with zlib like the fixture does.
export function pngBuffer(w = 96, h = 96) {
  const chunk = (tag, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(tag), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(td));
    return Buffer.concat([len, td, crc]);
  };
  const rows = [];
  for (let y = 0; y < h; y += 1) {
    const row = Buffer.alloc(1 + w * 3);
    for (let x = 0; x < w; x += 1) row.set([(x * 5) % 256, (y * 3) % 256, 180], 1 + x * 3);
    rows.push(row);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(Buffer.concat(rows))), chunk('IEND', Buffer.alloc(0))]);
}

// A short, valid 16-bit PCM WAV (sine), for audio uploads.
export function wavBuffer(seconds = 1, rate = 8000) {
  const n = Math.floor(seconds * rate);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write('WAVE', 8);
  buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i += 1) buf.writeInt16LE(Math.round(Math.sin((i / rate) * 2 * Math.PI * 440) * 12000), 44 + i * 2);
  return buf;
}

export async function noHorizontalOverflow(page) {
  const { sw, cw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  expect(sw, `scrollWidth ${sw} > clientWidth ${cw}`).toBeLessThanOrEqual(cw + 1);
}
