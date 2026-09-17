// History page and the dashboard resource widget (profile switching).
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, watchPage } from './helpers.js';

test('History lists media and music jobs with phases, filters and actions', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const media = (await (await page.request.get('/api/media/jobs')).json()).jobs;
  const music = (await (await page.request.get('/api/music/jobs')).json()).jobs;
  expect(media.length + music.length).toBeGreaterThan(0);
  await gotoPage(page, 'history');
  const list = page.locator('#history-list');
  await expect(list.locator('.job-card').first()).toBeVisible();
  await expect.poll(() => list.locator('.job-card').count()).toBeGreaterThanOrEqual(Math.min(150, media.length + music.length));
  for (const j of [...media.slice(0, 3), ...music.slice(0, 3)]) {
    await expect(list.locator(`.job-card[data-job="${j.id}"] .phase-badge`)).toBeVisible();
  }
  await expect(list.locator('.phase-badge').first()).toHaveText(/QUEUED|WAITING FOR RESOURCE|LOADING MODEL|PREPARING|GENERATING|PROCESSING|SAVING|COMPLETE|FAILED|CANCELLED/);
  await expect(list.locator('.job-elapsed').first()).toHaveText(/^\d+:\d\d/);
  await axeCheck(page, 'history');

  await page.getByRole('group', { name: 'Kind' }).getByRole('button', { name: 'Music' }).click();
  await expect(list.locator('.job-kind-image, .job-kind-video')).toHaveCount(0);
  await page.getByRole('group', { name: 'Status' }).getByRole('button', { name: 'Completed' }).click();
  await expect(list.locator('.phase-badge:not([data-phase="COMPLETE"])')).toHaveCount(0);
  // Open result of a completed music job goes to the Music workspace.
  await list.getByRole('button', { name: 'Open result' }).first().click();
  await expect(page.locator('#page-music h1')).toHaveText('Music');
  await expect(page.locator('.track-card.is-focus')).toBeVisible();

  // Retry resubmits the same recipe for a failed job, if there is one.
  await gotoPage(page, 'history');
  await page.getByRole('group', { name: 'Status' }).getByRole('button', { name: 'Failed' }).click();
  const retry = list.getByRole('button', { name: 'Retry' });
  if (await retry.count()) {
    const failedId = await list.locator('.job-card').first().getAttribute('data-job');
    const failed = [...media, ...music].find((j) => j.id === failedId);
    const [req] = await Promise.all([
      page.waitForRequest((r) => /\/api\/(media|music)\/jobs$/.test(r.url()) && r.method() === 'POST'),
      retry.first().click(),
    ]);
    if (failed && failed.kind) expect(req.postDataJSON()).toMatchObject({ kind: failed.kind, source_id: failed.source_id });
    expect((await req.response()).status()).toBe(202);
  }
  expect(problems).toEqual([]);
});

test('profile selector offers exactly Auto/Text/Media/Music/Max and switching applies', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const summary = await (await page.request.get('/api/resources/summary')).json();
  const radios = page.locator('#profile-select [role="radio"]');
  await expect(radios).toHaveCount(5);
  expect(await radios.evaluateAll((r) => r.map((x) => x.dataset.profile))).toEqual(['auto', 'text', 'media', 'music', 'max']);
  const labels = await radios.allTextContents();
  ['Auto', 'Text', 'Media', 'Music', 'Max'].forEach((w, i) => expect(labels[i]).toMatch(new RegExp(`^${w}`)));
  await expect(page.getByText(/maintenance/i)).toHaveCount(0);
  await expect(page.locator('.res-row')).toHaveText([/Text/, /Image/, /Video/, /Music/, /Max/]);
  await expect(page.locator('#queued-count')).toHaveText(/\d+ jobs? queued/);
  await expect(page.getByRole('link', { name: 'Open Advanced Resource Controls' })).toHaveAttribute('href', summary.control_center_url);
  await expect(page.locator('.studio-row')).toHaveCount(3);

  // Text: the plan drains nothing in the fixture -> applied without a dialog.
  const [post] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/resources/profile') && r.method() === 'POST'),
    radios.nth(1).click(),
  ]);
  expect(post.postDataJSON()).toEqual({ profile: 'text' });
  expect((await post.response()).status()).toBe(200);
  await expect(page.locator('#active-profile')).toHaveText(/^Text/);
  await expect(page.locator('#profile-select [data-profile="text"]')).toHaveAttribute('aria-checked', 'true');
  await expect(page.locator('#res-pill .res-profile')).toHaveText(/^Text/);

  // Max needs the typed phrase; cancel leaves the profile alone.
  await page.locator('#profile-select [data-profile="max"]').click();
  const dlg = page.locator('dialog[open]');
  await expect(dlg.getByRole('heading', { name: 'Switch to Max?' })).toBeVisible();
  const ok = dlg.locator('[data-role="confirm-ok"]');
  await expect(ok).toBeDisabled();
  await dlg.getByLabel('Type the confirmation phrase').fill('gx-max');
  await expect(ok).toBeEnabled();
  await axeCheck(page, 'max confirmation');
  await dlg.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.locator('#active-profile')).toHaveText(/^Text/);

  // Back to Auto.
  await page.locator('#profile-select [data-profile="auto"]').click();
  await expect(page.locator('#active-profile')).toHaveText('Auto');
  const after = await (await page.request.get('/api/resources/summary')).json();
  expect(after.profile).toBe('auto');
  expect(problems).toEqual([]);
});

test('dashboard quick create submits and opens the workspace', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await page.getByRole('group', { name: 'What to create' }).getByRole('button', { name: 'Music' }).click();
  await page.getByLabel('Describe what you want to create').fill('e2e quick lo-fi beat');
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/music/jobs') && r.method() === 'POST'),
    page.getByLabel('Describe what you want to create').press('Control+Enter'),
  ]);
  expect(req.postDataJSON()).toEqual({ operation: 'generate', prompt: 'e2e quick lo-fi beat' });
  await expect(page.locator('#page-music h1')).toHaveText('Music');
  await expect(page.locator('#music-jobs .job-card').first()).toBeVisible();
  // Quick-create cards focus the composer.
  await gotoPage(page, 'dashboard');
  await page.getByRole('link', { name: /New image/ }).click();
  await expect(page.locator('#image-prompt')).toBeFocused();
  expect(problems).toEqual([]);
});
