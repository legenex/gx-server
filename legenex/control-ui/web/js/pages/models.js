import { api } from '../api.js';
import {
  h, clear, kv, stateBadge, ago, duration, gib, table, errorBox, num,
} from '../dom.js';
import { operate } from './common.js';

let root;
let listEl;
let outputEl;
let focusAlias = null;
let busy = false;
let ctxRef;

const OP_LABEL = { load: 'Load', unload: 'Unload', restart: 'Restart', force_release: 'Force release' };

function yesno(v) { return v ? 'yes' : 'no'; }

function resultLine(r) {
  if (!r) return 'never recorded';
  const bits = [r.ok ? 'OK' : 'FAILED', ago(r.at)];
  if (r.latency_ms) bits.push(`${r.latency_ms} ms`);
  if (r.seconds) bits.push(`${r.seconds} s`);
  return h('span', { class: r.ok ? '' : 'text-crit' }, `${bits.join(' · ')}${r.detail ? ` — ${r.detail}` : ''}`);
}

// Badge vocabulary for the two-part state (service vs weights). The badge
// levels come from dom.js; the label is the state itself.
const STATE_BADGE = {
  READY: 'ready', LOADED: 'loaded', LOADING: 'loading', BUSY: 'running', QUEUED: 'queued',
  UNLOADED: 'unloaded', ERROR: 'error', BLOCKED: 'held',
};

function partBadge(state) {
  if (!state) return undefined;
  return stateBadge(STATE_BADGE[state] || 'unknown', state);
}

function allowed(op, state, alias) {
  if (alias === 'gx-music') {
    if (op === 'load') return ['ready', 'unloaded', 'error', 'degraded'].includes(state);
    if (op === 'unload') return ['loaded', 'ready', 'degraded', 'error'].includes(state);
  }
  switch (op) {
    case 'load': return ['unloaded', 'error', 'degraded'].includes(state);
    case 'unload': return ['loaded', 'loading', 'ready', 'degraded', 'error'].includes(state);
    // degraded/error must still allow restart — a failed last request is exactly
    // when operators need unload→load, not a permanently disabled Restart button.
    case 'restart': return ['loaded', 'unloaded', 'ready', 'degraded', 'error'].includes(state);
    case 'force_release': return true;
    default: return false;
  }
}

function controls(m) {
  const wrap = h('div', { class: 'btn-row' });
  const ops = Object.entries(m.actions || {});
  if (!ops.length) {
    if (m.alias === 'gx-auto') {
      wrap.append(h('span', { class: 'muted small' }, 'Routing alias — nothing to load. It uses whichever tiers are available.'));
    } else if (m.kind === 'audio-realtime') {
      // The engine loads when a session opens; load/unload live in Resource Control.
      wrap.append(h('span', { class: 'muted small' }, 'Loads on demand when a session opens. '),
        h('a', { class: 'small', href: '#/resources' }, 'Resource Control →'));
    } else {
      wrap.append(h('span', { class: 'muted small' }, 'No controls.'));
    }
    return wrap;
  }
  for (const [op, spec] of ops) {
    const enabled = allowed(op, m.state, m.alias);
    const cls = op === 'force_release' ? 'btn btn-danger btn-sm' : (op === 'load' ? 'btn btn-primary btn-sm' : 'btn btn-sm');
    const btn = h('button', {
      class: cls, type: 'button', disabled: busy || !enabled,
      title: spec.description, 'data-op': `${m.alias}:${op}`,
    }, OP_LABEL[op] || op);
    btn.addEventListener('click', async () => {
      busy = true;
      renderBusy();
      await operate(spec, {
        outputEl,
        onDone: () => { busy = false; ctxRef.refreshNow(); },
      });
      busy = false;
    });
    wrap.append(btn);
  }
  return wrap;
}

function renderBusy() {
  for (const b of root.querySelectorAll('button[data-op]')) b.disabled = true;
}

function rankRow(label, c) {
  return [label, c ? h('span', {}, stateBadge(c.state), ` ${c.status}`) : h('span', { class: 'muted' }, 'absent')];
}

function gxmaxPanel(m) {
  const lv = m.live || {};
  const solver = lv.solver || {};
  const reviewer = lv.reviewer || {};
  return h('div', { class: 'gxmax-panel' },
    h('h3', {}, 'Dual-worker workflow'),
    kv([
      ['Mode', lv.mode || 'dual-worker'],
      ['Solver', `${solver.alias || 'gx-code-01'} on ${solver.node || 'gx10-01'} · ${solver.state || '—'}`],
      ['Reviewer', `${reviewer.alias || 'gx-code-02'} on ${reviewer.node || 'gx10-02'} · ${reviewer.state || '—'}`],
      ['Workflow', m.state_detail || 'solver drafts, reviewer inspects, repair if needed'],
    ]),
  );
}

