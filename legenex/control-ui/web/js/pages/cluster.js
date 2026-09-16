import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, levelBadge, table, errorBox, bytes, duration, num,
} from '../dom.js';

let root;
let prev = null;

const SVG_NS = 'http://www.w3.org/2000/svg';
function s(tag, attrs = {}, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  if (text !== undefined) el.textContent = text;
  return el;
}

function rate(cur, old, key, dt) {
  if (!old || !dt || cur[key] === null || cur[key] === undefined || old[key] === null || old[key] === undefined) return null;
  return Math.max(0, (cur[key] - old[key]) / dt);
}

function topology(d, rates) {
  const [n1, n2] = d.nodes;
  const svg = s('svg', { viewBox: '0 0 760 330', class: 'topology', role: 'img', 'aria-labelledby': 'topo-title topo-desc' });
  svg.append(s('title', { id: 'topo-title' }, 'Cluster topology'),
    s('desc', { id: 'topo-desc' }, `gx10-01 and gx10-02 connected by two RoCE rails (${d.rails.map((r) => `${r.name} ${r.ok ? 'up' : 'down'}`).join(', ')}) and by Tailscale for management.`));
  const box = (x, n) => {
    const g = s('g', { class: `topo-node level-${n.level}` });
    g.append(s('rect', { x, y: 40, width: 220, height: 170, rx: 14 }));
    g.append(s('text', { x: x + 110, y: 72, 'text-anchor': 'middle', class: 'topo-title' }, n.name));
    g.append(s('text', { x: x + 110, y: 96, 'text-anchor': 'middle', class: 'topo-sub' }, '128 GB unified memory'));
    g.append(s('text', { x: x + 110, y: 122, 'text-anchor': 'middle', class: 'topo-sub' },
      n.reachable ? `${num(n.mem_available_gib)} GiB available` : 'unreachable'));
    g.append(s('text', { x: x + 110, y: 146, 'text-anchor': 'middle', class: 'topo-sub' },
      n.reachable ? `swap ${num(n.swap_used_gib)} / ${num(n.swap_total_gib)} GiB` : ''));
    g.append(s('text', { x: x + 110, y: 172, 'text-anchor': 'middle', class: 'topo-sub' }, n.kernel || ''));
    g.append(s('text', { x: x + 110, y: 196, 'text-anchor': 'middle', class: 'topo-sub' },
      n.key === 'node1' ? 'control · gx-mini · gx-fast · rank 0' : 'gx-reason · media · rank 1'));
    return g;
  };
  svg.append(box(20, n1), box(520, n2));
  d.rails.forEach((r, i) => {
    const y = 90 + i * 70;
    const cls = r.ok ? 'rail-up' : 'rail-down';
    svg.append(s('line', { x1: 240, y1: y, x2: 520, y2: y, class: `rail ${cls}` }));
    const rr = rates[r.name];
    const label = `${r.name} · ${r.node1.rate || '?'} · ${r.ok ? 'ACTIVE' : 'DOWN'}`;
    svg.append(s('text', { x: 380, y: y - 8, 'text-anchor': 'middle', class: 'rail-label' }, label));
    svg.append(s('text', { x: 380, y: y + 18, 'text-anchor': 'middle', class: 'rail-sub' },
      `${r.node1_ip} ↔ ${r.node2_ip}${rr ? ` · ${bytes(rr.tx)}/s tx · ${bytes(rr.rx)}/s rx` : ''}`));
  });
  const ts = d.tailscale.level === 'ok';
  svg.append(s('path', { d: 'M130 210 C 130 300, 630 300, 630 210', class: `ts-link ${ts ? 'rail-up' : 'rail-down'}` }));
  svg.append(s('text', { x: 380, y: 292, 'text-anchor': 'middle', class: 'rail-label' },
    `Tailscale — management only (${ts ? 'connected' : 'degraded'})`));
  svg.append(s('text', { x: 380, y: 314, 'text-anchor': 'middle', class: 'rail-sub' },
    `${n1.tailscale_ip} ↔ ${n2.tailscale_ip} · SSH ${d.management.ssh_node2.ok ? 'OK' : 'FAILING'}`));
  return h('div', { class: 'topo-wrap' }, svg);
}

