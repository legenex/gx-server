// STORAGE: per-node filesystem + /srv usage analysis. ANALYSIS ONLY — every
// deletion on this cluster goes through Files > trash; this page has no
// delete button by design.
import { api } from '../api.js';
import {
  h, clear, card, kv, levelBadge, table, spinner, bytes, num, toast,
} from '../dom.js';

let root;

function diskCard(node, key) {
  const body = [];
  const disk = node.disk;
  if (!disk) {
    body.push(h('p', { class: 'callout callout-warning' },
      'unavailable: no disk facts for this node right now.'));
  } else {
    const pct = disk.percent ?? (disk.total ? ((disk.used / disk.total) * 100) : 0);
    body.push(kv([
      ['Filesystem', disk.path || '/'],
      ['Total', bytes(disk.total)],
      ['Used', bytes(disk.used)],
      ['Free', bytes(disk.free)],
      ['Percent', `${num(pct, 1)} %`],
    ]));
  }
  if (node.srv_bytes) {
    body.push(table(['/srv slice', 'Size'],
      Object.entries(node.srv_bytes).map(([k, v]) => [k, v === null ? '—' : bytes(v)]),
      { caption: `${node.name} /srv breakdown (du)`, empty: '—' }));
  }
  if (node.docker && node.docker.available !== undefined) {
    if (node.docker.available) {
      body.push(table(['Type', 'Count', 'Active', 'Size', 'Reclaimable'],
        (node.docker.rows || []).map((r) => [r.Type || r.type || '—', r.Count ?? r.count ?? '—',
          r.Active ?? r.active ?? '—', r.Size ?? r.size ?? '—', r.Reclaimable ?? r.reclaimable ?? '—']),
        { caption: `${node.name} docker usage (docker system df)`, empty: 'No docker data.' }));
    } else {
      body.push(h('p', { class: 'muted small' }, `docker usage: unavailable: ${node.docker.reason || 'not measured'}`));
    }
  }
  if (node.orchestrator_facts && node.orchestrator_facts.available === false) {
    body.push(h('p', { class: 'muted small' },
      `worker facts via orchestrator: unavailable: ${node.orchestrator_facts.reason || 'not published yet'}`));
  }
  body.push(h('p', { class: 'muted small' }, node.note || ''));
  return h('section', { class: 'card', 'aria-label': node.name },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, node.name),
      node.reachable ? levelBadge('ok', 'reachable') : levelBadge('crit', 'unreachable')),
    ...body);
}

function render(d) {
  const grid = h('div', { class: 'grid grid-2' });
  grid.append(diskCard(d.head || {}, 'head'), diskCard(d.worker || {}, 'worker'));

  const nodes = [d.head || {}, d.worker || {}];
  const thresholds = d.thresholds || {};
  const freeRows = nodes.map((n) => {
    const free = n.disk ? (n.disk.free || 0) / 2 ** 30 : null;
    let lvl = 'unknown';
    if (free !== null) {
      if (thresholds.critical_free_gib && free < thresholds.critical_free_gib) lvl = 'crit';
      else if (thresholds.low_free_gib && free < thresholds.low_free_gib) lvl = 'warn';
      else lvl = 'ok';
    }
    return [n.name || '—', free === null ? '—' : `${num(free, 1)} GiB`, levelBadge(lvl),
      lvl === 'ok' ? '' : `below the ${lvl === 'crit' ? 'critical' : 'low'} free threshold`];
  });
  grid.append(h('section', { class: 'card hero' },
    h('h2', { class: 'card-title' }, 'Free space vs thresholds'),
    table(['Node', 'Free', 'Level', 'Note'], freeRows, { caption: 'Free space thresholds' }),
    h('p', { class: 'muted small' },
      `thresholds: critical ${thresholds.critical_free_gib} GiB · low ${thresholds.low_free_gib} GiB · `
      + `watch ${thresholds.watch_free_gib} GiB.`)));

  clear(root).append(grid,
    card('Largest files & directories (top 20 under the allowed roots)',
      table(['Path', 'Size', 'GiB'],
        (d.largest || []).map((f) => [h('code', { class: 'small' }, f.path), bytes(f.bytes), String(f.gib)]),
        { caption: 'Largest items', empty: 'Nothing measured yet.' })),
    card('Stale caches (/srv/cache, older than 14 days)',
      table(['Path', 'Size', 'Age'],
        (d.stale_cache || []).map((f) => [h('code', { class: 'small' }, f.path), bytes(f.bytes), `${f.age_days} d`]),
        { caption: 'Stale caches', empty: 'No stale cache entries.' }),
      h('p', { class: 'muted small' }, 'These are candidates, not orders: clean them through Files > trash.')),
    card('Duplicate candidates (same-size files under /srv/models)',
      table(['Size', 'Files'],
        (d.duplicate_candidates || []).map((g) => [bytes(g.bytes),
          h('ul', { class: 'problems' }, (g.files || []).map((f) => h('li', {}, h('code', { class: 'small' }, f))))]),
        { caption: 'Duplicate candidates', empty: 'No same-size pairs found.' }),
      h('p', { class: 'muted small' }, 'Same-size files are candidates, never proof of waste.')),
    h('p', { class: 'muted small' }, d.cleanup_note || 'analysis only — every deletion goes through Files > trash'),
    h('p', {}, h('a', { class: 'btn btn-ghost btn-sm', href: '#/files' }, 'Open the file manager →')));
}

export default {
  title: 'Storage',
  interval: 0,
  async mount(el) {
    root = el;
    const scan = h('button', { type: 'button', class: 'btn btn-primary btn-sm', id: 'scan-storage' }, 'Scan now');
    scan.addEventListener('click', async () => {
      scan.disabled = true;
      scan.textContent = 'Scanning…';
      try {
        const d = await api.post('/api/storage/scan', {});
        render(d);
        toast('Storage scan complete', 'ok');
      } catch (err) {
        toast(`Scan failed: ${err.message}`, 'crit');
      } finally {
        scan.disabled = false;
        scan.textContent = 'Scan now';
      }
    });
    clear(root).append(
      h('p', { class: 'lead' }, 'Per-node filesystems, /srv breakdown, docker usage, largest items, stale '
        + 'caches and duplicate candidates. Analysis only — every deletion goes through the file '
        + 'manager\'s trash.'),
      scan, spinner());
    render(await api.get('/api/storage'));
  },
};
