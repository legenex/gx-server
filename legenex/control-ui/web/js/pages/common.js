import { api, runAction, waitJob } from '../api.js';
import {
  h, kv, gib, num, meter, levelBadge, stateBadge, duration, short, confirmDialog, toast, ago, table,
} from '../dom.js';

export function nodeCard(n, { detailed = false } = {}) {
  const body = [];
  if (!n.reachable) {
    body.push(h('p', { class: 'callout callout-danger' }, (n.problems || []).join('; ') || 'unreachable'));
    if (n.fabric_probe) {
      body.push(kv(Object.entries(n.fabric_probe).map(([ip, st]) => [`fabric ${ip}:22`, st])));
    }
  } else {
    const swapPct = n.swap_total_gib ? (n.swap_used_gib / n.swap_total_gib) * 100 : 0;
    body.push(
      h('div', { class: 'metric' },
        h('div', { class: 'metric-head' }, h('span', {}, 'MemAvailable'),
          h('strong', {}, `${num(n.mem_available_gib)} / ${num(n.mem_total_gib)} GiB`)),
        meter(n.mem_total_gib - n.mem_available_gib, n.mem_total_gib,
          { label: `${n.name} memory in use`, warnAt: 75, critAt: 96 })),
      h('div', { class: 'metric' },
        h('div', { class: 'metric-head' }, h('span', {}, 'Swap used'),
          h('strong', {}, `${num(n.swap_used_gib)} / ${num(n.swap_total_gib)} GiB`)),
        meter(n.swap_used_gib, n.swap_total_gib, { label: `${n.name} swap used`, warnAt: 50, critAt: 90 })),
      kv([
        ['Kernel', h('span', {}, n.kernel || '—', ' ', n.kernel_ok ? stateBadge('ok', 'pinned') : stateBadge('error', 'NOT PINNED'))],
        ['Memory PSI (avg10)', `some ${num(n.psi_some_avg10, 2)}% · full ${num(n.psi_full_avg10, 2)}%`],
        ['Load (1 min)', `${num(n.load1, 2)} on ${n.nproc ?? '—'} CPUs`],
        ['GPU', n.gpu && n.gpu.name ? `${n.gpu.name} · ${num(n.gpu.celsius, 0)} °C · ${num(n.gpu.util_pct, 0)}% · ${num(n.gpu.power_w, 1)} W` : '—'],
        ['Hottest thermal zone', n.cpu_max_c !== null && n.cpu_max_c !== undefined ? `${num(n.cpu_max_c, 1)} °C` : '—'],
        ['Uptime', duration(n.uptime_seconds)],
        ['Hostwatch', n.hostwatch && n.hostwatch.ok ? `${n.hostwatch.status} (${n.hostwatch.detail}) · ${duration(n.hostwatch.age_seconds)} ago` : 'unavailable'],
        ['Admission lock', stateBadge(n.guard_lock)],
        ['Tailscale', n.tailscale_ok ? stateBadge('ok', 'running') : stateBadge('error', 'down')],
        ['Model workloads', n.workloads && n.workloads.length
          ? h('span', {}, n.workloads.map((w) => stateBadge(w.state, `${w.name}`)))
          : h('span', { class: 'muted' }, 'none')],
      ]),
    );
    if (n.problems && n.problems.length) {
      body.push(h('ul', { class: 'problems' }, n.problems.map((p) => h('li', {}, p))));
    }
  }
  return h('section', { class: `card node-card level-${n.level}`, 'aria-label': n.name },
    h('div', { class: 'card-head' },
      h('h2', { class: 'card-title' }, n.name),
      levelBadge(n.level)),
    h('p', { class: 'muted small' }, n.role),
    ...body,
    detailed ? null : null);
}

export function modelTile(m) {
  return h('a', { class: `model-tile state-${m.state}`, href: `#/models/${m.alias}` },
    h('span', { class: 'model-name' }, m.alias),
    stateBadge(m.state),
    h('span', { class: 'muted small model-detail' }, m.detail || m.state_detail || ''));
}

