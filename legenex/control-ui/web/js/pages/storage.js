// STORAGE & CLEANUP (D-037): both nodes. The browser only sends candidate
// ids from the latest scan; the node that owns the files re-checks every
// item before anything is removed. Protected items cannot be selected.
import { api } from '../api.js';
import {
  h, clear, card, kv, table, toast, errorBox, confirmDialog, bytes, meter, ago, num,
} from '../dom.js';

let root;
let ctxRef;
let data = null;
const selected = new Set();
let showProtected = false;

const CATEGORY_LABEL = {
  models: 'Models', docker_images: 'Docker images', docker_build_cache: 'Docker build cache',
  docker_volumes: 'Docker volumes', docker_containers: 'Docker containers', projects: 'Projects',
  caches: 'Caches', logs: 'Logs', temporary_uploads: 'Temporary uploads', generated_media: 'Generated media',
  staging: 'Staging', other: 'Other (system, home, packages)',
};
const HEALTH_CLASS = { ok: 'ok', watch: 'warn', warn: 'warn', crit: 'crit', unknown: 'unknown' };

function nodeCard(key, ov, scanNode) {
  const hlt = ov.health || {};
  const used = ov.used || 0;
  const total = ov.total || 0;
  const cats = (scanNode && scanNode.usage && scanNode.usage.categories) || null;
  return h('section', { class: `card storage-node level-${HEALTH_CLASS[hlt.level] || 'unknown'}`, 'aria-label': `${ov.name} disk` },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, ov.name),
      h('span', { class: `badge badge-${HEALTH_CLASS[hlt.level] || 'unknown'}`, 'data-health': key }, hlt.label || 'UNKNOWN')),
    h('div', { class: 'metric' },
      h('div', { class: 'metric-head' }, h('span', {}, 'Used'), h('strong', {}, `${bytes(used)} of ${bytes(total)} (${num(ov.percent, 0)} %)`)),
      meter(used, total || 1, { label: `${ov.name} disk used`, warnAt: 85, critAt: 97 })),
    kv([['Total', bytes(total)], ['Used', bytes(used)], ['Free', h('strong', { 'data-free': key }, bytes(ov.free))],
      ['Health', hlt.reason || '—']]),
    cats ? table(['Category', 'Size'], Object.entries(cats).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1])
      .map(([k, v]) => [CATEGORY_LABEL[k] || k, bytes(v)]), { caption: `${ov.name} usage by category` })
      : h('p', { class: 'muted small' }, 'Run a scan for the breakdown by category.'));
}

function candidatesOf(cls) {
  const out = [];
  for (const node of ['node1', 'node2']) {
    const r = ((data.scan || {}).result || {})[node];
    for (const c of (r && r.candidates) || []) if (c.class === cls) out.push(c);
  }
  return out.sort((a, b) => b.bytes - a.bytes);
}

function selectionBytes() {
  let total = 0;
  for (const cls of ['safe', 'review']) for (const c of candidatesOf(cls)) if (selected.has(c.id)) total += c.bytes;
  return total;
}

function candidateTable(cls) {
  const items = candidatesOf(cls);
  const nodeName = (n) => (n === 'node1' ? 'gx10-01' : 'gx10-02');
  const rows = items.map((c) => {
    const box = cls === 'protected' ? null : h('input', {
      type: 'checkbox', 'aria-label': `Select ${c.name} on ${nodeName(c.node)}`, checked: selected.has(c.id),
      'data-candidate': c.id,
      onchange: (ev) => { if (ev.target.checked) selected.add(c.id); else selected.delete(c.id); updateBar(); },
    });
    return [box || '', nodeName(c.node), h('span', {}, h('strong', {}, c.name), h('br'), h('code', { class: 'small' }, c.target)),
      bytes(c.bytes), c.reason, cls === 'safe' ? (c.mtime ? ago(c.mtime) : '—') : (c.consequence || '—')];
  });
  const heads = ['', 'Node', 'Item', 'Size', cls === 'protected' ? 'Why it is protected' : 'Why',
    cls === 'safe' ? 'Last modified' : 'Consequence'];
  return table(heads, rows, { caption: `${cls} items`, empty: 'None.' });
}

let barEl;
function updateBar() {
  if (!barEl) return;
  const review = candidatesOf('review').filter((c) => selected.has(c.id)).length;
  clear(barEl).append(
    h('span', {}, `${selected.size} selected · ${bytes(selectionBytes())}`),
    h('button', { type: 'button', class: 'btn btn-primary', id: 'clean-selected', disabled: !selected.size, onclick: cleanSelected },
      review ? 'Clean selected (includes REVIEW)' : 'Clean selected'),
    h('button', { type: 'button', class: 'btn btn-ghost', disabled: !selected.size, onclick: () => { selected.clear(); render(); } }, 'Clear selection'));
}

