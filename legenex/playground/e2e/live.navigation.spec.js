// LIVE navigation acceptance against the DEPLOYED GX-Playground on gx10-01.
// This is the Build V3 product-shape gate: it asserts the real browser sees the
// complete navigation, that every page opens without console or network errors,
// and that each page passes axe WCAG 2.2 AA and is reachable from the keyboard.
// It generates nothing, so it is safe to run at any time and costs no GPU.
//
//   cd legenex/playground && npx playwright test --project=live e2e/live.navigation.spec.js
//
// Evidence (screenshots + a JSON report): $GX_EVIDENCE_DIR, default
// /srv/logs/acceptance/build-v3/plt/navigation-<timestamp>/
import { expect, test } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { axeCheck, gotoPage, watchPage } from './helpers.js';

const PASSWORD_FILE = '/srv/projects/gx-cluster/secrets/control-ui/acceptance-password';
const EVIDENCE = process.env.GX_EVIDENCE_DIR
  || `/srv/logs/acceptance/build-v3/plt/navigation-${new Date().toISOString().replace(/[-:.]/g, '').slice(0, 15)}Z`;

// The complete Build V3 navigation, in the order the product specifies.
const NAV = [
  ['create', 'dashboard', 'Dashboard'],
  ['create', 'flows', 'Creative Flows'],
  ['create', 'images', 'Images'],
  ['create', 'video', 'Video'],
  ['create', 'music', 'Music'],
  ['create', 'voice', 'Voice'],
  ['realtime', 'live', 'Live'],
  ['realtime', 'call', 'Call Agents'],
  ['manage', 'library', 'Library'],
  ['manage', 'history', 'History'],
  ['manage', 'models', 'Models'],
  ['manage', 'logs', 'Logs'],
  ['manage', 'settings', 'Settings'],
];

const report = { at: new Date().toISOString(), url: null, nav: {}, pages: {}, header: {} };

// Playwright restarts the worker after a failing test, which resets module state.
// Merge with what is already on disk so one missing page cannot erase the
// results of the pages that were checked before it.
function save() {
  const file = join(EVIDENCE, 'navigation.json');
  let prev = {};
  try { prev = JSON.parse(readFileSync(file, 'utf8')); } catch { prev = {}; }
  const merged = {
    ...prev, ...report,
    nav: { ...(prev.nav || {}), ...report.nav },
    pages: { ...(prev.pages || {}), ...report.pages },
    header: { ...(prev.header || {}), ...report.header },
  };
  writeFileSync(file, JSON.stringify(merged, null, 1));
}

// Each test signs in on its own, so a missing page must not hide the others.
test.use({ screenshot: 'off', video: 'off', trace: 'off', actionTimeout: 15_000 });
// The `live` project allows 60 minutes per test for real generations. This spec
// generates nothing, so it must fail fast rather than hang on a missing page.
test.describe.configure({ timeout: 120_000 });

test.beforeAll(() => mkdirSync(EVIDENCE, { recursive: true }));

async function signIn(page) {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', readFileSync(PASSWORD_FILE, 'utf8').trim());
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
}

test('the deployed navigation contains every Build V3 page, with no dead links', async ({ page }) => {
  const problems = watchPage(page);
  await signIn(page);
  report.url = page.url();

  const missing = [];
  for (const [group, name, label] of NAV) {
    const link = page.locator(`#nav-${group} a[data-page="${name}"]`);
    const count = await link.count();
    const text = count ? (await link.locator('.rail-label').innerText()).trim() : null;
    const href = count ? await link.getAttribute('href') : null;
    report.nav[name] = { group, present: count > 0, label: text, href };
    if (!count) missing.push(`${group}/${name} (${label})`);
    else if (text !== label) missing.push(`${group}/${name}: labelled "${text}", expected "${label}"`);
    else if (href !== `#/${name}`) missing.push(`${group}/${name}: href "${href}", expected "#/${name}"`);
  }
  // The Control Center link stays in the header.
  const cc = page.locator('#app-view header a[href*="8088"], #app-view header a[data-control-center]');
  report.header.control_center = await cc.count() > 0 ? await cc.first().getAttribute('href') : null;

  await page.screenshot({ path: join(EVIDENCE, 'nav.png'), fullPage: false });
  save();
  expect(report.header.control_center, 'the header must link to the Control Center').not.toBeNull();
  expect(missing, 'navigation entries missing or wrong in the DEPLOYED Playground').toEqual([]);
  expect(problems, 'console/network problems on the shell').toEqual([]);
});

