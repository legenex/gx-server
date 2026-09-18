import { expect, test } from '@playwright/test';
import { login, watchPage } from './helpers.js';
import { livePassword, modelOperation, waitJobDone, waitModelState } from './live-helpers.js';

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ page }) => {
  await login(page, livePassword());
});

test('CSRF: Restart gx-mini then Load stays valid without a page refresh', async ({ page }) => {
  test.setTimeout(20 * 60_000);
  const problems = watchPage(page);
  page.on('response', async (res) => {
    if (res.url().includes('/api/') && res.status() === 403) {
      const body = await res.text().catch(() => '');
      problems.push(`csrf-403 ${res.url()} ${body.slice(0, 200)}`);
    }
  });
  await modelOperation(page, 'gx-mini', 'restart');
  const restart = await waitJobDone(page, 10 * 60_000);
  expect(restart.state, restart.output.join('\n')).toBe('succeeded');
  await waitModelState(page, 'gx-mini', ['loaded'], 8 * 60_000);
  expect(problems.filter((p) => /csrf|403|invalid CSRF|missing CSRF/i.test(p))).toEqual([]);
});
