// Real browser Creative Flow acceptance against GX-Playground :8090.
// Runs one production flow end-to-end (Image→Voiceover template duplicate),
// plus verifies page load / controls / history.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const require = createRequire(new URL('../playground/package.json', import.meta.url));
const { chromium } = require('playwright');

const BASE = process.env.GX_PG_URL || 'http://127.0.0.1:8090';
const PASS = readFileSync('/srv/projects/gx-cluster/secrets/control-ui/acceptance-password', 'utf8').trim();
const OUT = process.env.GX_EVIDENCE_DIR || `/srv/logs/acceptance/creative-flow-${Date.now()}`;
mkdirSync(OUT, { recursive: true });

const report = { steps: [], checks: [], url: BASE };
const note = (step, extra = {}) => {
  report.steps.push({ at: new Date().toISOString(), step, ...extra });
  console.log(`[flow] ${step} ${JSON.stringify(extra).slice(0, 300)}`);
};
const check = (label, ok, extra = {}) => {
  report.checks.push({ check: label, ok: !!ok, ...extra });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label} ${JSON.stringify(extra).slice(0, 200)}`);
};

const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.fill('#login-user', 'acceptance');
  await page.fill('#login-pass', PASS);
  await page.click('#login-submit');
  await page.waitForSelector('#app-view', { timeout: 30_000 });
  note('signed_in');
  check('page loads and signs in', true);

  await page.goto(BASE + '/#/flows');
  await page.waitForSelector('#page-flows h1', { timeout: 30_000 });
  await expectText(page, 'Creative Flows');
  note('flows_page');
  check('Creative Flows page loads', true);

  // Prefer a lightweight template: Image to Voiceover Video
  const fromTpl = page.getByRole('button', { name: 'From template' });
  if (await fromTpl.isVisible().catch(() => false)) {
    await fromTpl.click();
    note('opened_templates');
    const pick = page.getByRole('button', { name: /Image to Voiceover/i }).first()
      .or(page.locator('button:has-text("Image to Voiceover")').first());
    if (await pick.isVisible().catch(() => false)) {
      await pick.click();
      note('picked_template');
    } else {
      // dialog list
      const opt = page.locator('text=Image to Voiceover').first();
      await opt.click();
      const create = page.getByRole('button', { name: /create|use|open/i }).first();
      if (await create.isVisible().catch(() => false)) await create.click();
    }
  } else {
    // Duplicate existing ACCEPT flow
    const card = page.locator('text=ACCEPT Image to Voiceover Video').first();
    await card.click();
  }

  // Wait for editor
  await page.waitForTimeout(2000);
  const runBtn = page.getByRole('button', { name: /^(Run|Start|Execute)$/i }).first()
    .or(page.locator('button:has-text("Run")').first());
  check('run control present', await runBtn.isVisible().catch(() => false));

  // If we landed on list, open first runnable flow
  if (!(await runBtn.isVisible().catch(() => false))) {
    const open = page.getByRole('button', { name: 'Open' }).first();
    if (await open.isVisible().catch(() => false)) {
      await open.click();
      await page.waitForTimeout(2000);
    }
  }

  const run = page.getByRole('button', { name: /^(Run|Start|Execute)$/i }).first()
    .or(page.locator('#flow-run, button:has-text("Run flow"), button:has-text("Run")').first());
  if (!(await run.isVisible().catch(() => false))) {
    // API fallback: list flows and POST run on Image to Voiceover
    note('ui_run_missing_using_api');
    const sess = await page.request.get('/api/session');
    const { csrf } = await sess.json();
    const origin = new URL(page.url()).origin;
    const flows = await (await page.request.get('/api/flows')).json();
    const target = (flows.flows || []).find((f) => /Image to Voiceover/i.test(f.name || ''))
      || (flows.flows || []).find((f) => (f.node_count || 0) >= 6);
    check('found flow to run', !!target, { id: target?.id, name: target?.name });
    if (target) {
      const res = await page.request.post(`/api/flows/${target.id}/run`, {
        headers: { 'X-CSRF-Token': csrf, Origin: origin, Referer: origin + '/' },
        data: {},
      });
      const body = await res.json().catch(() => ({}));
      note('flow_run_submitted', { status: res.status(), body });
      check('flow start accepted', res.status() < 300, { status: res.status(), body });
      const runId = body.id || body.run_id || body.run?.id;
      if (runId) {
        const t0 = Date.now();
        let last = '';
        while (Date.now() - t0 < 45 * 60_000) {
          const st = await (await page.request.get(`/api/flow-runs/${runId}`)).json();
          const phase = st.status || st.state || st.phase;
          if (phase !== last) {
            note('flow_phase', { phase, detail: st.detail || st.error });
            last = phase;
          }
          if (['succeeded', 'completed', 'done', 'failed', 'cancelled', 'error'].includes(String(phase).toLowerCase())) {
            check('flow completed successfully', /succeed|complete|done/i.test(String(phase)), { phase, runId });
            report.run = st;
            // artifacts
            const assets = st.assets || st.outputs || st.result?.assets || [];
            check('generated artifact referenced', Array.isArray(assets) ? assets.length > 0 : !!st.result, { assets });
            break;
          }
          await page.waitForTimeout(5000);
        }
      }
    }
  } else {
    await run.click();
    note('clicked_run');
    check('flow start clicked', true);
    // poll UI for completion
    const t0 = Date.now();
    let done = false;
    while (Date.now() - t0 < 45 * 60_000) {
      const text = await page.locator('body').innerText();
      if (/failed|error/i.test(text) && /node|flow/i.test(text)) {
        note('possible_error', { snippet: text.slice(-400) });
      }
      if (/succeeded|completed|done/i.test(text) && /run|flow/i.test(text)) {
        done = true;
        break;
      }
      await page.waitForTimeout(5000);
    }
    check('flow run reached a terminal success state in UI', done);
  }

  // History page should list something
  await page.goto(BASE + '/#/history');
  await page.waitForTimeout(2000);
  const hist = await page.locator('body').innerText();
  check('history page loads', /History/i.test(hist));
  await page.screenshot({ path: join(OUT, 'creative-flow.png') });
  note('screenshot');
} catch (err) {
  check('driver completed without exception', false, { error: String(err?.stack || err).slice(0, 800) });
  report.error = String(err?.stack || err).slice(0, 1200);
} finally {
  await browser.close();
}

writeFileSync(join(OUT, 'creative-flow-acceptance.json'), JSON.stringify(report, null, 2));
const failed = report.checks.filter((c) => !c.ok).length;
console.log(JSON.stringify({ out: OUT, failed, total: report.checks.length }));
process.exit(failed ? 1 : 0);

async function expectText(page, t) {
  await page.waitForFunction((s) => document.body.innerText.includes(s), t, { timeout: 30_000 });
}