export function gitBlock(git) {
  const row = (label, sha) => [label, h('code', {}, short(sha, 12))];
  return h('div', {},
    h('div', { class: 'card-head' }, h('span', {}, git.match ? 'All three HEADs match' : 'HEADs differ'),
      levelBadge(git.level, git.match ? 'in sync' : 'out of sync')),
    kv([
      row('gx10-01 HEAD', git.node1_head),
      row('GitHub origin/main', git.origin_main),
      row('gx10-02 HEAD', git.node2_head),
      ['Last commit', git.node1_subject ? `${git.node1_subject} (${git.node1_date || ''})` : '—'],
      ['gx10-02 push', git.node2_push_disabled ? stateBadge('ok', 'disabled (pull-only)') : stateBadge('error', 'NOT disabled')],
      ['Uncommitted on gx10-01', String(git.node1_dirty_files ?? '—')],
    ]),
    git.note ? h('p', { class: 'muted small' }, git.note) : null,
    git.remote_error ? h('p', { class: 'callout callout-warning' }, `GitHub: ${git.remote_error}`) : null);
}

export function unitBadge(u) {
  if (!u) return stateBadge('unknown', 'not installed');
  return stateBadge(u.active, `${u.active}${u.sub && u.sub !== u.active ? ` / ${u.sub}` : ''}`);
}

export function servicesTable(services) {
  return table(['Service', 'State', 'Latency', 'Note'], services.map((s) => [
    s.name, levelBadge(s.level, s.ok ? 'up' : (s.level === 'ok' ? 'stopped' : 'down')),
    s.ms !== null && s.ms !== undefined ? `${s.ms} ms` : '—',
    s.error || s.note || '',
  ]), { caption: 'Services' });
}

// Runs a model/system operation with confirmation and live job output.
export async function operate(spec, { onDone, outputEl } = {}) {
  let confirm;
  if (spec.needs_confirm) {
    const res = await confirmDialog({
      title: spec.label,
      body: h('div', {}, h('p', {}, spec.description),
        spec.danger === 'danger' ? h('p', { class: 'callout callout-danger' }, 'This affects running workloads.') : null),
      phrase: spec.confirm_phrase || null,
      okLabel: spec.label,
      danger: spec.danger !== 'safe',
    });
    if (!res.ok) return null;
    confirm = spec.confirm_phrase ? res.phrase : true;
  }
  try {
    const job = await runAction(spec.name, confirm);
    toast(`Started: ${spec.label}`, 'ok');
    const final = await waitJob(job.id, (j) => {
      if (outputEl) {
        outputEl.hidden = false;
        outputEl.textContent = `${j.label} — ${j.state} (${duration(j.elapsed_seconds)})\n${(j.output || []).join('\n')}`;
        outputEl.scrollTop = outputEl.scrollHeight;
      }
    }, { interval: 2500 });
    toast(`${spec.label}: ${final.state}`, final.state === 'succeeded' ? 'ok' : 'crit', 8000);
    if (onDone) onDone(final);
    return final;
  } catch (err) {
    toast(`${spec.label}: ${err.message}`, 'crit', 9000);
    if (onDone) onDone(null);
    return null;
  }
}

export function lockLedger(ov) {
  const rows = [];
  for (const node of ['node1', 'node2']) {
    const ledger = (ov.ledger || {})[node] || {};
    const names = Object.keys(ledger).filter((k) => !k.startsWith('_'));
    rows.push([node === 'node1' ? 'gx10-01' : 'gx10-02', stateBadge((ov.locks || {})[node] || 'unknown'),
      names.length ? names.map((k) => `${k} (${ledger[k].class}, ${ledger[k].estimated_gib} GiB)`).join(', ') : 'empty']);
  }
  return table(['Node', 'Admission lock', 'Residency ledger'], rows, { caption: 'Locks and ledger' });
}

export function relTime(epoch) { return ago(epoch); }
export { api };
