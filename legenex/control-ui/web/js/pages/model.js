// MODEL: the single DeepSeek V4.1 Flash EXL3 world. Registry cards (stock +
// uncensored packs), the gx-max/gx-auto aliases, the six serving profiles with
// their effective values, the reasoning ladder, and the gx-max lifecycle.
// All facts come from /api/models + /api/resources; nothing is guessed.
import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, table, errorBox, spinner,
} from '../dom.js';
import { operate } from './common.js';

let root;
let ctxRef;
let busy = false;
let outputEl;
let actions = {};
let state = { models: null, resources: null };

const TOKENS = (n) => (n === null || n === undefined ? '—' : `${Number(n).toLocaleString()} tokens`);

function packCard(m) {
  return h('section', { class: `card model-card state-${m.state}`, id: `model-${m.id}`, 'aria-labelledby': `mt-${m.id}` },
    h('div', { class: 'card-head' },
      h('h2', { class: 'card-title', id: `mt-${m.id}` }, m.id),
      h('span', {}, stateBadge(m.state),
        m.uncensored ? h('span', { class: 'badge badge-warn' }, 'UNCENSORED') : null,
        m.production ? h('span', { class: 'badge badge-ok' }, 'PRODUCTION') : null)),
    h('p', { class: 'muted small' }, m.state_detail || ''),
    kv([
      ['Source', m.source ? h('a', {
        href: `https://huggingface.co/${m.source}/tree/${m.revision || 'main'}`,
        target: '_blank', rel: 'noopener noreferrer',
      }, m.source) : '—'],
      ['Revision (pinned)', m.revision ? h('code', {}, m.revision) : '—'],
      ['Local path', m.path ? h('code', {}, m.path) : '—'],
      ['Engram dir', m.engram_dir ? h('code', {}, m.engram_dir) : '—'],
      ['Quantisation', m.quant || '—'],
      ['Max context', m.max_context ? TOKENS(m.max_context) : '—'],
      ['Vision / tools', `${m.vision ? 'yes' : 'no'} / ${m.tools ? 'yes' : 'no'}`],
      ['Production role', m.production ? 'served through gx-max (uncensored is the production requirement)'
        : 'on disk; gx-max never silently falls back to stock'],
      ['Serving notes', m.serving_notes ? h('code', { class: 'small' }, JSON.stringify(m.serving_notes)) : undefined],
    ]));
}

function aliasCard(m) {
  return h('section', { class: 'card model-card', id: `model-${m.id || m.alias}`, 'aria-labelledby': `at-${m.id || m.alias}` },
    h('div', { class: 'card-head' },
      h('h2', { class: 'card-title', id: `at-${m.id || m.alias}` }, m.id || m.alias),
      stateBadge(m.state)),
    h('p', { class: 'muted small' }, m.state_detail || ''),
    kv([
      ['Mode', m.mode || '—'],
      ['Description', m.description || '—'],
      ['Model', m.model || '—'],
      ['Runtime', m.runtime || '—'],
      ['Uncensored', m.uncensored ? 'yes' : '—'],
    ]));
}

function profileValue(v) {
  if (Array.isArray(v)) return v.join(' – ');
  if (v === null || v === undefined) return '—';
  return String(v);
}

function profileGrid(profiles, serving, gxState) {
  const names = Object.keys(profiles || {});
  if (!names.length) {
    return h('p', { class: 'muted' }, 'unavailable: the registry does not expose profiles (not schema 2).');
  }
  const wrap = h('div', { class: 'profile-grid', role: 'list' });
  for (const name of names) {
    const p = profiles[name] || {};
    const active = serving === name;
    const btn = h('button', {
      type: 'button', class: `profile-btn${active ? ' active' : ''}`, role: 'listitem',
      'aria-pressed': String(active), 'data-profile': name, disabled: busy,
    },
    h('strong', {}, name),
    h('span', { class: 'small' }, p.target || ''),
    h('span', { class: 'small muted' },
      `max_num_seqs ${profileValue(p.max_num_seqs)} · spec ${profileValue(p.spec_method)}`
      + ` · max_model_len ${profileValue(p.max_model_len)} · reasoning ${profileValue(p.reasoning_default)}`));
    btn.addEventListener('click', () => switchProfile(name, serving, gxState));
    wrap.append(btn);
  }
  return wrap;
}

async function switchProfile(name, serving, gxState) {
  // The serving profile is chosen at acquire time: switching it means a
  // restart while READY, a start while down. Both need the typed "gx-max"
  // confirmation; a mid-transition lifecycle is refused server-side.
  const spec = gxState === 'ready' ? actions.gxmax_restart : actions.gxmax_start;
  if (!spec) { return; }
  busy = true;
  renderProfiles();
  await operate(spec, { outputEl, args: { profile: name }, onDone: () => { busy = false; ctxRef.refreshNow(); } });
  busy = false;
}

function renderProfiles() {
  const holder = root.querySelector('.profile-holder');
  if (!holder || !state.models || !state.resources) return;
  const reg = state.models.registry || {};
  const res = state.resources;
  clear(holder).append(
    card('Serving profiles',
      h('p', { class: 'muted small' }, `Current serving profile: ${res.serving || '—'}`
        + ` · gx-max is ${res.gxmax ? res.gxmax.state : '—'}.`
        + ' The profile is applied at acquire time: clicking one restarts (or starts) gx-max with it.'),
      profileGrid(reg.profiles, res.serving, res.gxmax ? res.gxmax.state : 'unknown'),
      h('p', { class: 'muted small' }, 'Effective admission: 105 GiB per rank + 30 GiB reserve per node, '
        + `enforced by the orchestrator guard (need ${res.reserve_gib} GiB free per node before acquire).`)),
    reasoningCard(reg));
}

