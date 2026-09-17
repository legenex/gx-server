// Images workspace (Build V3): model selector, edit modes, edit quality, masks.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, noHorizontalOverflow, watchPage } from './helpers.js';

const submitted = (page) => page.waitForRequest((r) => r.url().endsWith('/api/media/jobs') && r.method() === 'POST');

test('switch image models: sizes, quality and options follow the model', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const options = await (await page.request.get('/api/media/options')).json();
  expect(options.image_models.map((m) => m.id)).toEqual(['qwen-image-2512', 'qwen-image-edit-2511', 'visionmaster-pro-v3']);
  expect(options.image_models.find((m) => m.id === 'visionmaster-pro-v3').label).toBe('VisionmasterPro_V3');
  await gotoPage(page, 'images');
  const model = page.getByLabel('Model', { exact: true });
  await expect(model).toHaveValue('qwen-image-2512');
  await expect(model.locator('option')).toHaveText(['Qwen Image 2512', 'VisionmasterPro_V3']);
  await expect(page.getByRole('group', { name: 'Quality' })).toBeVisible();
  await expect(page.getByRole('switch', { name: 'Uncensored adapter' })).toBeVisible();
  await expect(page.getByRole('switch', { name: 'Quality tags' })).toBeHidden();

  await model.selectOption('visionmaster-pro-v3');
  await expect(page.locator('#image-model-hint')).toContainText('SDXL');
  await expect(page.getByRole('group', { name: 'Quality' })).toBeHidden();
  await expect(page.getByRole('switch', { name: 'Uncensored adapter' })).toBeHidden();
  await expect(page.getByRole('switch', { name: 'Quality tags' })).toBeVisible();
  const sizes = page.locator('.chips-size .chip');
  await expect(sizes.first()).toHaveAttribute('data-value', '1024x1024');
  await expect(page.locator('.chips-size .chip[aria-pressed="true"]')).toHaveAttribute('data-value', '832x1216');
  await expect(page.locator('.chips-size .chip[data-value="1328x1328"]')).toHaveCount(0);
  await axeCheck(page, 'images with VisionmasterPro_V3 selected');

  await page.fill('#image-prompt', 'e2e visionmaster portrait');
  await page.locator('label.switch', { hasText: 'Quality tags' }).click();
  await expect(page.getByRole('switch', { name: 'Quality tags' })).not.toBeChecked();
  const [req] = await Promise.all([submitted(page), page.click('#generate-btn')]);
  const body = req.postDataJSON();
  expect(body).toMatchObject({ kind: 't2i', image_model: 'visionmaster-pro-v3', size: '832x1216', quality_tags: false });
  expect(body.quality).toBeUndefined();
  expect(body.uncensored).toBeUndefined();
  expect((await req.response()).status()).toBe(202);
  const job = await (await req.response()).json();
  expect(job.params).toMatchObject({ image_model: 'visionmaster-pro-v3', size: '832x1216' });

  // Switching back restores the Qwen controls; the choice is remembered per mode.
  await model.selectOption('qwen-image-2512');
  await expect(page.getByRole('group', { name: 'Quality' })).toBeVisible();
  await page.getByRole('tab', { name: 'Edit' }).click();
  await expect(model).toHaveValue('qwen-image-edit-2511');
  await expect(model.locator('option')).toHaveText(['Qwen Image Edit 2511', 'VisionmasterPro_V3']);
  await page.getByRole('tab', { name: 'Variation' }).click();
  await expect(model).toBeDisabled();
  await expect(model).toHaveValue('qwen-image-edit-2511');
  await page.getByRole('tab', { name: 'Generate' }).click();
  await expect(model).toHaveValue('qwen-image-2512');
  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalOverflow(page);
  expect(problems).toEqual([]);
});

