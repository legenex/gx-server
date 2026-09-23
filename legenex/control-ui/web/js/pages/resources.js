// RESOURCE CONTROL (D-037): profiles, live resource map, manual lifecycle
// controls, admission explanations, compatibility and advanced policy.
// Every control goes through the server's Resource Controller, which uses
// the sanctioned lifecycle paths and never bypasses admission.
import { api } from '../api.js';
import {
  h, clear, card, kv, table, toast, errorBox, confirmDialog, num, meter, duration, ago,
} from '../dom.js';

let root;
let ctxRef;
let busy = false;
let snapCache = null;
let compatCache = null;
let compatAt = 0;
let selectedPair = null;

const STATE_CLASS = {
  READY: 'ok', GENERATING: 'ok', LOADING: 'warn', WAITING: 'warn', DRAINING: 'warn',
  UNLOADED: 'idle', BLOCKED: 'idle', ERROR: 'crit',
};
const VERDICT = {
  coexist: ['ok', 'Coexist'], scheduled: ['warn', 'Scheduler'], serialized: ['warn', 'Serialized'],
  exclusive: ['crit', 'Exclusive'],
};
const OP_LABEL = { load: 'Load', unload: 'Unload', drain: 'Drain', pin: 'Pin', unpin: 'Unpin' };

function stateChip(state) {
  return h('span', { class: `badge badge-${STATE_CLASS[state] || 'unknown'}` },
    h('span', { class: 'dot', 'aria-hidden': 'true' }), state);
}

function gb(v) { return v === null || v === undefined ? '—' : `${num(v, 1)} GiB`; }

async function changeProfile(target) {
  if (busy) return;
  let plan;
  try {
    plan = await api.get(`/api/resources/profile/plan?to=${encodeURIComponent(target)}`);
  } catch (e) { toast(e.message, 'crit'); return; }
  const list = (label, items) => (items && items.length
    ? h('p', {}, h('strong', {}, `${label}: `), items.join(', ')) : null);
  const queued = Object.entries(plan.queued || {}).map(([k, v]) => `${k} ${v}`);
  const body = h('div', {},
    h('p', {}, plan.summary),
    list('Stays loaded', plan.stays),
    list('Unloaded now', plan.drains_now),
    list('May be unloaded when needed', plan.may_drain),
    list('Working right now', plan.active),
    list('Queued', queued),
    plan.conflicts.length ? h('ul', { class: 'problems' }, plan.conflicts.map((c) => h('li', {}, c))) : null);
  let confirm;
  if (plan.needs_confirm || target === 'maintenance') {
    const res = await confirmDialog({
      title: `Switch to ${plan.label}?`, body, phrase: plan.confirm_phrase || null,
      okLabel: `Switch to ${plan.label}`, danger: true,
    });
    if (!res.ok) return;
    confirm = plan.confirm_phrase ? res.phrase : true;
  }
  busy = true;
  try {
    const res = await api.post('/api/resources/profile', confirm === undefined ? { profile: target } : { profile: target, confirm });
    toast(`Profile: ${plan.label}${res.steps.length ? ` — ${res.steps.join('; ')}` : ''}`, 'ok', 8000);
  } catch (e) { toast(e.message, 'crit', 9000); }
  busy = false;
  ctxRef.refreshNow();
}

