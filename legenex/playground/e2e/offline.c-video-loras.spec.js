// Video page, Build V3 WAN: LoRA library, LoRA stack, presets, advanced
// workflow view, generation history and errors. The fixture runs the real
// media-router code with a fake ComfyUI and synthetic LoRA files.
import { expect, test } from '@playwright/test';
import { axeCheck, expectPhase, gotoPage, login, watchPage } from './helpers.js';

const postVideo = (page) => page.waitForRequest((r) => r.url().endsWith('/api/video/generate') && r.method() === 'POST');

async function openLibrary(page) {
  await page.locator('[data-action="add-lora"]').click();
  const drawer = page.getByRole('dialog', { name: 'Add a LoRA' });
  await expect(drawer.locator('.wan-entry').first()).toBeVisible();
  return drawer;
}

function entry(scope, name) {
  return scope.locator('.wan-entry').filter({ has: scope.page().locator('.wan-name', { hasText: new RegExp(`^${name}$`) }) });
}

async function closeDialog(page) {
  await page.keyboard.press('Escape');
  await expect(page.locator('dialog[open]')).toHaveCount(0);
}

test('LoRA library: search, filter, details, keyboard reorder and add to the stack', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'video');
  await expect(page.locator('#wan-loras-h')).toHaveText('LoRAs');
  await expect(page.locator('.wan-stack .wan-empty')).toBeVisible();
  const drawer = await openLibrary(page);
  await axeCheck(page, 'LoRA library drawer');

  await drawer.getByRole('searchbox', { name: 'Search LoRAs' }).fill('glow');
  await expect(drawer.locator('.wan-entry')).toHaveCount(1);
  await drawer.getByRole('searchbox', { name: 'Search LoRAs' }).fill('');
  await drawer.getByRole('combobox', { name: 'Filter LoRAs' }).selectOption('incompatible');
  await expect(entry(drawer, 'qwen_portrait')).toBeVisible();
  await expect(entry(drawer, 'CinematicGlow')).toHaveCount(0);
  await expect(entry(drawer, 'qwen_portrait').getByRole('button', { name: 'Add qwen_portrait' })).toBeDisabled();
  await drawer.getByRole('combobox', { name: 'Filter LoRAs' }).selectOption('all');

  const glow = entry(drawer, 'CinematicGlow');
  await expect(glow.locator('.badge', { hasText: 'Paired' })).toBeVisible();
  await expect(glow.locator('.badge', { hasText: 'Compatible' })).toBeVisible();
  await glow.getByRole('button', { name: 'Show details for CinematicGlow' }).click();
  await expect(glow.locator('.wan-file code').first()).toHaveText('/srv/models/video/loras/wan22/paired/CinematicGlow_high_noise.safetensors');
  await expect(glow.getByText('high-noise expert', { exact: false }).first()).toBeVisible();
  await axeCheck(page, 'LoRA details');
  await glow.getByRole('button', { name: 'Hide details for CinematicGlow' }).click();

  // keyboard-accessible library reorder (no drag)
  const names = async () => drawer.locator('.wan-entry .wan-name').allTextContents();
  const before = await names();
  const second = before[1];
  await entry(drawer, second).getByRole('button', { name: `Move ${second} up` }).focus();
  await page.keyboard.press('Enter');
  await expect.poll(async () => (await names())[0]).toBe(second);
  await entry(drawer, second).getByRole('button', { name: `Move ${second} down` }).click();
  await expect.poll(async () => (await names())[1]).toBe(second);

  await glow.getByRole('button', { name: 'Add CinematicGlow' }).click();
  await entry(drawer, 'DetailBoost').getByRole('button', { name: 'Add DetailBoost' }).click();
  await closeDialog(page);

  const items = page.locator('.wan-stack .wan-item');
  await expect(items).toHaveCount(2);
  await expect(page.getByRole('slider', { name: 'CinematicGlow: high-noise strength' })).toHaveValue('0.8');
  await expect(page.getByRole('slider', { name: 'CinematicGlow: low-noise strength' })).toHaveValue('0.8');
  // two enabled LoRAs: the 0.5 starting point is offered, never applied by itself
  await expect(page.locator('[data-action="balance"]')).toBeVisible();
  await expect(page.getByRole('slider', { name: 'DetailBoost: high-noise strength' })).toHaveValue('0.8');
  await page.locator('[data-action="balance"]').click();
  await expect(page.getByRole('slider', { name: 'DetailBoost: low-noise strength' })).toHaveValue('0.5');
  await page.getByRole('slider', { name: 'CinematicGlow: low-noise strength' }).fill('1.2');
  await page.getByRole('button', { name: 'Move DetailBoost up' }).click();
  await expect(items.first().locator('.wan-name')).toHaveText('DetailBoost');
  await axeCheck(page, 'video page with a LoRA stack');
  expect(problems).toEqual([]);
});