async function cleanSelected() {
  const ids = [...selected];
  let plan;
  try { plan = await api.post('/api/storage/plan', { ids }); } catch (e) { toast(e.message, 'crit'); return; }
  const reviewItems = plan.items.filter((c) => c.class === 'review');
  if (plan.maintenance_required && !plan.maintenance) {
    await confirmDialog({
      title: 'Maintenance mode needed',
      body: h('div', {}, h('p', {}, 'REVIEW items are only removed while the cluster is in Maintenance mode, so nothing can start using them during the cleanup.'),
        h('p', {}, h('a', { href: '#/resources' }, 'Open Resource Control'), ' and choose Enter Maintenance, then come back.')),
      okLabel: 'OK', danger: false,
    });
    return;
  }
  const body = h('div', {},
    h('p', {}, `Dry run: ${plan.items.length} item(s), ${bytes(plan.bytes)} expected to be freed.`),
    h('ul', {}, plan.items.slice(0, 20).map((c) => h('li', {}, `${c.node === 'node1' ? 'gx10-01' : 'gx10-02'}: ${c.name} (${bytes(c.bytes)})`
      + (c.class === 'review' ? ` — ${c.consequence}` : '')))),
    plan.items.length > 20 ? h('p', { class: 'small' }, `… and ${plan.items.length - 20} more`) : null,
    reviewItems.length ? h('p', { class: 'callout callout-danger' }, `${reviewItems.length} REVIEW item(s) are included. Read the consequences above.`) : null,
    h('p', { class: 'small muted' }, 'Each item is checked again on its node right before it is removed.'));
  const res = await confirmDialog({
    title: 'Clean the selected items?', body, okLabel: `Clean ${bytes(plan.bytes)}`,
    phrase: reviewItems.length ? 'CLEAN' : null,
  });
  if (!res.ok) return;
  try {
    const out = await api.post('/api/storage/clean', { ids, allow_review: reviewItems.length > 0, confirm: reviewItems.length ? true : undefined });
    selected.clear();
    toast(`Freed ${bytes(out.freed_bytes)} (expected ${bytes(out.dry_run_bytes)}); ${out.refused.length} refused`, out.refused.length ? 'warn' : 'ok', 10000);
    await startScan(true);
  } catch (e) { toast(e.message, 'crit', 10000); }
}

async function startScan(quiet) {
  try {
    await api.post('/api/storage/scan', {});
    if (!quiet) toast('Scanning both nodes…', 'ok', 3000);
    ctxRef.refreshNow();
  } catch (e) { toast(e.message, 'crit'); }
}

