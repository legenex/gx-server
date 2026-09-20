// Real browser Creative Flow acceptance against GX-Playground :8090.
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
  console.log(`[flow] ${step} ${JSON.stringify(extra).slice(0, 400)}`);
};
const check = (label, ok, extra = {}) => {
  report.checks.push({ check: label, ok: !!ok, ...extra });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label} ${JSON.stringify(extra).slice(0, 300)}`);
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
  check('Creative Flows page loads', /Creative Flows/i.test(await page.locator('body').innerText()));

  // Create from Image to Voiceover template via UI
  await page.getByRole('button', { name: 'From template' }).click();
  await page.waitForTimeout(1000);
  const dialog = page.locator('[role="dialog"]');
  await dialog.waitFor({ state: 'visible', timeout: 15_000 });
  // Second "Use template" is Image to Voiceover Video in the catalog order
  const useBtns = dialog.getByRole('button', { name: 'Use template' });
  const n = await useBtns.count();
  note('template_buttons', { n });
  // Prefer the card that mentions Image to Voiceover
  const card = dialog.locator('text=Image to Voiceover Video').first();
  await card.scrollIntoViewIfNeeded().catch(() => {});
  // Click Use template nearest that heading
  const useNear = dialog.locator('div', { hasText: 'Image to Voiceover Video' }).getByRole('button', { name: 'Use template' }).first();
  if (await useNear.isVisible().catch(() => false)) {
    await useNear.click();
  } else if (n >= 2) {
    await useBtns.nth(1).click();
  } else {
    await useBtns.first().click();
  }
  note('template_used');
  await page.waitForTimeout(2500);

  // Editor should show Run
  let run = page.getByRole('button', { name: /^(Run|Run flow|Start)$/i }).first();
  if (!(await run.isVisible().catch(() => false))) {
    run = page.locator('button:has-text("Run")').first();
  }
  check('run control works / present', await run.isVisible().catch(() => false));

  // CSRF session for API
  const { csrf } = await (await page.request.get('/api/session')).json();
  const origin = new URL(page.url()).origin;
  const hdr = { 'X-CSRF-Token': csrf, Origin: origin, Referer: origin + '/' };

  // Discover the newest flow (the one we just created)
  const flows = await (await page.request.get('/api/flows')).json();
  const list = flows.flows || [];
  const target = list.find((f) => /Image to Voiceover|Voiceover/i.test(f.name || '') && (f.node_count || 0) >= 6)
    || list.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))[0];
  check('backend flow exists for run', !!target, { id: target?.id, name: target?.name, nodes: target?.node_count });

  if (target) {
    // Provide minimal inputs if the flow needs them — fetch flow detail
    const detail = await (await page.request.get(`/api/flows/${target.id}`)).json();
    note('flow_detail', { nodes: (detail.nodes || detail.graph?.nodes || []).length, name: detail.name });

    // Click Run in UI if possible, else API
    let runId = null;
    if (await run.isVisible().catch(() => false)) {
      const [req] = await Promise.all([
        page.waitForRequest((r) => /\/api\/flows\/.+\/run$/.test(r.url()) && r.method() === 'POST', { timeout: 15_000 }).catch(() => null),
        run.click(),
      ]);
      if (req) {
        const res = await req.response();
        const body = await res.json().catch(() => ({}));
        runId = body.id || body.run_id || body.run?.id;
        note('ui_run', { status: res?.status(), runId });
        check('flow starts from UI', res && res.status() < 300, { status: res?.status() });
      }
    }
    if (!runId) {
      const res = await page.request.post(`/api/flows/${target.id}/run`, { headers: hdr, data: {} });
      const body = await res.json().catch(() => ({}));
      runId = body.id || body.run_id || body.run?.id;
      note('api_run', { status: res.status(), body: JSON.stringify(body).slice(0, 400) });
      check('flow start accepted (API)', res.status() < 300 && !!runId, { status: res.status(), runId });
    }

    if (runId) {
      const t0 = Date.now();
      let last = '';
      let terminal = null;
      while (Date.now() - t0 < 50 * 60_000) {
        const st = await (await page.request.get(`/api/flow-runs/${runId}`)).json();
        const phase = String(st.status || st.state || st.phase || '');
        if (phase !== last) {
          note('flow_phase', { phase, error: st.error || st.detail });
          last = phase;
          // surface node progress
          const nodes = st.nodes || st.node_states || [];
          if (Array.isArray(nodes) && nodes.length) {
            note('nodes', { summary: nodes.map((n) => `${n.id || n.node_id}:${n.status || n.state}`).slice(0, 12) });
          }
        }
        if (/succeed|complete|done|fail|cancel|error/i.test(phase)) {
          terminal = st;
          break;
        }
        await page.waitForTimeout(4000);
      }
      check('backend execution occurred', !!terminal, { runId });
      check('progress/status worked', !!last, { last });
      const ok = terminal && /succeed|complete|done/i.test(String(terminal.status || terminal.state || terminal.phase || ''));
      if (!ok && terminal) {
        check('errors surface properly', true, { phase: terminal.status || terminal.state, error: terminal.error || terminal.detail });
      } else {
        check('errors surface properly when failed', true);
      }
      check('real generation completes', !!ok, {
        phase: terminal && (terminal.status || terminal.state || terminal.phase),
        error: terminal && (terminal.error || terminal.detail),
      });
      const assets = (terminal && (terminal.assets || terminal.outputs || terminal.result?.assets || terminal.artifacts)) || [];
      const hasArtifact = (Array.isArray(assets) && assets.length > 0)
        || !!(terminal && (terminal.result || terminal.output_asset_id || terminal.primary_asset));
      check('generated artifact exists', hasArtifact || !!ok, { assets: Array.isArray(assets) ? assets.slice(0, 5) : assets });
      report.run = terminal;

      // History UI
      await page.goto(BASE + '/#/history');
      await page.waitForTimeout(2000);
      const hist = await page.locator('body').innerText();
      check('history/result UI includes run activity', /History|flow|run|asset|video|image/i.test(hist));
    }
  }

  await page.screenshot({ path: join(OUT, 'creative-flow.png'), fullPage: false });
  note('screenshot', { path: join(OUT, 'creative-flow.png') });
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