test('generate with LoRAs, inspect the workflow, and reload it from history', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'video');
  const drawer = await openLibrary(page);
  await entry(drawer, 'CinematicGlow').getByRole('button', { name: 'Add CinematicGlow' }).click();
  await entry(drawer, 'FilmGrain').getByRole('button', { name: 'Add FilmGrain' }).click();
  await entry(drawer, 'SoloMotion').getByRole('button', { name: 'Add SoloMotion' }).click();
  await closeDialog(page);

  await page.fill('#video-prompt', 'e2e two adults walking through a hotel lobby');
  // a general LoRA needs an explicit branch
  await page.click('#generate-btn');
  await expect(page.locator('.panel-foot .form-error')).toContainText('Choose where “FilmGrain” applies');
  await page.getByRole('combobox', { name: 'FilmGrain: where it applies' }).selectOption('both');
  await page.getByRole('slider', { name: 'FilmGrain: low-noise strength' }).fill('0.3');
  await page.getByRole('switch', { name: 'SoloMotion: enabled' }).uncheck({ force: true });
  await expect(page.getByRole('slider', { name: 'SoloMotion: high-noise strength' })).toBeVisible();
  await expect(page.getByRole('slider', { name: 'SoloMotion: low-noise strength' })).toHaveCount(0);

  // advanced settings + workflow preview
  await page.locator('#video-advanced > summary').click();
  await page.fill('#video-shift', '6');
  await page.locator('[data-action="preview-workflow"]').click();
  const adv = page.getByRole('dialog', { name: 'Advanced view' });
  await expect(adv.locator('.wan-table tbody tr')).toHaveCount(6); // base + CinematicGlow + FilmGrain per expert
  await expect(adv.locator('.wan-table')).toContainText('wan22/paired/CinematicGlow_high_noise.safetensors');
  await expect(adv.locator('.wan-json')).toContainText('"LoraLoaderModelOnly"');
  await expect(adv.locator('.wan-json')).not.toContainText('/srv/');
  await expect(adv.locator('.wan-json')).not.toContainText('SoloMotion');
  await axeCheck(page, 'advanced workflow view');
  await closeDialog(page);

  const [req] = await Promise.all([postVideo(page), page.click('#generate-btn')]);
  const body = req.postDataJSON();
  expect(body).toMatchObject({ prompt: 'e2e two adults walking through a hotel lobby', advanced: { shift: 6 } });
  expect(body.loras.map((l) => [l.display_name, l.apply, l.enabled])).toEqual([
    ['CinematicGlow', 'pair', true], ['FilmGrain', 'both', true], ['SoloMotion', 'high', false]]);
  expect(body.loras[1]).toMatchObject({ strength_low: 0.3, strength_high: 0.8 });
  expect(Object.keys(body.loras[0]).sort()).toEqual(['apply', 'display_name', 'enabled', 'entry_id', 'high_file', 'low_file', 'strength_high', 'strength_low']);
  expect((await req.response()).status()).toBe(202);
  await expectPhase(page.locator('#ws-jobs'), 'COMPLETE', 60_000);
  await expect(page.locator('#viewer video')).toBeVisible();
  await expect(page.locator('#viewer [data-action="workflow"]')).toBeVisible();

  const hist = page.locator('.wan-history .wan-gen').first();
  await expect(hist).toContainText('e2e two adults walking through a hotel lobby');
  await expect(hist.locator('.badge', { hasText: 'Complete' })).toBeVisible();
  await expect(hist.locator('.badge', { hasText: 'CinematicGlow 0.80/0.80' })).toBeVisible();
  await hist.getByRole('button', { name: 'Details, workflow and metadata' }).click();
  const details = page.getByRole('dialog', { name: 'Video generation' });
  await expect(details.locator('video')).toBeVisible();
  await expect(details.getByRole('link', { name: 'Reuse in Creative Flows' })).toHaveAttribute('href', /^#\/flows\?asset=a_[0-9a-f]{24}$/);
  await details.locator('summary', { hasText: 'Advanced view' }).click();
  await expect(details.getByRole('link', { name: 'Download JSON' })).toHaveAttribute('href', /\/api\/video\/generations\/[0-9a-f]{16}\/workflow$/);
  await expect(details.locator('.wan-advanced')).toContainText('gx-wan-lora/1+wan22-t2v-a14b-uncensored@');
  await expect(details.locator('.wan-advanced')).toContainText('e2e-prompt-');
  await axeCheck(page, 'generation details');
  const download = await Promise.all([page.waitForEvent('download'), details.getByRole('link', { name: 'Download JSON' }).click()]);
  expect(download[0].suggestedFilename()).toMatch(/^gx-wan22-[0-9a-f]{16}-workflow\.json$/);
  await closeDialog(page);

  // reload the settings into a changed form
  await page.fill('#video-prompt', 'something else');
  await page.getByRole('button', { name: 'Remove CinematicGlow' }).click();
  await hist.getByRole('button', { name: 'Load settings into the form' }).click();
  await expect(page.locator('#video-prompt')).toHaveValue('e2e two adults walking through a hotel lobby');
  await expect(page.locator('.wan-stack .wan-item')).toHaveCount(3);
  await expect(page.getByRole('slider', { name: 'FilmGrain: low-noise strength' })).toHaveValue('0.3');
  await expect(page.locator('#video-shift')).toHaveValue('6');
  expect(problems).toEqual([]);
});

