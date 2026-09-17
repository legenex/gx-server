// Creative Flows (FLO): the React/@xyflow island inside the Playground shell,
// against the real Control Center backend of the offline fixture.
//
// Covered here: creating a flow, adding nodes from the library, connecting
// them, refusing an incompatible connection, configuring a node, autosave,
// surviving a reload, the outline view, templates and error handling.
import { expect, test } from '@playwright/test';

import { axeCheck, gotoPage, login, noHorizontalOverflow, watchPage } from './helpers.js';

test.describe.configure({ mode: 'serial' });

const editor = '.gxf-app';
const nodeOf = (type) => `.gxf-node[data-node-type="${type}"]`;

/** The node library filters as you type; adding puts the node on the canvas. */
async function addNode(page, type, label) {
  await page.fill('[data-palette-search]', label);
  const item = page.locator(`.gxf-palette-item[data-node-type="${type}"]`);
  await expect(item).toBeVisible();
  await item.click();
  await expect(page.locator(nodeOf(type)).first()).toBeVisible();
}

async function openFlows(page) {
  await gotoPage(page, 'flows');
  await expect(page.locator('#page-flows h1')).toHaveText('Creative Flows');
}

test('creates a flow and keeps it in the URL', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await openFlows(page);

  await page.getByRole('button', { name: 'New flow' }).click();
  await expect(page.locator(editor)).toBeVisible();
  const flowId = await page.locator(editor).getAttribute('data-flow-id');
  expect(flowId).toMatch(/^flow_[0-9a-f]{24}$/);
  expect(page.url()).toContain(`flow=${flowId}`);
  await expect(page.getByLabel('Flow name')).toHaveValue('Untitled flow');
  await expect(page.locator('[data-save-state]')).toHaveAttribute('data-save-state', 'saved');
  await expect(page.locator('.gxf-statusbar')).toContainText('0 node(s), 0 connection(s)');

  expect(problems, problems.join('\n')).toEqual([]);
});

test('adds nodes, connects compatible ports and refuses incompatible ones', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await openFlows(page);
  await page.getByRole('button', { name: 'New flow' }).click();
  await expect(page.locator(editor)).toBeVisible();

  await addNode(page, 'text.input', 'Text');
  await addNode(page, 'image.generate', 'Generate Image');
  await addNode(page, 'sound.upload', 'Audio Upload');
  await expect(page.locator('.gxf-statusbar')).toContainText('3 node(s), 0 connection(s)');

  // Connect through the outline, which is the keyboard/AT equivalent of dragging.
  await page.getByRole('button', { name: 'Outline' }).click();
  const textItem = page.locator('.gxf-outline-item', { hasText: 'Text' }).first();
  await textItem.getByRole('button', { name: /^Connect/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Only compatible inputs are listed');
  await dialog.getByRole('radio', { name: /Prompt/ }).first().check();
  await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
  await expect(page.locator('.gxf-statusbar')).toContainText('1 connection(s)');

  // The audio output has no compatible free input in this flow: the editor
  // offers none rather than letting an impossible edge be drawn.
  const audioItem = page.locator('.gxf-outline-item', { hasText: 'Audio Upload' }).first();
  await audioItem.getByRole('button', { name: /^Connect/ }).click();
  const refusal = page.getByRole('dialog');
  await expect(refusal).toContainText('No compatible input is free');
  await expect(refusal.getByRole('radio')).toHaveCount(0);
  await refusal.getByRole('button', { name: 'Cancel' }).click();

  expect(problems, problems.join('\n')).toEqual([]);
});

test('refuses an incompatible connection dragged on the canvas', async ({ page }) => {
  await login(page);
  await openFlows(page);
  await page.getByRole('button', { name: 'New flow' }).click();
  await expect(page.locator(editor)).toBeVisible();

  await addNode(page, 'sound.upload', 'Audio Upload');
  await addNode(page, 'image.generate', 'Generate Image');
  // The inspector opens on the node that was just added; it would cover the canvas.
  await page.getByRole('button', { name: 'Close the inspector' }).click();
  await page.getByRole('button', { name: 'Nodes' }).click();          // collapse the library too
  await page.getByRole('button', { name: 'Fit the flow in view' }).click();

  const from = page.locator(`${nodeOf('sound.upload')} .gxf-port-out[data-port="audio"] .gxf-handle`).first();
  const to = page.locator(`${nodeOf('image.generate')} .gxf-port-in[data-port="prompt"] .gxf-handle`).first();
  const a = await from.boundingBox();
  const b = await to.boundingBox();
  expect(a && b, 'both handles are on screen').toBeTruthy();
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move((a.x + b.x) / 2, (a.y + b.y) / 2, { steps: 8 });
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 8 });
  await page.mouse.up();

  await expect(page.locator('.toast-msg').last()).toContainText(/accepts text|conversion node/i);
  await expect(page.locator('.gxf-statusbar')).toContainText('0 connection(s)');
});

