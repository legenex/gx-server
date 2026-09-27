// NETWORK: both ConnectX rails from the registry, live-verified with ethtool,
// show_gids, /sys counters and a ping — read-only by construction (L-7: no
// dashboard path can reconfigure the fabric). Diagnostics is a runner, not a fixer.
import { api } from '../api.js';
import {
  h, clear, card, kv, levelBadge, stateBadge, table, errorBox, spinner, bytes, num,
} from '../dom.js';

let root;

function countersKv(c) {
  if (!c) return h('span', { class: 'muted' }, '—');
  return kv([
    ['rx/tx bytes', `${bytes(c.rx_bytes)} / ${bytes(c.tx_bytes)}`],
    ['rx/tx errors', `${c.rx_errors} / ${c.tx_errors}`],
    ['rx/tx dropped', `${c.rx_dropped} / ${c.tx_dropped}`],
  ]);
}

function railCard(r) {
  const et = r.ethtool || {};
  const body = [];
  if (r.offline) {
    body.push(h('p', { class: 'callout callout-warning' }, 'unavailable: offline mode'));
  }
  body.push(kv([
    ['Head IP (gx10-01)', r.head_ip || '—'],
    ['Worker IP (gx10-02)', r.worker_ip || '—'],
    ['HCA', r.hca || '—'],
    ['Interface state', r.interface ? `${r.interface.state || '—'} ${(r.interface.ips || []).join(' ')}` : 'not found in ip -br addr'],
    ['Link (ethtool)', et.link === true ? levelBadge('ok', 'up') : (et.link === false ? levelBadge('crit', 'down') : stateBadge('unknown', String(et.error || 'unknown')))],
    ['Speed', et.speed_mbps ? `${et.speed_mbps} Mb/s` : '—'],
  ]));
  body.push(countersKv(r.counters));
  if (r.gids !== undefined) {
    body.push(table(['GID index', 'GID', 'Type'],
      (r.gids || []).map((g) => [String(g.gid_index), h('code', { class: 'small' }, g.gid), g.gid_type]),
      { caption: `${r.name} GID table`, empty: 'No GID entries (show_gids failed or not installed).' }));
    const pinned = r.gids_pinned;
    body.push(h('p', { class: 'muted small' },
      `NCCL_IB_GID_INDEX pin: ${r.nccl_gid_index_pin ?? 'not set'} · `
      + (pinned ? `pinned entry ${pinned.gid_index} → ${pinned.gid}` : 'the pinned index is NOT present in the GID table')));
  }
  const ping = r.ping_worker || {};
  body.push(h('p', {}, 'Ping worker over the fabric: ',
    ping.ok ? levelBadge('ok', `ok${ping.ms ? ` (${num(ping.ms, 1)} ms)` : ''}`)
      : levelBadge('crit', `failed${ping.reason ? ` — ${ping.reason}` : (ping.ms !== null && ping.ms !== undefined ? '' : '')}`)));
  return h('section', { class: `card level-${r.level}`, 'aria-label': r.name },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, r.name), levelBadge(r.level)),
    ...body);
}

function render(d) {
  const grid = h('div', { class: 'grid grid-2' });
  for (const r of d.rails || []) grid.append(railCard(r));
  const wrap = h('div', {});
  wrap.append(h('div', { class: 'grid grid-2' }, grid),
    card('Rank containers (name=rank)',
      table(['Container', 'Image', 'State', 'Status'],
        (d.rank_containers || []).map((c) => [c.Names || c.names || '—', c.Image || c.image || '—',
          c.State || c.state || '—', c.Status || c.status || '—']),
        { caption: 'Rank containers', empty: 'No rank containers are running (expected while gx-max is down).' }),
      h('p', { class: 'muted small' }, d.diagnostics_note || 'read-only diagnostics')),
    h('div', { class: 'diag-holder' }));
  clear(root).append(wrap);
}

async function runDiagnostics() {
  const holder = root.querySelector('.diag-holder');
  clear(holder).append(spinner('Running read-only diagnostics…'));
  try {
    const d = await api.get('/api/network/diagnostics');
    clear(holder).append(card('Diagnostics run (read-only)',
      d.available === false
        ? h('p', { class: 'callout callout-warning' }, `unavailable: ${d.reason || 'diagnostics not available'}`)
        : table(['Rail', 'From', 'To', 'Target', 'Result'],
          (d.pairs || []).map((p) => [p.rail || '—', p.from || '—', p.to || '—', p.target || '—',
            p.result && p.result.ok ? levelBadge('ok', `ok${p.result.ms ? ` (${num(p.result.ms, 1)} ms)` : ''}`)
              : levelBadge('crit', 'failed')]),
          { caption: 'Fabric ping pairs (both directions)', empty: 'No pairs measured.' }),
      d.ibv_devinfo
        ? kv([
          ['ibv_devinfo installed', d.ibv_devinfo.present ? 'yes' : 'no'],
          ['Devices', (d.ibv_devinfo.devices || []).join(', ') || '—'],
          ['Command ok', String(d.ibv_devinfo.command_ok ?? '—')],
        ]) : null,
      h('p', { class: 'muted small' },
        'These are measurements, not fixes. The dashboard never reconfigures the network (L-7).')));
  } catch (err) {
    clear(holder).append(errorBox(err));
  }
}

export default {
  title: 'Network',
  interval: 30,
  async mount(el) {
    root = el;
    const btn = h('button', { type: 'button', class: 'btn btn-primary btn-sm', id: 'net-diag' }, 'Run diagnostics');
    btn.addEventListener('click', () => runDiagnostics());
    clear(root).append(
      h('p', { class: 'lead' }, 'Both ConnectX-7 RoCE rails, registry-pinned and live-verified. Model and '
        + 'NCCL traffic use only these rails; Tailscale is management-only (L-3). Read-only (L-7).'),
      btn, spinner());
    render(await api.get('/api/network'));
  },
  async refresh({ signal }) {
    try {
      render(await api.get('/api/network', { signal }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