test('edit modes, edit quality and a masked edit (keyboard and rectangle entry)', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'images');
  await page.getByRole('tab', { name: 'Edit' }).click();
  await page.getByRole('button', { name: 'Choose from Library' }).click();
  await page.locator('dialog[open] .pick-tile').first().click();
  await expect(page.locator('#source-section .source-chip')).toBeVisible();

  const modes = page.getByRole('group', { name: 'Edit mode' });
  await expect(modes.locator('.chip')).toHaveText(['Change / replace', 'Add', 'Remove', 'Restyle', 'Background', 'Subject', 'Full transformation']);
  await expect(modes.locator('.chip[aria-pressed="true"]')).toHaveText('Change / replace');
  // Qwen instruction modes ignore strength; the slider is not offered.
  await expect(page.getByRole('slider', { name: 'Strength' })).toBeHidden();
  await modes.getByRole('button', { name: 'Full transformation' }).click();
  await expect(page.getByRole('slider', { name: 'Strength' })).toBeVisible();
  await expect(page.locator('#edit-mode-hint')).toContainText('Large changes');
  await modes.getByRole('button', { name: 'Background' }).click();
  await expect(page.getByRole('slider', { name: 'Strength' })).toBeHidden();

  // High quality exposes the negative prompt (it only matters with real guidance).
  const quality = page.getByRole('group', { name: 'Edit quality' });
  await expect(page.getByLabel('Negative prompt')).toBeHidden();
  await quality.getByRole('button', { name: /High quality/ }).click();
  await page.getByText('Advanced').click();
  await expect(page.getByLabel('Negative prompt')).toBeVisible();
  await page.getByLabel('Negative prompt').fill('blurry');

  // Mask: keyboard painting on the canvas.
  await page.locator('#mask-section summary').click();
  const canvas = page.getByRole('application', { name: /Mask painter/ });
  await canvas.focus();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('Space');
  await expect(page.locator('.mask-status')).toContainText('% of the image is selected');
  await page.keyboard.press('Control+z');
  await expect(page.locator('.mask-status')).toContainText('Nothing selected yet');
  // Rectangle entry validates bounds, then adds.
  await page.getByLabel('Left', { exact: true }).fill('80');
  await page.getByLabel('Width', { exact: true }).fill('40');
  await page.getByRole('button', { name: 'Add rectangle' }).click();
  await expect(page.locator('.mask-rect-entry .form-error')).toContainText('inside the image');
  await page.getByLabel('Left', { exact: true }).fill('10');
  await page.getByLabel('Top', { exact: true }).fill('20');
  await page.getByLabel('Width', { exact: true }).fill('50');
  await page.getByLabel('Height', { exact: true }).fill('40');
  await page.getByRole('button', { name: 'Add rectangle' }).click();
  await expect(page.locator('.mask-rects li')).toHaveCount(1);
  await expect(page.locator('.mask-status')).toContainText(/^(19|20|21)(\.\d)?% of the image is selected/);
  await axeCheck(page, 'images edit with mask editor');

  await page.fill('#image-prompt', 'replace the background with a beach');
  const [req] = await Promise.all([submitted(page), page.click('#generate-btn')]);
  const body = req.postDataJSON();
  expect(body).toMatchObject({ kind: 'edit', image_model: 'qwen-image-edit-2511', edit_mode: 'background', edit_quality: 'quality', negative_prompt: 'blurry', mask_source: 'rectangles' });
  expect(body.mask).toMatch(/^data:image\/png;base64,/);
  expect(body.mask_rects).toEqual([{ x: 0.1, y: 0.2, w: 0.5, h: 0.4 }]);
  expect(body.strength).toBeUndefined();
  const res = await req.response();
  expect(res.status()).toBe(202);
  const job = await res.json();
  // The server decoded and measured the mask; the image itself never appears in the job.
  expect(job.params.mask).toMatchObject({ source: 'rectangles', rects: [{ x: 0.1, y: 0.2, w: 0.5, h: 0.4 }] });
  expect(job.params.mask.coverage).toBeGreaterThan(0.15);
  expect(job.params.mask.coverage).toBeLessThan(0.25);
  expect(JSON.stringify(job)).not.toContain('base64');

  // Full transformation refuses a mask on Qwen.
  await modes.getByRole('button', { name: 'Full transformation' }).click();
  await page.click('#generate-btn');
  await expect(page.locator('.panel-foot .form-error')).toContainText('Full transformation changes the whole image');

  // VisionmasterPro_V3 has no instruction following: Change needs a mask.
  await page.getByLabel('Model', { exact: true }).selectOption('visionmaster-pro-v3');
  await expect(page.getByRole('group', { name: 'Edit quality' })).toBeHidden();
  await modes.getByRole('button', { name: 'Change / replace' }).click();
  await page.getByRole('button', { name: 'Clear' }).click();
  await expect(page.locator('#mask-note')).toContainText('needs a mask');
  await page.click('#generate-btn');
  await expect(page.locator('.panel-foot .form-error')).toContainText('needs a mask');
  await modes.getByRole('button', { name: 'Restyle' }).click();
  await expect(page.getByRole('slider', { name: 'Strength' })).toBeVisible();
  await page.getByRole('slider', { name: 'Strength' }).fill('0.4');
  await page.fill('#image-prompt', 'oil painting of the same scene');
  const [req2] = await Promise.all([submitted(page), page.click('#generate-btn')]);
  expect(req2.postDataJSON()).toMatchObject({ kind: 'edit', image_model: 'visionmaster-pro-v3', edit_mode: 'restyle', strength: 0.4 });
  expect((await req2.response()).status()).toBe(202);

  // Pointer painting works too (touch-style drag).
  await page.getByLabel('Model', { exact: true }).selectOption('qwen-image-edit-2511');
  await modes.getByRole('button', { name: 'Remove' }).click();
  await canvas.scrollIntoViewIfNeeded();
  const box = await canvas.boundingBox();
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.3);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.6, { steps: 6 });
  await page.mouse.up();
  await expect(page.locator('.mask-status')).toContainText('% of the image is selected');
  await page.fill('#image-prompt', 'the bicycle');
  const [req3] = await Promise.all([submitted(page), page.click('#generate-btn')]);
  expect(req3.postDataJSON()).toMatchObject({ edit_mode: 'remove', mask_source: 'painted' });
  expect((await req3.response()).status()).toBe(202);
  expect(problems).toEqual([]);
});

