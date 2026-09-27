// Shared helpers for the V4.1 e2e suite (offline.v41.spec.js against
// fixture_server_v41.py). The pre-V4.1 PAGES map and the 'Dashboard'
// default page were retired with the old eleven-alias world; the rebuilt
// dashboard defaults to Overview.
import AxeBuilder from '@axe-core/playwright';
import { expect } from '@playwright/test';

export const PAGES = [
  ['overview', 'Overview'],
  ['model', 'Model'],
  ['performance', 'Performance'],
  ['requests', 'Requests'],
  ['agents', 'Agents'],
  ['tasks', 'Tasks'],
  ['projects', 'Projects'],
  ['files', 'Files'],
  ['storage', 'Storage'],
  ['logs', 'Logs'],
  ['network', 'Network'],
  ['updates', 'Updates'],
  ['settings', 'Settings / System'],
  ['recovery', 'Recovery'],
  ['jobs', 'Jobs / Actions'],
  ['keys', 'API Keys'],
];

// Collects console errors, page errors, CSP violations and failed requests.
export function watchPage(page) {
  const problems = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') problems.push(`console: ${msg.text()}`);
  });
  page.on('pageerror', (err) => problems.push(`pageerror: ${err.message}`));
  page.on('requestfailed', (req) => {
    const failure = req.failure();
    // Aborted polling requests on navigation are expected.
    if (failure && !/ERR_ABORTED/.test(failure.errorText)) problems.push(`requestfailed: ${req.url()} ${failure.errorText}`);
  });
  page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => {
      console.error(`CSP violation: ${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  return problems;
}

export async function login(page, password, username = process.env.GX_UI_USER || 'admin') {
  await page.goto('/');
  await expect(page.locator('#login-view')).toBeVisible();
  await page.fill('#login-user', username);
  await page.fill('#login-pass', password);
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
  await expect(page.locator('.page-title')).toHaveText('Overview');
}

export async function gotoPage(page, name, title) {
  await page.click(`#sidenav a[data-page="${name}"]`);
  await expect(page.locator('.page-title')).toHaveText(title);
  await expect(page.locator('.page .loading')).toHaveCount(0, { timeout: 60_000 });
}

export async function axeCheck(page, label) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  const serious = results.violations.filter((v) => ['serious', 'critical'].includes(v.impact));
  const summary = serious.map((v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}`);
  expect(summary, `${label}: axe serious/critical violations`).toEqual([]);
  return results.violations.length;
}
