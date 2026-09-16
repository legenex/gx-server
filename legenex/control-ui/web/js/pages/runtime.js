import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, table, errorBox, num, gib, bytes,
} from '../dom.js';
import { nodeCard, servicesTable, unitBadge } from './common.js';

let root;

function psiTable(psi) {
  const rows = [];
  for (const kind of ['memory', 'cpu', 'io']) {
    const k = (psi || {})[kind] || {};
    for (const scope of ['some', 'full']) {
      if (!k[scope]) continue;
      rows.push([kind, scope, num(k[scope].avg10, 2), num(k[scope].avg60, 2), num(k[scope].avg300, 2)]);
    }
  }
  return table(['Resource', 'Scope', 'avg10 %', 'avg60 %', 'avg300 %'], rows, { caption: 'Pressure stall information' });
}

function nodeDetail(n) {
  const mem = n.memory || {};
  const stats = n.docker_stats || {};
  const running = (n.containers || []).filter((c) => c.state === 'running');
  const other = (n.containers || []).filter((c) => c.state !== 'running');
  return h('div', { class: 'stack' },
    nodeCard(n),
    card(`${n.name} — memory & swap`,
      kv([
        ['MemTotal', gib(mem.MemTotal, 2)],
        ['MemAvailable', gib(mem.MemAvailable, 2)],
        ['MemFree', gib(mem.MemFree, 2)],
        ['Page cache', gib(mem.Cached, 2)],
        ['Shmem', gib(mem.Shmem, 2)],
        ['Mlocked', gib(mem.Mlocked, 2)],
        ['Swap total / free', `${gib(mem.SwapTotal, 2)} / ${gib(mem.SwapFree, 2)}`],
        ['Swap cached', gib(mem.SwapCached, 2)],
      ]),
      table(['Swap device', 'Size', 'Used', 'Priority'], (n.swaps || []).map((s) => [
        h('code', {}, s.name), bytes(s.size), bytes(s.used), s.priority,
      ]), { caption: 'Swap devices' }),
      psiTable(n.psi)),
    card(`${n.name} — Docker workloads`,
      table(['Container', 'State', 'CPU', 'Memory (cgroup)', 'Image'], running.map((c) => [
        h('strong', {}, c.name), stateBadge(c.state, c.status),
        (stats[c.name] || {}).cpu || '—', (stats[c.name] || {}).mem || '—', h('code', {}, c.image),
      ]), { caption: 'Running containers', empty: 'No running containers.' }),
      h('p', { class: 'muted small' }, 'On DGX Spark unified memory the CUDA pool is not charged to the container cgroup (B-021): ',
        'use MemAvailable, not the cgroup column, to judge a model\'s footprint.'),
      other.length ? h('details', {}, h('summary', {}, `${other.length} stopped container(s)`),
        table(['Container', 'State', 'Image'], other.map((c) => [c.name, stateBadge(c.state, c.status), c.image]),
          { caption: 'Stopped containers' })) : null),
    card(`${n.name} — user services`,
      table(['Unit', 'State', 'Enabled', 'Next run'], (n.units || []).map((u) => [
        h('code', {}, u.unit), unitBadge(u), u.enabled || '—', u.next && u.next !== 'n/a' ? u.next : '—',
      ]), { caption: 'User units' }),
      kv([['gx-max watcher on this node', n.watcher && n.watcher.alive
        ? stateBadge('running', `pid ${n.watcher.pid}`) : stateBadge('idle', 'not running (normal while gx-max is down)')]])));
}

function render(d) {
  const swapRows = [];
  for (const [node, list] of Object.entries(d.llama_swap || {})) {
    for (const r of list || []) swapRows.push([node === 'node1' ? 'gx10-01' : 'gx10-02', r.model, stateBadge(r.state), r.ttl ? `${r.ttl}s` : 'never']);
  }
  const media = d.media || {};
  const orch = d.orchestrator || {};
  const tiers = orch.tiers || {};
  const ledgerRows = [];
  for (const node of ['node1', 'node2']) {
    const l = (d.ledger || {})[node] || {};
    for (const [name, e] of Object.entries(l)) {
      if (!name.startsWith('_')) ledgerRows.push([node, name, e.class, `${e.estimated_gib} GiB`, e.container || name]);
    }
  }
  const gx = d.gxmax || {};
  clear(root).append(
    h('div', { class: 'grid grid-2' }, nodeDetail(d.node1), nodeDetail(d.node2)),
    h('div', { class: 'grid grid-2' },
      card('Control-plane services', servicesTable(d.services)),
      card('llama-swap — running models',
        table(['Node', 'Model', 'State', 'Idle TTL'], swapRows, { caption: 'llama-swap running', empty: 'No model is running under llama-swap.' })),
      card('Orchestrator view (per tier)',
        table(['Tier', 'State', 'Routable', 'Reason'], Object.entries(tiers).map(([t, v]) => [
          t, stateBadge(v.state), v.usable ? 'yes' : 'no', v.reason || '',
        ]), { caption: 'Orchestrator tiers', empty: 'Orchestrator not reachable.' })),
      card('gx-max lifecycle & watchdogs',
        kv([
          ['State', stateBadge(gx.state)],
          ['Phase', gx.phase || '—'],
          ['rank 0 watcher (gx10-01)', d.node1.watcher && d.node1.watcher.alive ? stateBadge('running') : stateBadge('idle', 'not running')],
          ['rank 1 deadman (gx10-02)', d.node2.watcher && d.node2.watcher.alive ? stateBadge('running') : stateBadge('idle', 'not running')],
        ])),
      card('Media router / ComfyUI (gx10-02)',
        media && media.status ? kv([
          ['Router', stateBadge('ready', media.status)],
          ['Busy', media.busy ? `yes — ${media.held_by} (${media.held_for_seconds} s)` : 'no'],
          ['Video queue depth', String(media.video_queue_depth ?? 0)],
          ['Workflows', (media.workflows || []).join(', ')],
          ['ComfyUI', media.comfyui && media.comfyui.reachable ? stateBadge('ready', `v${media.comfyui.comfyui_version}`) : stateBadge('error', 'unreachable')],
          ['ComfyUI free VRAM (reported)', media.comfyui ? gib(media.comfyui.vram_free_bytes) : '—'],
          ['Device', media.comfyui ? media.comfyui.device : '—'],
        ]) : h('p', { class: 'muted' }, 'Media router not reachable (stopped on purpose while gx-max runs).')),
      card('Residency ledger',
        table(['Node', 'Workload', 'Class', 'Estimate', 'Container'], ledgerRows, { caption: 'Residency ledger', empty: 'Empty — no large/exclusive workload is registered.' })),
      card('Hostwatch',
        table(['Node', 'Check', 'Level', 'Detail'], ['node1', 'node2'].flatMap((k) => {
          const hw = d[k].hostwatch || {};
          return Object.entries(hw.checks || {}).map(([c, v]) => [d[k].name, c, stateBadge(v.level === 'OK' ? 'ok' : 'error', v.level), v.detail]);
        }), { caption: 'Hostwatch checks' })),
    ),
  );
}

export default {
  title: 'Runtime',
  interval: 5,
  mount(el) { root = el; },
  async refresh({ signal }) {
    try {
      render(await api.get('/api/nodes', { signal }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