for (const [group, name, label] of NAV) {
  test(`${label} page opens cleanly and passes axe`, async ({ page }) => {
    const problems = watchPage(page);
    await signIn(page);
    const link = page.locator(`#nav-${group} a[data-page="${name}"]`);
    if (await link.count() === 0) {
      report.pages[name] = { missing: true };
      save();
      throw new Error(`${label} is not in the deployed navigation (the navigation test reports it)`);
    }
    await gotoPage(page, name);
    // gotoPage waits for `.loading`; pages that render skeletons instead would
    // otherwise be judged (and screenshotted) while they are still empty.
    const body = page.locator(`#page-${name}`);
    await expect(body.locator('.skeleton')).toHaveCount(0, { timeout: 30_000 });

    // The app's own error boundary. A page that threw while loading still has a
    // heading and passes axe, so without this a broken page reads as healthy —
    // which is exactly how "aiPanel is not defined" survived on Music.
    const boundary = body.locator('.callout-danger .callout-title', { hasText: 'could not be loaded' });
    const broken = await boundary.count()
      ? (await body.locator('.callout-danger .callout-text').first().innerText()).trim()
      : null;

    // A real page, not a placeholder.
    const text = (await body.innerText()).trim();
    const placeholder = /coming soon|not implemented|placeholder|lorem ipsum|TODO/i.test(text);
    // Every control must have an accessible name. Use textContent, not innerText:
    // innerText needs layout and returns '' for a label inside a collapsed
    // section, which would report a correctly labelled control as unnamed.
    const unnamed = await body.evaluate((el) => {
      const text = (node) => (node ? (node.textContent || '') : '').trim();
      const out = [];
      for (const c of el.querySelectorAll('button, a, input, select, textarea')) {
        if (c.closest('[hidden], [aria-hidden="true"]') || c.offsetParent === null) continue;
        const byIds = (c.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
          .map((id) => text(document.getElementById(id))).join(' ');
        const name = (c.getAttribute('aria-label') || byIds || c.getAttribute('title')
          || text(c.labels && c.labels[0]) || text(c)
          || (c.tagName === 'INPUT' && c.type === 'image' ? c.getAttribute('alt') : '') || '').trim();
        if (!name) {
          out.push(c.tagName.toLowerCase() + (c.id ? `#${c.id}` : '')
            + (c.className ? `.${String(c.className).split(' ')[0]}` : ''));
        }
      }
      return out.slice(0, 10);
    });
    // Keyboard: the page must be reachable and its first control focusable.
    await page.keyboard.press('Tab');
    const focusVisible = await page.evaluate(() => {
      const a = document.activeElement;
      if (!a || a === document.body) return false;
      const s = getComputedStyle(a);
      return s.outlineStyle !== 'none' || s.boxShadow !== 'none' || a.matches(':focus-visible');
    });

    report.pages[name] = { chars: text.length, placeholder, unnamed, focusVisible, broken,
      problems: [...problems] };
    await page.screenshot({ path: join(EVIDENCE, `page-${name}.png`), fullPage: true });
    save();

    expect(broken, `${label}: the page threw while loading`).toBeNull();
    expect(text.length, `${label}: the page rendered almost nothing`).toBeGreaterThan(40);
    expect(placeholder, `${label}: placeholder text is shipped`).toBe(false);
    expect(unnamed, `${label}: controls without an accessible name`).toEqual([]);
    expect(focusVisible, `${label}: nothing focusable with a visible focus ring`).toBe(true);
    expect(problems, `${label}: console/network problems`).toEqual([]);
    await axeCheck(page, label);
  });
}