function codeWorkers(m) {
  const lv = m.live || {};
  return kv([
    ['Worker 01 (gx10-01)', lv['gx-code-01'] || (lv.container && lv.container.node1 && lv.container.node1.state) || '—'],
    ['Worker 02 (gx10-02)', lv['gx-code-02'] || (lv.container && lv.container.node2 && lv.container.node2.state) || '—'],
  ]);
}

function tokens(n) {
  return n === null || n === undefined ? '—' : `${Number(n).toLocaleString()} tokens`;
}

function ms(v) {
  if (v === null || v === undefined) return '—';
  return v >= 10000 ? `${num(v / 1000, 1)} s` : `${Math.round(v)} ms`;
}

function lastRequestRows(r, age) {
  if (!r) return [['Last request', 'none recorded yet']];
  const failed = r.outcome && r.outcome !== 'ok';
  return [
    ['Last request', h('span', { class: failed ? 'text-crit' : '' },
      `${failed ? 'FAILED' : 'OK'} · ${r.outcome || '—'}${r.status ? ` · HTTP ${r.status}` : ''} · ${age !== null && age !== undefined ? `${duration(age)} ago` : ago(r.ts)}${r.via ? ` · via ${r.via}` : ''}`)],
    ['Last TTFT', r.stream === false ? 'n/a (not streamed)' : ms(r.ttft_ms)],
    ['Last tokens / s', r.tokens_per_s ? num(r.tokens_per_s, 1) : '—'],
    ['Last response time', ms(r.elapsed_ms)],
    ['Prompt / completion', `${r.prompt_tokens ?? '—'} / ${r.completion_tokens ?? '—'} tokens`],
    ['Estimated input', r.estimated_input_tokens !== undefined ? `${tokens(r.estimated_input_tokens)} (tool schema ${tokens(r.tool_schema_tokens)})` : undefined],
    ['Requested output', r.requested_output_tokens !== undefined ? tokens(r.requested_output_tokens) : undefined],
    ['Safe output allowance', r.safe_output_tokens !== undefined ? tokens(r.safe_output_tokens) : undefined],
    ['Output sent', r.output_tokens !== undefined ? `${tokens(r.output_tokens)}${r.clamped ? ' (clamped to fit)' : ''}` : undefined],
    ['Attempts', r.attempts ? `${r.attempts}${r.retry_reason ? ` (${r.retry_reason})` : ''}` : undefined],
    ['Last error', r.error ? h('code', {}, String(r.error).slice(0, 240)) : undefined],
  ];
}

function textPanel(m) {
  const t = (m.live || {}).text;
  if (!t) return null;
  const rows = [];
  if (t.context_limit) {
    rows.push(['Served context window', tokens(t.context_limit)]);
    rows.push(['Engine output ceiling', tokens(t.max_output)]);
    rows.push(['gx-auto needs free for this tier', tokens(t.planning_output)]);
  }
  if (t.runtime) rows.push(['Runtime (orchestrator view)', `${t.runtime.state}${t.runtime.reason ? ` — ${t.runtime.reason}` : ''}`]);
  const lc = t.lifecycle;
  if (lc) {
    rows.push(['Lifecycle phase', `${lc.phase || lc.state}${lc.phase_seconds ? ` for ${duration(lc.phase_seconds)}` : ''}`]);
    rows.push(['Requests in flight', String(lc.in_flight ?? 0)]);
    rows.push(['Keep-warm left', lc.ttl_remaining_seconds !== null && lc.ttl_remaining_seconds !== undefined
      ? `${duration(lc.ttl_remaining_seconds)} of ${duration(lc.idle_ttl)}` : `not running (keep-warm ${duration(lc.idle_ttl)})`]);
  }
  const d = t.last_decision;
  if (d) {
    const b = d.budget || {};
    rows.push(['Last routing', h('span', {}, h('strong', {}, d.tier), ` — ${d.summary || ''}`)]);
    rows.push(['Why', (d.reasons || []).join(' · ')]);
    rows.push(['Task facts', `intent ${d.intent}, task ~${d.task_tokens} tokens, reasoning evidence ${d.reasoning_score} (raw ${d.reasoning_raw ?? '—'}, density ${d.density ?? '—'})${(d.indicators || []).length ? `: ${d.indicators.join(', ')}` : ''}`]);
    rows.push(['Context budget', `${tokens(b.estimated_input_tokens)} input (tool schema ${tokens(b.tool_schema_tokens)}, ${d.tool_count || 0} tools) in ${tokens(b.context_limit)}; output ${tokens(b.output_tokens ?? b.requested_output_tokens)}${b.clamped ? ` — clamped from ${tokens(b.requested_output_tokens)}` : ''} (${b.status})`]);
  }
  rows.push(...lastRequestRows(t.last_request, t.last_request_age_s));
  if (t.recent_gateway_failures) rows.push(['Gateway failures (15 min)', String(t.recent_gateway_failures)]);
  return h('details', { class: 'text-panel', open: m.state === 'degraded' },
    h('summary', {}, 'Latency, context budget and routing'),
    kv(rows));
}

