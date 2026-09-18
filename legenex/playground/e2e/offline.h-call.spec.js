// Call Agents (Build V3 CAL): the agent list, the versioned editor with its
// compiled-prompt preview, duplication, enabling, the Call tab (engine banner,
// secure-context callout, a refused start) and the History tab.
//
// The backend is the real Control Center. gx-call itself has no service key in
// the fixture, which is exactly the "node 2 is not available" path the page has
// to survive: a real 503, reported to the user, with no dead button.
//
// The tests share one backend, so they run in order and build on each other.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, watchPage } from './helpers.js';

test.describe.configure({ mode: 'serial' });

// The default is the general agent; the motor vehicle accident intake is one
// optional template, picked explicitly in the "Start from" list.
const GENERAL_NAME = 'New call agent';
const TEMPLATE_NAME = 'Motor vehicle accident intake';
const EDITED_NAME = 'Night line intake';
// The browser logs every 4xx/5xx response; several of them are deliberate here.
const HTTP_NOISE = [/Failed to load resource/];

async function openCall(page) {
  await gotoPage(page, 'call');
  await expect(page.locator('#page-call h1')).toHaveText('Call Agents');
  return page.locator('#page-call');
}

async function createAgent(page, { template } = {}) {
  await page.getByRole('button', { name: 'New agent' }).click();
  const dialog = page.getByRole('dialog');
  const startFrom = dialog.getByLabel('Start from');
  await expect(startFrom).toBeVisible();
  // the general voice agent is the first option and therefore the default
  await expect(startFrom.locator('option').first()).toHaveText('General voice agent');
  await expect(startFrom).toHaveValue('general');
  if (template) await startFrom.selectOption({ label: template });
  await dialog.getByRole('button', { name: 'Create', exact: true }).click();
  const editor = page.getByRole('dialog');
  await expect(editor.getByRole('heading', { name: `Edit ${template || GENERAL_NAME}` })).toBeVisible();
  return editor;
}

test('empty state: no agents, and the Call tab says so', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const root = await openCall(page);
  await expect(root.getByRole('tablist', { name: 'Call Agents sections' }).getByRole('tab'))
    .toHaveText(['Agents', 'Call', 'History']);
  await expect(root.getByText('No call agents yet')).toBeVisible();
  // the empty state offers a voice agent, not one vendor's intake product
  await expect(root.getByRole('button', { name: 'Create a voice agent' })).toBeVisible();
  await expect(root.getByRole('button', { name: 'Create the IntakePilot template' })).toHaveCount(0);
  await axeCheck(page, 'call agents empty');
  await root.getByRole('tab', { name: 'Call' }).click();
  await expect(root.getByText('No agent is enabled')).toBeVisible();
  await root.getByRole('button', { name: 'Go to agents' }).click();
  await expect(root.getByText('No call agents yet')).toBeVisible();
  await root.getByRole('tab', { name: 'History' }).click();
  await expect(root.getByText('No calls yet')).toBeVisible();
  await axeCheck(page, 'call history empty');
  expect(problems).toEqual([]);
});