async function control(alias, op, runtime, admission) {
  if (busy) return;
  let confirm;
  if (op === 'load' && admission && !admission.allowed) {
    const action = (admission.actions || [])[0];
    if (!action) {
      toast(`${alias} cannot load now: ${admission.reason}`, 'warn', 9000);
      return;
    }
    const res = await confirmDialog({
      title: `Load ${alias}?`,
      body: h('div', {}, h('p', {}, admission.reason), h('p', {}, `${action.label}. Nothing that is working right now is interrupted.`)),
      okLabel: action.label, danger: true,
    });
    if (!res.ok) return;
    confirm = 'unload_and_continue';
  } else if (op === 'load' && alias === 'gx-max') {
    const res = await confirmDialog({
      title: 'Load gx-max (two-node takeover)?',
      body: 'Every other model is drained on both nodes, then rank 1 and rank 0 start (about 9 minutes). Prefer the Max profile.',
      phrase: 'gx-max', okLabel: 'Load gx-max',
    });
    if (!res.ok) return;
    confirm = res.phrase;
  } else if (op === 'unload' && runtime.state === 'GENERATING') {
    const res = await confirmDialog({
      title: `Unload ${alias} while it is working?`,
      body: 'The running request or job fails. Use Drain to let it finish first.',
      okLabel: 'Unload anyway',
    });
    if (!res.ok) return;
    confirm = true;
  } else if (op === 'unload' && ['gx-mini', 'gx-fast'].includes(alias)) {
    const res = await confirmDialog({
      title: `Unload ${alias}?`, body: `${alias} is resident by policy. Requests will wait while it loads again.`,
      okLabel: 'Unload',
    });
    if (!res.ok) return;
  }
  busy = true;
  try {
    const res = await api.post(`/api/resources/${alias}/${op}`, confirm === undefined ? {} : { confirm });
    toast(`${OP_LABEL[op]} ${alias}: ${res.started ? 'started' : 'done'}`, 'ok');
  } catch (e) {
    toast(`${OP_LABEL[op]} ${alias}: ${e.message}`, 'crit', 9000);
  }
  busy = false;
  ctxRef.refreshNow();
}

function runtimeTile(snap, alias) {
  const r = snap.runtimes[alias];
  const adm = (snap.admission || {})[alias] || {};
  const p = snap.policies[alias];
  const ops = (r.controls || []).filter((op) => (op === 'pin' ? !r.pinned : op === 'unpin' ? r.pinned : true));
  const btns = ops.map((op) => {
    let disabled = busy;
    if (op === 'load') disabled = disabled || ['READY', 'GENERATING', 'LOADING'].includes(r.state);
    if (op === 'unload' || op === 'drain') disabled = disabled || ['UNLOADED'].includes(r.state);
    return h('button', {
      type: 'button', class: `btn btn-sm${op === 'load' ? ' btn-primary' : ''}`, disabled,
      'data-control': `${alias}:${op}`, 'aria-label': `${OP_LABEL[op]} ${alias}`,
      onclick: () => control(alias, op, r, adm),
    }, OP_LABEL[op]);
  });
  const why = r.state !== 'READY' && adm && adm.allowed === false && adm.reason
    ? h('p', { class: 'small admission-why' }, adm.reason) : null;
  return h('article', { class: `rt-tile rt-${(STATE_CLASS[r.state] || 'unknown')}`, 'data-alias': alias },
    h('div', { class: 'card-head' }, h('strong', {}, alias), stateChip(r.state)),
    h('p', { class: 'muted small' }, r.detail || ''),
    kv([
      ['Footprint', alias === 'gx-auto' ? 'none' : `~${num(p.footprint_gib, 0)} GiB (measured)`],
      ['Residency', p.idle_ttl_s ? `on demand · unloads after ${duration(p.idle_ttl_s)} idle` : p.residency],
      ['Queue', r.queue !== undefined ? String(r.queue) : undefined],
      ['Pin', r.pinned ? `${(r.pin_state || {}).honoured ? 'honoured' : 'suspended'}: ${(r.pin_state || {}).reason || ''}` : undefined],
      ['Last load', r.last_load_seconds ? `${num(r.last_load_seconds, 0)} s` : undefined],
    ]),
    why,
    btns.length ? h('div', { class: 'btn-row' }, btns) : null);
}

function nodeColumn(snap, key) {
  const n = snap.nodes[key];
  const used = n.mem_total_gib && n.mem_available_gib !== null ? n.mem_total_gib - n.mem_available_gib : null;
  const holds = Object.entries(n.holds || {}).filter(([, v]) => v && v.active !== false).map(([k]) => k);
  const ledger = Object.entries(n.ledger || {});
  return h('section', { class: 'card node-map', 'aria-label': `${n.name} resources` },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, n.name),
      holds.length ? h('span', { class: 'badge badge-warn' }, `hold: ${holds.join(', ')}`) : null),
    h('div', { class: 'metric' },
      h('div', { class: 'metric-head' }, h('span', {}, 'MemAvailable'),
        h('strong', {}, `${gb(n.mem_available_gib)} of ${gb(n.mem_total_gib)}`)),
      used !== null ? meter(used, n.mem_total_gib, { label: `${n.name} memory in use`, warnAt: 70, critAt: 90 }) : null),
    h('p', { class: 'small muted' }, `Reserve kept free: ${snap.reserve_gib} GiB · Swap ${gb(n.swap_used_gib)} / ${gb(n.swap_total_gib)}`
      + (n.psi_full_avg10 !== null && n.psi_full_avg10 !== undefined ? ` · PSI full ${num(n.psi_full_avg10, 1)}%` : '')),
    h('p', { class: 'small' }, h('strong', {}, 'Guard ledger: '),
      ledger.length ? ledger.map(([name, e]) => `${name} (${e.class}, ${e.estimated_gib} GiB)`).join(', ') : 'empty'),
    h('div', { class: 'rt-grid' }, n.runtimes.map((a) => runtimeTile(snap, a))));
}