function render() {
  if (!data) return;
  const scan = data.scan || {};
  const result = scan.result || {};
  const wrap = h('div', { class: 'stack' });
  wrap.append(h('div', { class: 'grid grid-2' },
    nodeCard('node1', data.overview.node1 || {}, result.node1),
    nodeCard('node2', data.overview.node2 || {}, result.node2)));
  const lib = data.library || {};
  wrap.append(card('Generated media (Library, gx10-01)',
    kv(Object.entries(lib).map(([t, v]) => [t, `${v.count} item(s), ${bytes(v.bytes)}`])),
    h('p', { class: 'small muted' }, 'Generated images, video and music are user assets, not cache: they are never removed by storage cleanup. Manage them in ',
      h('a', { href: data.playground_url || '#', target: '_blank', rel: 'noopener' }, 'GX-Playground > Library'), '.')));
  const progress = scan.progress || {};
  const scanning = scan.state === 'scanning';
  wrap.append(h('section', { class: 'card', 'aria-label': 'Scan' },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, 'Scan'),
      h('span', { class: `badge badge-${scanning ? 'warn' : scan.state === 'done' ? 'ok' : 'unknown'}`, id: 'scan-state', role: 'status' },
        scanning ? 'scanning…' : scan.state || 'not scanned yet')),
    h('p', {}, scanning
      ? `gx10-01: ${progress.node1 || '…'} · gx10-02: ${progress.node2 || '…'} · ${num(scan.elapsed_seconds, 0)} s`
      : scan.finished ? `Last scan ${ago(scan.finished)} (${num(scan.elapsed_seconds, 0)} s).${scan.error ? ` Problem: ${scan.error}` : ''}`
        : 'The scan reads disk usage, model directories, Docker objects, caches, logs and uploads on both nodes, and cross-checks the model registry, bindings, rollbacks, running containers, active downloads and the Library.'),
    h('div', { class: 'btn-row' },
      h('button', { type: 'button', class: 'btn btn-primary', id: 'scan-storage', disabled: scanning, onclick: () => startScan(false) },
        scanning ? 'Scanning…' : 'Scan storage'),
      h('a', { class: 'btn btn-ghost', href: '#/resources' }, data.scan.maintenance ? 'Maintenance is ON (Resource Control)' : 'Enter Maintenance (Resource Control)')),
    scanning ? h('div', { class: 'indeterminate', role: 'progressbar', 'aria-label': 'Scanning' }) : null));

  if (scan.state === 'done' || scan.state === 'partial') {
    barEl = h('div', { class: 'selection-bar', role: 'region', 'aria-label': 'Selection' });
    const safeIds = candidatesOf('safe').map((c) => c.id);
    wrap.append(h('section', { class: 'card', 'aria-label': 'Safe to clean' },
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, 'SAFE TO CLEAN'),
        h('button', { type: 'button', class: 'btn btn-sm', id: 'select-safe', disabled: !safeIds.length,
          onclick: () => { safeIds.forEach((i) => selected.add(i)); render(); } }, 'Select all safe items')),
      h('p', { class: 'small muted' }, 'Temporary data, rotated logs, download caches and the Docker build cache that no running, referenced or active workload uses.'),
      candidateTable('safe')));
    wrap.append(h('section', { class: 'card', 'aria-label': 'Review' },
      h('h2', { class: 'card-title' }, 'REVIEW'),
      h('p', { class: 'small muted' }, 'Possibly useful. Never removed automatically; each one needs your selection, a typed confirmation and Maintenance mode.'),
      candidateTable('review')));
    wrap.append(barEl);
    updateBar();
    const protectedCount = candidatesOf('protected').length;
    wrap.append(h('section', { class: 'card', 'aria-label': 'Protected' },
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, `PROTECTED (${protectedCount})`),
        h('button', { type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': String(showProtected),
          onclick: () => { showProtected = !showProtected; render(); } }, showProtected ? 'Hide' : 'Show')),
      h('p', { class: 'small muted' }, 'Active checkpoints, gx-max, gx-image/video/music weights, shared encoders, rollbacks, running images, mounted paths, active downloads, secrets, Git, state and the Library. They cannot be cleaned here; model removal is a Model Manager workflow.'),
      showProtected ? candidateTable('protected') : null));
    const largest = ['node1', 'node2'].flatMap((n) => ((result[n] || {}).largest || []).map((r) => [n === 'node1' ? 'gx10-01' : 'gx10-02', h('code', {}, r.path), bytes(r.bytes)]));
    wrap.append(h('details', { class: 'card' }, h('summary', {}, h('strong', {}, 'Largest directories')),
      table(['Node', 'Path', 'Size'], largest, { caption: 'Largest directories' })));
  }
  const last = scan.last_cleanup;
  if (last) {
    wrap.append(card('Last cleanup',
      kv([['When', `${ago(last.at)} by ${last.by}`], ['Expected (dry run)', bytes(last.dry_run_bytes)],
        ['Actually freed', h('strong', { id: 'last-freed' }, bytes(last.freed_bytes))],
        ['Refused', last.refused.length ? last.refused.map((r) => `${r.target}: ${r.error}`).join('; ') : 'none'],
        ['Post-cleanup checks', last.verification ? (last.verification.ok ? 'all passed' : last.verification.checks.filter((c) => !c.ok).map((c) => c.check).join(', ')) : '—']])));
  }
  clear(root).append(wrap);
}

export default {
  title: 'Storage & Cleanup',
  interval: 4,
  async mount(el, { ctx }) {
    root = el;
    ctxRef = ctx;
    selected.clear();
  },
  async refresh({ signal }) {
    try {
      const [st, setup] = await Promise.all([api.get('/api/storage', { signal }),
        data ? Promise.resolve({ playground_url: data.playground_url }) : api.get('/api/setup', { signal })]);
      data = { ...st, playground_url: setup.playground_url };
      const active = document.activeElement;
      const focusId = active && active.dataset ? active.dataset.candidate : null;
      render();
      if (focusId) {
        const again = root.querySelector(`[data-candidate="${focusId}"]`);
        if (again) again.focus();
      }
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