function repoLink(m) {
  if (!m.repository) return m.model;
  return h('a', { href: `https://huggingface.co/${m.repository}/tree/${m.revision || 'main'}`, target: '_blank', rel: 'noopener noreferrer' }, m.repository);
}

function componentsTable(m) {
  if (!m.components || !m.components.length) return null;
  return h('details', {}, h('summary', {}, `Components (${m.components.length})`),
    table(['Role', 'Kind', 'Repository', 'Revision', 'File', 'Base match'], m.components.map((c) => [
      c.role, c.kind, c.repository || '—', c.revision ? c.revision.slice(0, 12) : '—', h('code', {}, c.file || '—'), c.base_match || '—',
    ]), { caption: `${m.alias} components` }));
}

const KIND_LABEL = {
  llm: 'Text model', media: 'Media service', 'audio-realtime': 'Audio / realtime service',
};

function dependsOn(m) {
  if (!m.depends_on || !m.depends_on.length) return undefined;
  return h('span', {}, m.depends_on.map((d) => h('span', { class: 'dep' },
    h('a', {
      href: `https://huggingface.co/${d.repository}/tree/${d.revision || 'main'}`,
      target: '_blank', rel: 'noopener noreferrer',
    }, d.repository), d.role ? ` — ${d.role}` : '')));
}

// A service alias has two states: the supervisor (a resident systemd --user
// process on gx10-02) and the engine it loads on demand. An idle engine is
// READY, not offline.
function serviceRows(m) {
  const sup = (m.live || {}).supervisor;
  const svc = m.service || {};
  const fp = m.measured_footprint || {};
  if (!sup && !svc.port) return [];
  const mem = (sup && sup.memory) || {};
  const sessions = sup && (sup.active_sessions ?? sup.active_jobs);
  return [
    ['Supervisor (gx10-02)', h('span', {}, partBadge(m.service_state),
      svc.port ? ` ${svc.unit || 'systemd --user'} · port ${svc.port}` : '')],
    ['Engine residency', h('span', {}, partBadge(m.engine_state),
      sup && sup.container ? ` · container ${sup.container.state}` : ' · no engine container')],
    ['Memory estimate', mem.estimate_gib ? `${num(mem.estimate_gib, 1)} GiB held while loaded` : undefined],
    ['Resident now', mem.resident_gib ? `${num(mem.resident_gib, 1)} GiB` : undefined],
    ['Measured footprint', fp.resident_gib
      ? `${num(fp.resident_gib, 1)} GiB resident${fp.cold_gib ? ` (${num(fp.cold_gib, 1)} GiB peak while loading)` : ''} — measured ${fp.measured}`
      : undefined],
    ['Cold startup', fp.startup_s ? `${num(fp.startup_s, 0)} s` : undefined],
    ['First audio', fp.first_audio_s ? `${num(fp.first_audio_s, 1)} s` : undefined],
    ['Active sessions', sessions === undefined || sessions === null ? undefined : String(sessions)],
    ['Supervisor version', (sup && sup.version) || undefined],
    ['Last health', sup && sup.checked_at ? ago(sup.checked_at) : undefined],
  ];
}