test('presets: apply, save, rename, duplicate and delete', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'video');
  const sel = page.getByRole('combobox', { name: 'Preset' });
  await expect(sel.locator('option')).toHaveCount(6);
  await sel.selectOption({ label: 'Cinematic Realism (example)' });
  await page.locator('[data-action="apply-preset"]').click();
  await expect(page.locator('.chips-size .chip[data-value="832x480"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#video-suffix')).toHaveValue(/cinematic lighting/);
  await expect(page.locator('#video-negative')).toHaveValue(/cartoon/);
  await sel.selectOption({ label: 'Character Consistency (example)' });
  await page.locator('[data-action="apply-preset"]').click();
  await expect(page.locator('.chips-size .chip[data-value="480x832"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('spinbutton', { name: 'Seed' })).toHaveValue('424242');

  await page.locator('[data-action="save-preset"]').click();
  await page.getByRole('dialog', { name: 'Save preset' }).getByLabel('Preset name').fill('E2E portrait look');
  await page.getByRole('dialog', { name: 'Save preset' }).getByRole('button', { name: 'Save' }).click();
  await expect(sel).toHaveValue(/^wp_[0-9a-f]{16}$/);
  await expect(sel.locator('option:checked')).toHaveText('E2E portrait look');

  await page.locator('[data-action="manage-presets"]').click();
  const mgr = page.getByRole('dialog', { name: 'Video presets' });
  await expect(mgr.locator('.wan-preset')).toHaveCount(6);
  await axeCheck(page, 'preset manager');
  await mgr.getByRole('button', { name: 'Rename E2E portrait look' }).click();
  const ren = page.getByRole('dialog', { name: 'Rename preset' });
  await ren.getByLabel('Preset name').fill('E2E portrait');
  await ren.getByRole('button', { name: 'Save' }).click();
  await expect(mgr.getByRole('button', { name: 'Duplicate E2E portrait' })).toBeVisible();
  await mgr.getByRole('button', { name: 'Duplicate E2E portrait' }).click();
  await expect(mgr.locator('.wan-preset')).toHaveCount(7);
  for (const name of ['E2E portrait copy', 'E2E portrait']) {
    await mgr.getByRole('button', { name: `Delete ${name}`, exact: true }).click();
    await page.getByRole('dialog', { name: `Delete “${name}”?` }).getByRole('button', { name: 'Delete' }).click();
    await expect(mgr.getByRole('button', { name: `Delete ${name}`, exact: true })).toHaveCount(0);
  }
  await expect(mgr.locator('.wan-preset')).toHaveCount(5);
  await closeDialog(page);
  expect(problems).toEqual([]);
});

test('manual pairing, unpairing and rescan', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'video');
  await page.locator('[data-action="open-lora-library"]').click();
  const drawer = page.getByRole('dialog', { name: 'LoRA library' });
  await expect(drawer.locator('.wan-entry').first()).toBeVisible();
  await expect(drawer.getByRole('button', { name: /^Add / })).toHaveCount(0);
  await drawer.getByRole('combobox', { name: 'High-noise file' }).selectOption('orphan_high_noise.safetensors');
  await drawer.getByRole('combobox', { name: 'Low-noise file' }).selectOption('stray_low_noise.safetensors');
  await drawer.locator('[data-action="pair"]').click();
  const paired = entry(drawer, 'orphan');
  await expect(paired.locator('.badge', { hasText: 'Manual pair' })).toBeVisible();
  await paired.getByRole('button', { name: 'Unpair orphan' }).click();
  await page.getByRole('dialog', { name: 'Unpair these files?' }).getByRole('button', { name: 'Unpair' }).click();
  await expect(entry(drawer, 'orphan_high_noise')).toBeVisible();
  await expect(entry(drawer, 'orphan_high_noise').locator('.badge', { hasText: 'High only' })).toBeVisible();
  await drawer.locator('[data-action="rescan"]').click();
  await expect(drawer.locator('.row-between .muted')).toContainText('scanned just now');
  await expect(entry(drawer, 'corrupt_style').locator('.badge', { hasText: 'Incompatible' })).toBeVisible();
  await closeDialog(page);
  expect(problems).toEqual([]);
});

test('a failed generation explains itself; phone layout stays usable', async ({ page }) => {
  const problems = watchPage(page, { allow: [/status of 4\d\d/] });
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto('/#/video');
  await expect(page.locator('#page-video h1')).toHaveText('Video');
  await page.fill('#video-prompt', 'e2e-oom stress scene');
  await page.click('#generate-btn');
  await expectPhase(page.locator('#ws-jobs'), 'FAILED', 60_000);
  await expect(page.locator('#ws-jobs .job-error')).toContainText('ran out of memory');
  const errors = page.locator('.wan-errors');
  await page.locator('summary', { hasText: 'Errors' }).click();
  await expect(errors.locator('.wan-err').first()).toContainText('ran out of memory');
  await expect(errors.locator('.wan-err').first().locator('.badge')).toHaveText('out_of_memory');
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(390);
  await axeCheck(page, 'video page on a phone');
  expect(problems).toEqual([]);
});
