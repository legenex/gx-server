// Call Agents (Build V3 CAL): the agent list, the versioned editor with its
// compiled-prompt preview, duplication, enabling, the Call tab (engine banner,
// secure-context callout, a refused start) and the History tab.
// The backend is the real Control Center; gx-call itself is not configured in
// the fixture, which is exactly the "node 2 not available" path the page must
// survive without a dead button.
import { expect, test } from '@playwright/test';
import { axeCheck, gotoPage, login, watchPage } from './helpers.js';

const TEMPLATE_NAME = 'IntakePilot MVA intake';

async function openAgents(page) {
  await gotoPage(page, 'call');
  await expect(page.locator('#page-call h1')).toHaveText('Call Agents');
  return page.locator('#page-call');
}

async function createTemplateAgent(page) {
  await page.getByRole('button', { name: 'New agent' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Start from')).toBeVisible();
  await dialog.getByRole('button', { name: 'Create', exact: true }).click();
  const editor = page.getByRole('dialog');
  await expect(editor.getByRole('heading', { name: `Edit ${TEMPLATE_NAME}` })).toBeVisible();
  return editor;
}

test('agents: template, editor, compiled prompt, new version and enable', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const root = await openAgents(page);
  await expect(root.getByRole('tablist', { name: 'Call Agents sections' }).getByRole('tab'))
    .toHaveText(['Agents', 'Call', 'History']);
  await expect(root.getByText('No call agents yet')).toBeVisible();
  await axeCheck(page, 'call agents empty');

  const editor = await createTemplateAgent(page);
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
  await editor.getByLabel('Name', { exact: true }).fill('Night line intake');
  await editor.getByRole('button', { name: 'Save new version' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const card = page.locator('#page-call .card', { hasText: 'Night line intake' });
  await expect(card).toBeVisible();
  await expect(card.getByText('v2')).toBeVisible();
  await expect(card.getByText('draft')).toBeVisible();

  // enable it, then duplicate it
  await page.getByRole('button', { name: 'Enable Night line intake' }).click();
  await expect(card.getByText('enabled')).toBeVisible();
  await page.getByRole('button', { name: 'Duplicate Night line intake' }).click();
  const prompt = page.getByRole('dialog');
  await expect(prompt.getByLabel('Name of the copy')).toHaveValue('Night line intake (copy)');
  await prompt.getByRole('button', { name: 'Duplicate', exact: true }).click();
  await expect(page.locator('#page-call .card', { hasText: 'Night line intake (copy)' })).toBeVisible();
  expect(problems).toEqual([]);
});

test('call tab: engine banner, secure-context callout and a refused start', async ({ page }) => {
  // gx-call has no service key in the fixture, so POST /api/call/sessions is a
  // real 503 the browser also logs; everything else must stay silent.
  const problems = watchPage(page, { allow: [/Failed to load resource/] });
  // a browser without getUserMedia must get the shared secure-context helper
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, get: () => undefined });
  });
  await login(page);
  const root = await openAgents(page);
  await expect(root.getByText('gx-call is not answering')).toBeVisible();

  await root.getByRole('tab', { name: 'Call' }).click();
  await expect(root.getByText('No agent is enabled')).toBeVisible();
  await root.getByRole('button', { name: 'Go to agents' }).click();
  await createTemplateAgent(page);
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: `Enable ${TEMPLATE_NAME}` }).click();

  await root.getByRole('tab', { name: 'Call' }).click();
  await expect(root.getByText('Microphone and camera need a secure connection')).toBeVisible();
  const start = root.getByRole('button', { name: 'Start the call' });
  await expect(start).toBeDisabled();
  await axeCheck(page, 'call tab without a microphone');
  expect(problems).toEqual([]);
});

test('call tab: a start that gx-call refuses is reported, not swallowed', async ({ page }) => {
  const problems = watchPage(page, { allow: [/Failed to load resource/] });
  await login(page);
  const root = await openAgents(page);
  await createTemplateAgent(page);
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: `Enable ${TEMPLATE_NAME}` }).click();
  await root.getByRole('tab', { name: 'Call' }).click();
  const start = root.getByRole('button', { name: 'Start the call' });
  await expect(start).toBeEnabled(); // 127.0.0.1 IS a secure context
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/call/sessions') && r.request().method() === 'POST'),
    start.click(),
  ]);
  expect(resp.status()).toBe(503);
  await expect(page.locator('#toasts')).toContainText('gx-call');
  await expect(start).toBeEnabled(); // the button comes back, it is not dead
  expect(problems).toEqual([]);
});

test('history: empty state and accessibility', async ({ page }) => {
  const problems = watchPage(page);
  await login(page);
  const root = await openAgents(page);
  await root.getByRole('tab', { name: 'History' }).click();
  await expect(root.getByText('No calls yet')).toBeVisible();
  await axeCheck(page, 'call history');
  // the tablist is fully keyboard operable
  await root.getByRole('tab', { name: 'History' }).focus();
  await page.keyboard.press('Home');
  await expect(root.getByRole('tab', { name: 'Agents' })).toHaveAttribute('aria-selected', 'true');
  expect(problems).toEqual([]);
});