function render(d) {
  const now = Date.now() / 1000;
  const rates = {};
  if (prev) {
    const dt = now - prev.t;
    for (const r of d.rails) {
      const old = prev.rails[r.name];
      const tx = rate(r.node1, old, 'xmit_bytes', dt);
      const rx = rate(r.node1, old, 'rcv_bytes', dt);
      if (tx !== null) rates[r.name] = { tx, rx };
    }
  }
  prev = { t: now, rails: Object.fromEntries(d.rails.map((r) => [r.name, r.node1])) };

  clear(root).append(
    h('aside', { class: 'callout callout-note' }, h('strong', {}, 'Two separate computers. '), d.explanation),
    card('Topology', topology(d, rates)),
    h('div', { class: 'grid grid-2' },
      ...d.nodes.map((n) => card(n.name, kv([
        ['Role', n.role],
        ['State', levelBadge(n.level)],
        ['Management (Tailscale) IP', h('code', {}, n.tailscale_ip)],
        ['RoCE IPs', h('code', {}, n.key === 'node1' ? '192.168.100.10 · 192.168.101.10' : '192.168.100.11 · 192.168.101.11')],
        ['Kernel', n.kernel ? h('span', {}, n.kernel, ' ', n.kernel_ok ? stateBadge('ok', 'pinned') : stateBadge('error', 'NOT PINNED')) : '—'],
        ['Uptime', duration(n.uptime_seconds)],
        ['SSH', n.key === 'node1' ? 'local (control node)' : (d.management.ssh_node2.ok ? stateBadge('ok', `OK · ${d.management.ssh_node2.ms} ms round trip`) : stateBadge('error', 'failing'))],
      ])))),
    card('RoCE rails (model + NCCL traffic only)',
      table(['Rail', 'Subnet', 'gx10-01 port', 'gx10-02 port', 'Link / RDMA', 'MTU', 'Peer TCP probe', 'Throughput now (node 1)', 'Cumulative RDMA tx / rx (node 1)'],
        d.rails.map((r) => [
          r.name, r.subnet,
          h('span', {}, h('code', {}, r.node1.netdev || r.netdev), ` ${r.node1.device || ''}`),
          h('span', {}, h('code', {}, r.node2.netdev || r.netdev), ` ${r.node2.device || ''}`),
          h('span', {}, levelBadge(r.level, r.ok ? 'ACTIVE / LinkUp' : 'DOWN'), ` ${r.node1.rate || ''}`),
          String((r.node1_iface || {}).mtu ?? '—'),
          r.tcp_probe,
          rates[r.name] ? `${bytes(rates[r.name].tx)}/s tx · ${bytes(rates[r.name].rx)}/s rx` : 'measuring…',
          `${bytes(r.node1.xmit_bytes)} / ${bytes(r.node1.rcv_bytes)}`,
        ]), { caption: 'RoCE rails' }),
      h('p', { class: 'muted small' }, 'Only the f0 port of each ConnectX-7 is cabled. The f1 ports report DOWN/Disabled by design. ',
        'Rates are computed between two refreshes of the RDMA port counters (sysfs port_xmit_data / port_rcv_data).')),
    card('Management plane (Tailscale)',
      kv([
        ['gx10-01', d.tailscale.node1 && d.tailscale.node1.ok ? stateBadge('ok', `${d.tailscale.node1.backend} · ${(d.tailscale.node1.ips || [])[0] || ''}`) : stateBadge('error', 'down')],
        ['gx10-02', d.tailscale.node2 && d.tailscale.node2.ok ? stateBadge('ok', `${d.tailscale.node2.backend} · ${(d.tailscale.node2.ips || [])[0] || ''}`) : stateBadge('unknown', 'no data')],
        ['Peer path gx10-01 → gx10-02', ((d.tailscale.node1 || {}).peers || []).map((p) => `${p.host}: ${p.online ? 'online' : 'offline'}, ${p.direct ? 'direct' : 'relay/idle'}`).join('; ') || '—'],
        ['Rule', d.management.note],
      ])),
  );
}

export default {
  title: 'Cluster',
  interval: 5,
  mount(el) { root = el; prev = null; },
  async refresh({ signal }) {
    try {
      render(await api.get('/api/cluster', { signal }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