test('the NSFW adapter and the quality tags are only offered where they do something', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await gotoPage(page, 'images');

  // Generation: the adapter matches Qwen Image 2512 and is on by default.
  const adapter = page.getByRole('switch', { name: 'Uncensored adapter' });
  await expect(adapter).toBeChecked();
  await page.fill('#image-prompt', 'e2e adapter default on generation');
  const [gen] = await Promise.all([submitted(page), page.click('#generate-btn')]);
  expect(gen.postDataJSON()).toMatchObject({ kind: 't2i', uncensored: true });

  // Editing: the only edit adapter is a Qwen-Image LoRA, so it starts off.
  await page.getByRole('tab', { name: 'Edit' }).click();
  await expect(adapter).not.toBeChecked();
  await expect(page.locator('.switch-field', { hasText: 'Uncensored adapter' }).locator('.field-hint'))
    .toContainText('off by default for edits');
  await page.getByRole('button', { name: 'Choose from Library' }).click();
  await page.locator('dialog[open] .pick-tile').first().click();
  await page.fill('#image-prompt', 'e2e adapter default on an edit');
  const [edit] = await Promise.all([submitted(page), page.click('#generate-btn')]);
  expect(edit.postDataJSON()).toMatchObject({ kind: 'edit', uncensored: false });

  // Turning it on for the edit does not change the generation default.
  await page.locator('label.switch', { hasText: 'Uncensored adapter' }).click();
  await expect(adapter).toBeChecked();
  const [edit2] = await Promise.all([submitted(page), page.click('#generate-btn')]);
  expect(edit2.postDataJSON()).toMatchObject({ kind: 'edit', uncensored: true });
  await page.getByRole('tab', { name: 'Generate' }).click();
  await expect(adapter).toBeChecked();

  // Quality tags reach the router only on generation, so the switch is hidden on edits.
  const tags = page.getByRole('switch', { name: 'Quality tags' });
  await page.getByLabel('Model', { exact: true }).selectOption('visionmaster-pro-v3');
  await expect(tags).toBeVisible();
  await page.getByRole('tab', { name: 'Edit' }).click();
  await page.getByLabel('Model', { exact: true }).selectOption('visionmaster-pro-v3');
  await expect(tags).toBeHidden();
  await axeCheck(page, 'images edit with VisionmasterPro_V3 and no decorative switches');
  expect(problems).toEqual([]);
});