function profileCard(snap) {
  const cur = snap.profile.profile;
  const buttons = snap.profiles.filter((p) => p.user_selectable !== false && p.id !== 'maintenance' && p.id !== 'media' && p.id !== 'music').map((p) => h('button', {
    type: 'button', class: `profile-btn${p.id === cur ? ' active' : ''}`, 'aria-pressed': String(p.id === cur),
    disabled: busy, 'data-profile': p.id, onclick: () => (p.id === cur ? null : changeProfile(p.id)),
  }, h('strong', {}, p.label), h('span', { class: 'small' }, p.summary)));
  const since = snap.profile.since ? ` since ${ago(snap.profile.since)}${snap.profile.by ? ` (by ${snap.profile.by})` : ''}` : '';
  return h('section', { class: 'card hero', 'aria-label': 'Resource profile' },
    h('div', { class: 'card-head' },
      h('h2', { class: 'card-title' }, 'Active profile: ', h('span', { id: 'active-profile' }, snap.profile.label)),
      snap.maintenance ? h('span', { class: 'badge badge-warn' }, 'MAINTENANCE') : null),
    h('p', { class: 'muted small' }, `${snap.profile.summary}${since}`),
    h('div', { class: 'profile-grid', role: 'group', 'aria-label': 'Choose a profile' }, buttons),
    h('div', { class: 'btn-row' },
      snap.maintenance
        ? h('button', { type: 'button', class: 'btn btn-primary', id: 'exit-maintenance', disabled: busy,
          onclick: () => changeProfile('auto') }, 'End Maintenance (back to Auto)')
        : h('button', { type: 'button', class: 'btn btn-danger', id: 'enter-maintenance', disabled: busy,
          onclick: () => changeProfile('maintenance') }, 'Enter Maintenance'),
      h('a', { class: 'btn btn-ghost', href: '#/storage' }, 'Storage & Cleanup')),
    h('p', { class: 'small muted' }, 'Profiles set priorities. They never bypass admission, the 30 GiB reserve, gx-max or host safety.'));
}

function waitingCard(snap) {
  const rows = [];
  for (const [alias, r] of Object.entries(snap.runtimes)) {
    if (['WAITING', 'BLOCKED'].includes(r.state) || (r.queue || 0) > 0) {
      rows.push([alias, stateChip(r.state), String(r.queue || 0), r.detail || '']);
    }
  }
  const mr = snap.media_router || {};
  return card('Queues and waiting reasons',
    table(['Runtime', 'State', 'Queued', 'Why'], rows, { caption: 'Waiting work', empty: 'Nothing is waiting.' }),
    kv([
      ['Waiting work', rows.length ? `${rows.length} runtime(s)` : 'none'],
    ]));
}

function admissionCard(snap) {
  const rows = Object.entries(snap.admission || {}).map(([alias, a]) => [
    alias,
    a.allowed ? h('span', { class: 'badge badge-ok' }, 'admissible') : h('span', { class: 'badge badge-warn' }, 'waits'),
    a.need_gib !== undefined && a.need_gib !== null ? `${num(a.need_gib, 0)} GiB` : '—',
    a.available_gib !== undefined && a.available_gib !== null ? `${num(a.available_gib, 0)} GiB` : '—',
    a.enforced_by || '',
    a.reason || '',
  ]);
  return card('Admission now (what a load would need)',
    table(['Runtime', 'Now', 'Needs available', 'Available', 'Enforced by', 'Explanation'], rows,
      { caption: 'Admission', empty: 'No data.' }));
}

