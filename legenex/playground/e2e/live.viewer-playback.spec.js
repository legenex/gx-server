// LIVE regression: a playing clip must survive a viewer re-render.
//
// createStage().renderViewer() used to clear the viewer and rebuild its
// <video>, so any re-render (a job card ticking, a favourite toggled) detached
// the element a user was watching and playback restarted at 0. The WAN live
// acceptance hit this as `played.advanced === 0` and it read as a generation
// failure. workspace.js now keeps the media wrapper across re-renders.
//
//   npx playwright test --project=live e2e/live.viewer-playback.spec.js
//
// Needs at least one video in the Library; it generates nothing and costs no GPU.
import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { gotoPage } from './helpers.js';

test.describe.configure({ timeout: 180_000 });
test.use({ actionTimeout: 20_000 });

test('a playing video survives a viewer re-render', async ({ page }) => {
  await page.goto('/');
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', readFileSync('/srv/projects/gx-cluster/secrets/control-ui/acceptance-password', 'utf8').trim());
  await page.click('#login-submit');
  await expect(page.locator('#app-view')).toBeVisible();
  await gotoPage(page, 'video');

  // Pick the newest clip from the history filmstrip.
  const first = page.locator('#filmstrip .result-btn').first();
  await expect(first).toBeVisible({ timeout: 30_000 });
  await first.click();
  const video = page.locator('#viewer video').first();
  await expect(video).toBeVisible();
  await video.evaluate((v) => { v.muted = true; return v.play(); });
  await page.waitForTimeout(1200);
  const before = await video.evaluate((v) => v.currentTime);
  expect(before, 'the clip should be playing before the re-render').toBeGreaterThan(0.2);

  // Force a viewer re-render the way the app does (toggle favourite).
  await page.locator('#viewer [data-action="favourite"]').click();
  await page.waitForTimeout(900);
  const after = await page.locator('#viewer video').first().evaluate((v) => ({ t: v.currentTime, paused: v.paused, err: v.error }));
  console.log(JSON.stringify({ before, after }));
  expect(after.err, 'no media error after the re-render').toBeNull();
  expect(after.t, 'playback must continue, not restart at 0').toBeGreaterThanOrEqual(before);
  // put the favourite back
  await page.locator('#viewer [data-action="favourite"]').click();
});
