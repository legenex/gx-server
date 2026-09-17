// Video workspace: text-to-video completes; image-to-video and video edit submit.
import { expect, test } from '@playwright/test';
import { axeCheck, expectPhase, gotoPage, login, watchPage } from './helpers.js';

const postJob = (page) => page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST');

test('text to video completes and plays in the viewer; edit again submits v2v', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'video');
  await expect(page.getByRole('tab', { name: 'Text to Video' })).toHaveAttribute('aria-selected', 'true');
  await page.fill('#video-prompt', 'e2e slow pan over a misty lake');
  await page.getByRole('slider', { name: 'Length' }).fill('2');
  await page.getByRole('slider', { name: 'Frame rate' }).fill('12');
  await page.locator('.chips-size .chip[data-value="832x480"]').click();
  // text to video goes through the Wan video API (LoRAs, presets, history)
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/video/generate') && r.method() === 'POST'),
    page.click('#generate-btn')]);
  expect(req.postDataJSON()).toMatchObject({ prompt: 'e2e slow pan over a misty lake', seconds: 2, fps: 12, size: '832x480', loras: [] });
  expect((await req.response()).status()).toBe(202);
  await expectPhase(page.locator('#ws-jobs'), 'COMPLETE', 90_000);
  const video = page.locator('#viewer video');
  await expect(video).toBeVisible();
  await expect(video).toHaveAttribute('src', /\/api\/media\/assets\/a_[0-9a-f]{24}\/file$/);
  await expect(video).toHaveAttribute('controls', '');
  await expect(page.locator('#viewer a[download]')).toHaveAttribute('href', /download=1/);
  await axeCheck(page, 'video with a result');

  const assetId = await page.locator('#session-results .result-tile.is-selected').getAttribute('data-asset');
  await page.locator('#viewer [data-action="edit-again"]').click();
  await expect(page.getByRole('tab', { name: 'Video Edit' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('slider', { name: 'Frame rate' })).toBeHidden();
  const strength = page.getByRole('slider', { name: 'Edit strength' });
  await strength.fill('0.3');
  await expect(page.locator('.strength-explain')).toContainText('Restyle');
  await strength.fill('0.8');
  await expect(page.locator('.strength-explain')).toContainText('Strong instruction edit');
  await page.fill('#video-prompt', 'make it night time');
  const [vreq] = await Promise.all([postJob(page), page.click('#generate-btn')]);
  expect(vreq.postDataJSON()).toMatchObject({ kind: 'v2v', source_id: assetId, strength: 0.8, prompt: 'make it night time' });
  expect(vreq.postDataJSON().fps).toBeUndefined();
  expect((await vreq.response()).status()).toBe(202);
  expect(problems).toEqual([]);
});

test('Make video from an image opens image-to-video with the source preselected', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'images');
  const first = page.locator('#filmstrip .result-tile').first();
  const imageId = await first.getAttribute('data-asset');
  await first.locator('button').click();
  await page.locator('#viewer [data-action="make-video"]').click();
  await expect(page.locator('#page-video h1')).toHaveText('Video');
  await expect(page.getByRole('tab', { name: 'Image to Video' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.source-chip')).toBeVisible();
  await page.click('#generate-btn');
  await expect(page.locator('.panel-foot .form-error')).toHaveText('Write a prompt first.');
  await page.fill('#video-prompt', 'the scene comes alive');
  const [req] = await Promise.all([postJob(page), page.click('#generate-btn')]);
  expect(req.postDataJSON()).toMatchObject({ kind: 'i2v', source_id: imageId });
  expect((await req.response()).status()).toBe(202);
  expect(problems).toEqual([]);
});