function compatCard(compat) {
  const aliases = compat.aliases;
  const byPair = new Map();
  for (const p of compat.pairs) {
    byPair.set(`${p.a}|${p.b}`, p);
    byPair.set(`${p.b}|${p.a}`, p);
  }
  const detail = h('div', { class: 'compat-detail', 'aria-live': 'polite' });
  const show = (p) => {
    selectedPair = `${p.a}|${p.b}`;
    const [lvl] = VERDICT[p.verdict] || ['unknown'];
    clear(detail).append(h('h3', {}, `${p.a} + ${p.b}: `, h('span', { class: `badge badge-${lvl}` }, p.summary)),
      h('ul', {}, p.why.map((w) => h('li', {}, w))));
  };
  const head = h('tr', {}, h('th', { scope: 'col' }, ''), aliases.map((a) => h('th', { scope: 'col' }, a)));
  const body = aliases.map((a) => h('tr', {}, h('th', { scope: 'row' }, a), aliases.map((b) => {
    if (a === b) return h('td', { class: 'compat-self' }, '—');
    const p = byPair.get(`${a}|${b}`);
    const [lvl, label] = VERDICT[p.verdict] || ['unknown', p.verdict];
    return h('td', {}, h('button', {
      type: 'button', class: `compat-cell badge-${lvl}`, 'data-pair': `${a}|${b}`,
      'aria-label': `${a} with ${b}: ${p.summary}`, onclick: () => show(p),
    }, label));
  })));
  const tableEl = h('div', { class: 'table-wrap', tabindex: '0', role: 'region', 'aria-label': 'Compatibility matrix' },
    h('table', { class: 'table compat' }, h('thead', {}, head), h('tbody', {}, body)));
  const initial = selectedPair && byPair.get(selectedPair);
  if (initial) show(initial);
  else clear(detail).append(h('p', { class: 'muted' }, 'Choose a cell to see why.'));
  return card('Compatibility (computed from live memory and measured footprints)',
    h('p', { class: 'small muted' }, `Idle capacity used for the calculation: gx10-01 ${num(compat.capacity_gib.node1, 0)} GiB, gx10-02 ${num(compat.capacity_gib.node2, 0)} GiB. `,
      'Legend: Coexist = both fit; Scheduler = one order fits, the scheduler waits or hands memory over; Serialized = one engine, one job at a time; Exclusive = never together.'),
    tableEl, detail);
}

function policyCard(snap) {
  const rows = Object.values(snap.policies).map((p) => [
    p.alias, p.node, p.engine, String(p.priority),
    p.idle_ttl_s ? duration(p.idle_ttl_s) : '—',
    `cold ${num(p.cold_gib, 0)} / resident ${num(p.footprint_gib, 0)} GiB`,
    snap.runtimes[p.alias] && snap.runtimes[p.alias].pinned ? 'pinned' : p.residency,
    p.queue_allowed ? 'yes' : 'no', p.preemptible ? 'when idle' : 'no', p.exclusive,
    p.measured,
  ]);
  return h('details', { class: 'card advanced' },
    h('summary', {}, h('strong', {}, 'Advanced resource policy (read-only defaults)')),
    table(['Alias', 'Node', 'Engine', 'Priority', 'Idle TTL', 'Memory', 'Residency', 'Queue', 'Preemptible', 'Exclusive / takeover', 'Measured'],
      rows, { caption: 'Resource policy' }),
    h('p', { class: 'small muted' }, 'These are the measured defaults of the existing architecture. Pins and profiles change priority; nothing here changes the enforced admission numbers.'));
}

function render(snap, compat) {
  const grid = h('div', { class: 'stack' });
  grid.append(profileCard(snap));
  grid.append(h('div', { class: 'grid grid-2' }, nodeColumn(snap, 'node1'), nodeColumn(snap, 'node2')));
  grid.append(waitingCard(snap));
  grid.append(admissionCard(snap));
  if (compat) grid.append(compatCard(compat));
  grid.append(policyCard(snap));
  clear(root).append(grid);
}

export default {
  title: 'Resource Control',
  interval: 5,
  async mount(el, { ctx }) {
    root = el;
    ctxRef = ctx;
    busy = false;
  },
  async refresh({ signal }) {
    try {
      const snap = await api.get('/api/resources', { signal });
      if (!compatCache || Date.now() - compatAt > 15000) {
        compatCache = await api.get('/api/resources/compatibility', { signal });
        compatAt = Date.now();
      }
      snapCache = snap;
      render(snapCache, compatCache);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