function modelCard(m) {
  const res = m.results || {};
  const llm = m.kind === 'llm' || !m.kind;
  const facts = kv([
    ['Purpose', m.purpose],
    ['Type', KIND_LABEL[m.kind] || undefined],
    ['Model', repoLink(m)],
    ['Revision (pinned)', m.revision ? h('code', {}, m.revision) : '—'],
    ['Depends on', dependsOn(m)],
    ['Family', m.family || undefined],
    ['Parameters', m.parameters || undefined],
    ['Active parameters (MoE)', m.active_parameters || undefined],
    ['Quantization', m.quantization || undefined],
    ['Uncensored / abliterated', m.uncensored || '—'],
    ['Engine / runtime', m.engine],
    ['Node(s)', (m.nodes || []).join(', ') || '—'],
    ['Local path', m.path ? h('code', {}, m.path) : undefined],
    // Chat-completion facts only where they mean something (D-040).
    ['Context window', llm ? (m.context ? `${m.context.toLocaleString()} tokens` : 'n/a') : undefined],
    ['Max output', llm ? (m.max_output ? `${m.max_output.toLocaleString()} tokens` : 'n/a') : undefined],
    ['Vision', llm ? yesno(m.vision) : undefined],
    ['Tool calling', llm ? yesno(m.tools) : undefined],
    ['Reasoning output', llm ? (typeof m.reasoning === 'string' ? m.reasoning : yesno(m.reasoning)) : undefined],
    ['Startup behaviour', m.startup],
    ['Memory impact', m.resource],
    ['Endpoint path', h('code', {}, m.endpoint)],
    ['Measured', m.measured],
    ['Licence', m.licence || undefined],
    ['Last health check', m.live && m.live.orchestrator_view && m.live.orchestrator_view.state
      ? `${m.live.orchestrator_view.state}${m.live.orchestrator_view.reason ? ` (${m.live.orchestrator_view.reason})` : ''}` : m.state_detail],
    ['Last real inference', resultLine(res.inference)],
    ['Last load', resultLine(res.load)],
    ['Last unload', resultLine(res.unload)],
    ['Previous model', m.previous ? `${m.previous.repository || m.previous.path} — ${m.previous.status || ''}` : undefined],
    ['Task', m.task && m.task !== 'chat' ? m.task : undefined],
    ['Container image', m.image ? h('code', {}, m.image) : undefined],
    ['Runtime source', m.runtime_repository ? h('span', {}, h('code', {}, m.runtime_repository), ' @ ',
      h('code', {}, String(m.runtime_revision || '').slice(0, 12))) : undefined],
    ['Supported', m.capabilities ? h('span', {}, m.capabilities.map((c) => h('span', { class: 'chip' }, c))) : undefined],
    ['Not supported', m.not_supported ? m.not_supported.join('; ') : undefined],
    ...serviceRows(m),
  ]);
  const interim = m.interim && m.target ? h('div', { class: 'callout callout-danger', role: 'note' },
    h('strong', {}, 'Interim model. '), `Target: ${m.target.repository} @ ${String(m.target.revision).slice(0, 12)}. `,
    m.target.status || '', ' ', h('a', { href: '#/manager' }, 'Model Manager →')) : null;
  const card = h('section', {
    class: `card model-card state-${m.state}${focusAlias === m.alias ? ' focused' : ''}`,
    id: `model-${m.alias}`, 'aria-labelledby': `mt-${m.alias}`,
  },
  h('div', { class: 'card-head' },
    h('h2', { class: 'card-title', id: `mt-${m.alias}` }, m.alias),
    stateBadge(m.state)),
  h('p', { class: 'muted small' }, m.state_detail || ''),
  interim,
  controls(m),
  facts,
  m.alias === 'gx-code' ? codeWorkers(m) : null,
  componentsTable(m),
  textPanel(m),
  m.alias === 'gx-max' ? gxmaxPanel(m) : null);
  return card;
}

export default {
  title: 'Models',
  interval: 5,
  mount(el, { params, ctx }) {
    root = el;
    ctxRef = ctx;
    focusAlias = params && params[0] ? params[0] : null;
    clear(root);
    root.append(
      h('p', { class: 'lead' }, 'Four logical modes: gx-mini, gx-code, gx-auto and gx-max. ',
        'gx-code is two independent Ornith Q5_K_M workers (01 on gx10-01, 02 on gx10-02). ',
        'gx-auto routes light work to mini and coding to gx-code. ',
        'gx-max is dual-worker solver + reviewer on those two workers.'),
      h('div', { class: 'btn-row' },
        h('a', { class: 'btn btn-ghost btn-sm', href: '#/docs/models' }, 'Which model should I use? →')),
    );
    outputEl = h('pre', { class: 'job-output', hidden: true, 'aria-live': 'polite', tabindex: '0' });
    root.append(outputEl);
    listEl = h('div', { class: 'model-list' });
    root.append(listEl);
  },
  async refresh({ signal }) {
    try {
      const data = await api.get('/api/models', { signal });
      if (data.running_jobs && data.running_jobs.length) busy = true;
      else if (busy && !data.running_jobs.length) busy = false;
      clear(listEl).append(...data.models.map(modelCard));
      if (data.running_jobs.length) {
        listEl.prepend(h('p', { class: 'callout callout-note' },
          `Running: ${data.running_jobs.map((j) => `${j.label} (${duration(j.elapsed_seconds)})`).join(', ')}. Controls are locked until it finishes.`));
      }
      if (focusAlias) {
        const target = document.getElementById(`model-${focusAlias}`);
        if (target) target.scrollIntoView({ block: 'start' });
        focusAlias = null;
      }
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(listEl).append(errorBox(err));
      throw err;
    }
  },
};
