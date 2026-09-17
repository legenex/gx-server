import AxeBuilder from '@axe-core/playwright';
import { expect } from '@playwright/test';

export const PAGES = [
  ['dashboard', 'Dashboard'],
  ['resources', 'Resource Control'],
  ['models', 'Models'],
  ['manager', 'Model Manager'],
  ['storage', 'Storage & Cleanup'],
  ['setup', 'Setup'],
  ['runtime', 'Runtime'],
  ['cluster', 'Cluster'],
  ['jobs', 'Jobs / Queue'],
  ['logs', 'Logs'],
  ['playground', 'API Playground'],
  ['docs', 'Docs'],
  ['keys', 'API Keys'],
  ['settings', 'Settings / System'],
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
  await expect(page.locator('.page-title')).toHaveText('Dashboard');
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