test('configures a node, autosaves and survives a reload', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await openFlows(page);
  await page.getByRole('button', { name: 'New flow' }).click();
  await expect(page.locator(editor)).toBeVisible();
  const flowId = await page.locator(editor).getAttribute('data-flow-id');

  await page.getByLabel('Flow name').fill('E2E lighthouse flow');
  await addNode(page, 'text.input', 'Text');

  // Adding a node opens the Inspector on its settings.
  const inspector = page.locator('.gxf-inspector');
  await expect(inspector).toBeVisible();
  await inspector.getByLabel('Text', { exact: true }).fill('a lighthouse at dawn, long exposure');
  await expect(page.locator('[data-save-state]')).toHaveAttribute('data-save-state', 'saved', { timeout: 15_000 });

  await page.reload();
  await expect(page.locator(editor)).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(editor)).toHaveAttribute('data-flow-id', flowId);
  await expect(page.getByLabel('Flow name')).toHaveValue('E2E lighthouse flow');
  await expect(page.locator(nodeOf('text.input'))).toContainText('Text');
  await page.locator(`${nodeOf('text.input')} button[aria-label^="Inspect"]`).click();
  await expect(page.locator('.gxf-inspector').getByLabel('Text', { exact: true }))
    .toHaveValue('a lighthouse at dawn, long exposure');

  // Back to the browser: the flow is listed with its new name.
  await page.getByRole('button', { name: 'Back to all flows' }).click();
  await expect(page.locator('.gxf-flow-card', { hasText: 'E2E lighthouse flow' })).toBeVisible();

  expect(problems, problems.join('\n')).toEqual([]);
});

test('creates a flow from a built-in template', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  await openFlows(page);

  await page.getByRole('button', { name: 'From template' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.locator('.gxf-template').first()).toBeVisible({ timeout: 20_000 });
  const count = await dialog.locator('.gxf-template').count();
  expect(count).toBeGreaterThan(0);
  await dialog.locator('.gxf-template').first().getByRole('button', { name: 'Use template' }).click();

  await expect(page.locator(editor)).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('.gxf-node').first()).toBeVisible();
  await expect(page.locator('.gxf-statusbar')).not.toContainText('0 node(s)');

  expect(problems, problems.join('\n')).toEqual([]);
});

test('outline view lists nodes in execution order with their ports', async ({ page }) => {
  await login(page);
  await openFlows(page);
  await page.getByRole('button', { name: 'New flow' }).click();
  await addNode(page, 'text.input', 'Text');
  await addNode(page, 'image.generate', 'Generate Image');
  await page.getByRole('button', { name: 'Outline' }).click();

  const items = page.locator('.gxf-outline-item');
  await expect(items).toHaveCount(2);
  await expect(items.first()).toContainText('Text');
  await expect(items.nth(1)).toContainText('Prompt');
  await expect(items.nth(1)).toContainText('not connected');
  // Every canvas action has an equivalent here.
  for (const name of ['Inspect', 'Run', 'Bypass', 'Lock', 'Delete']) {
    await expect(items.first().getByRole('button', { name: new RegExp(`^${name}`) }).first()).toBeVisible();
  }
  await noHorizontalOverflow(page);
  await axeCheck(page, 'flows outline');
});

test('reports backend failures instead of showing an empty page', async ({ page }) => {
  await login(page);

  await page.route('**/api/flows?*', (route) => route.fulfill({
    status: 503, contentType: 'application/json',
    body: JSON.stringify({ error: { message: 'the flow store is unavailable', code: 'unavailable' } }),
  }));
  await page.route('**/api/flows', (route) => route.fulfill({
    status: 503, contentType: 'application/json',
    body: JSON.stringify({ error: { message: 'the flow store is unavailable', code: 'unavailable' } }),
  }));
  await gotoPage(page, 'flows');
  await expect(page.locator('#page-flows')).toContainText('Flows could not be loaded');
  await expect(page.locator('#page-flows')).toContainText('the flow store is unavailable');

  // Recovering does not need a page reload.
  await page.unroute('**/api/flows?*');
  await page.unroute('**/api/flows');
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.locator('#page-flows')).not.toContainText('Flows could not be loaded');
});

test('works at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await openFlows(page);
  await page.getByRole('button', { name: 'New flow' }).click();
  await expect(page.locator(editor)).toBeVisible();
  await page.getByRole('button', { name: 'Nodes' }).click();   // the library is an overlay here
  await expect(page.locator('.gxf-palette')).toBeVisible();
  await noHorizontalOverflow(page);
});