function reasoningCard(reg) {
  const r = reg.reasoning || {};
  const levels = r.levels || [];
  if (!levels.length) {
    return card('Reasoning ladder', h('p', { class: 'muted' }, 'unavailable: the registry does not expose the reasoning ladder.'));
  }
  const rows = levels.map((lvl) => {
    const m = (r.mapping || {})[lvl];
    return [lvl, m === undefined ? '—' : h('code', {}, JSON.stringify(m))];
  });
  if (r.numeric_range) {
    rows.push(['numeric range', `${r.numeric_range[0]} – ${r.numeric_range[1]} (reasoning_effort)`]);
  }
  return card('Reasoning ladder',
    table(['Level', 'Effective request mapping'], rows, { caption: 'Reasoning ladder' }),
    h('p', { class: 'muted small' }, 'gx-auto picks a level per request; gx-max uses the profile default.'));
}

function lifecycleCard(res) {
  const gx = (res && res.gxmax) || {};
  const specs = ['gxmax_start', 'gxmax_restart', 'gxmax_stop', 'gxmax_drain']
    .map((n) => actions[n]).filter(Boolean);
  const wrap = h('div', { class: 'btn-row' });
  if (!specs.length) {
    wrap.append(h('span', { class: 'muted small' }, 'Lifecycle actions are not loaded.'));
  } else {
    const profile = h('select', { id: 'model-profile', 'aria-label': 'Serving profile' },
      ['fast', 'balanced', 'swarm', 'deep', 'long'].map((p) => h('option', { value: p, selected: p === 'balanced' }, p)));
    wrap.append(h('label', { class: 'inline', for: 'model-profile' }, 'profile ', profile));
    for (const spec of specs) {
      const needsProfile = (spec.args || []).includes('profile');
      const btn = h('button', {
        type: 'button', class: `btn btn-sm ${spec.danger === 'danger' ? 'btn-danger' : ''}`,
        disabled: busy, 'data-op': spec.name, title: spec.description,
      }, spec.label);
      btn.addEventListener('click', async () => {
        busy = true;
        renderLifecycle(res);
        const args = needsProfile ? { profile: profile.value } : {};
        await operate(spec, { outputEl, args, onDone: () => { busy = false; ctxRef.refreshNow(); } });
        busy = false;
      });
      wrap.append(btn);
    }
  }
  return card('gx-max lifecycle',
    kv([
      ['State', stateBadge(gx.state || 'unknown')],
      ['Node 2 hold', gx.hold ? stateBadge('held', 'gx-max hold on gx10-02') : 'none'],
      ['Sizing', gx.sizing ? `${gx.sizing.rank_gib} GiB per rank · need ${gx.sizing.need_gib} GiB · reserve ${gx.sizing.reserve_gib} GiB` : '—'],
      ['Maintenance', res && res.maintenance ? stateBadge('held', 'ON — new heavy work is refused') : 'off'],
      ['Guard profile', (res && res.profile && res.profile.label) || '—'],
    ]),
    wrap,
    h('p', { class: 'muted small' }, 'All changes go through the orchestrator lifecycle API only. '
      + 'Start and restart need the typed confirmation “gx-max”.'));
}

function renderLifecycle(res) {
  const holder = root.querySelector('.lifecycle-holder');
  if (!holder) return;
  clear(holder).append(lifecycleCard(res));
}

export default {
  title: 'Model',
  interval: 5,
  mount(el, { ctx }) {
    root = el;
    ctxRef = ctx;
    busy = false;
    outputEl = h('pre', { class: 'job-output', hidden: true, 'aria-live': 'polite', tabindex: '0' });
    clear(root).append(
      h('p', { class: 'lead' }, 'One model — DeepSeek V4.1 Flash EXL3 — behind two aliases. '
        + 'gx-max serves the uncensored production pack directly; gx-auto lets the scheduler pick '
        + 'profile and reasoning per request. Registry schema 2 is the single source of truth.'),
      spinner(),
      h('div', { class: 'lifecycle-holder' }),
      h('h2', {}, 'Packs & aliases'),
      h('div', { class: 'model-list' }),
      h('div', { class: 'profile-holder' }),
      outputEl);
  },
  async refresh({ signal }) {
    try {
      const [models, res, act] = await Promise.all([
        api.get('/api/models', { signal }),
        api.get('/api/resources', { signal }).catch(() => null),
        api.get('/api/actions', { signal }).then((a) => {
          actions = {};
          for (const s of a.actions || []) if (s.name.startsWith('gxmax')) actions[s.name] = s;
        }).catch(() => {}),
      ]);
      state = { models, resources: res };
      const listEl = root.querySelector('.model-list');
      clear(listEl);
      const reg = models.registry || {};
      if (reg.registry_ok === false) {
        listEl.append(h('p', { class: 'callout callout-warning' },
          `unavailable: ${reg.note || 'registry.json is not at schema 2 — no model facts are shown'}`));
      }
      for (const m of models.models || []) {
        listEl.append(m.kind === 'alias' ? aliasCard(m) : packCard(m));
      }
      if ((models.running_jobs || []).length) {
        listEl.prepend(h('p', { class: 'callout callout-note' },
          `Running: ${(models.running_jobs).map((j) => j.label).join(', ')}. Controls are locked until it finishes.`));
      }
      renderLifecycle(res);
      renderProfiles();
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