test('agents: template, editor, compiled prompt, new version, enable and duplicate', async ({ page }) => {
  const problems = watchPage(page, { allow: HTTP_NOISE }); // one preview is deliberately invalid
  await login(page);
  await openCall(page);

  const editor = await createAgent(page, { template: TEMPLATE_NAME });
  // the editor shows the real, saved configuration
  await expect(editor.getByLabel('Name', { exact: true })).toHaveValue(TEMPLATE_NAME);
  await expect(editor.getByLabel('Role and task')).toContainText('intake specialist');
  await expect(editor.getByRole('group', { name: 'Tools the agent may call' })
    .getByRole('button', { name: 'Update intake fields' })).toHaveAttribute('aria-pressed', 'true');
  await expect(editor.getByRole('group', { name: 'Required intake fields' })
    .getByRole('button', { name: 'caller_name', exact: true })).toHaveAttribute('aria-pressed', 'true');

  // the compiled prompt is what the model would really receive
  await editor.getByRole('button', { name: 'Check and preview' }).click();
  await expect(editor.locator('.field-hint', { hasText: 'Valid ·' })).toBeVisible();
  await expect(editor.locator('pre.code-wrap')).toContainText('caller_name');
  await axeCheck(page, 'call agent editor');

  // an invalid change is refused by the server and explained in place: the
  // template already uses the maximum of five tools, so a sixth is too many
  const tools = editor.getByRole('group', { name: 'Tools the agent may call' });
  await tools.getByRole('button', { name: 'Send webhook' }).click();
  await editor.getByRole('button', { name: 'Check and preview' }).click();
  await expect(editor.locator('.field-hint', { hasText: 'at most' })).toBeVisible();
  await tools.getByRole('button', { name: 'Send webhook' }).click();

  // a real edit saves a new immutable version
  await editor.getByLabel('Name', { exact: true }).fill(EDITED_NAME);
  await editor.getByRole('button', { name: 'Save new version' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const card = page.locator('#page-call .card', { hasText: EDITED_NAME }).first();
  await expect(card).toBeVisible();
  await expect(card.getByText('v2', { exact: true })).toBeVisible();
  await expect(card.getByText('draft', { exact: true })).toBeVisible();

  // enable it, then duplicate it
  await page.getByRole('button', { name: `Enable ${EDITED_NAME}` }).click();
  await expect(card.getByText('enabled', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: `Duplicate ${EDITED_NAME}` }).click();
  const prompt = page.getByRole('dialog');
  await expect(prompt.getByLabel('Name of the copy')).toHaveValue(`${EDITED_NAME} (copy)`);
  await prompt.getByRole('button', { name: 'Duplicate', exact: true }).click();
  await expect(page.locator('#page-call .card', { hasText: `${EDITED_NAME} (copy)` })).toBeVisible();
  expect(problems).toEqual([]);
});

test('call tab: the engine banner and the shared secure-context callout', async ({ page }) => {
  const problems = watchPage(page);
  // a browser without getUserMedia must get the shared secure-context helper
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, get: () => undefined });
  });
  await login(page);
  const root = await openCall(page);
  await expect(root.getByText('gx-call is not answering')).toBeVisible();
  await root.getByRole('tab', { name: 'Call' }).click();
  await expect(root.getByText('Microphone and camera need a secure connection')).toBeVisible();
  await expect(root.getByRole('link', { name: 'HTTPS setup help' })).toBeVisible();
  await expect(root.getByRole('button', { name: 'Start the call' })).toBeDisabled();
  await axeCheck(page, 'call tab without a microphone');
  expect(problems).toEqual([]);
});

test('call tab: a start that gx-call refuses is reported, and lands in the history', async ({ page }) => {
  const problems = watchPage(page, { allow: HTTP_NOISE }); // the 503 from gx-call
  await login(page);
  const root = await openCall(page);
  await root.getByRole('tab', { name: 'Call' }).click();
  const start = root.getByRole('button', { name: 'Start the call' });
  await expect(start).toBeEnabled(); // 127.0.0.1 IS a secure context
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/call/sessions') && r.request().method() === 'POST'),
    start.click(),
  ]);
  expect(resp.status()).toBe(503);
  await expect(page.locator('#toasts')).toContainText('not configured');
  await expect(start).toBeEnabled(); // the button comes back, it is not dead

  // the refused call is recorded and can be inspected
  await root.getByRole('tab', { name: 'History' }).click();
  const row = root.locator('.log-row').first();
  await expect(row.getByText('failed', { exact: true })).toBeVisible();
  await axeCheck(page, 'call history with one call');
  await row.getByRole('button', { name: 'Open' }).click();
  const drawer = page.getByRole('dialog');
  await expect(drawer.getByText('The call failed')).toBeVisible();
  await expect(drawer.getByText('not_configured')).toBeVisible();
  await axeCheck(page, 'call detail drawer');
  await drawer.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(problems).toEqual([]);
});

test('the section tabs are fully keyboard operable', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const root = await openCall(page);
  await root.getByRole('tab', { name: 'Agents' }).focus();
  await page.keyboard.press('End');
  await expect(root.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Home');
  await expect(root.getByRole('tab', { name: 'Agents' })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('ArrowRight');
  await expect(root.getByRole('tab', { name: 'Call' })).toHaveAttribute('aria-selected', 'true');
  expect(problems).toEqual([]);
});
