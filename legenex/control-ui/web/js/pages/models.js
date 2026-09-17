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

function allowed(op, state, alias) {
  if (alias === 'gx-music') {
    if (op === 'load') return ['ready', 'unloaded'].includes(state);
    if (op === 'unload') return ['loaded'].includes(state);
  }
  switch (op) {
    case 'load': return ['unloaded'].includes(state);
    case 'unload': return ['loaded', 'loading', 'ready'].includes(state);
    case 'restart': return ['loaded', 'unloaded'].includes(state);
    case 'force_release': return true;
    default: return false;
  }
}

function controls(m) {
  const wrap = h('div', { class: 'btn-row' });
  const ops = Object.entries(m.actions || {});
  if (!ops.length) {
    wrap.append(h('span', { class: 'muted small' },
      m.alias === 'gx-auto' ? 'Routing alias — nothing to load. It uses whichever tiers are available.' : 'No controls.'));
    return wrap;
  }
  for (const [op, spec] of ops) {
    const enabled = allowed(op, m.state, m.alias) || (m.alias.startsWith('gx-image') || m.alias.startsWith('gx-video'));
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
  if (m.alias === 'gx-image' || m.alias === 'gx-video' || m.alias === 'gx-music') {
    wrap.append(h('span', { class: 'muted small' }, 'Loads automatically on the first generation. '),
      h('a', { class: 'small', href: '#/resources' }, 'Resource Control →'));
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
  const lc = lv.lifecycle || {};
  const info = lv.sglang_info || {};
  const served = (lv.sglang_models || []).map((x) => x.id).join(', ');
  const swap = lv.swap || {};
  const rdma = lv.rdma || {};
  const rdmaRows = [];
  for (const node of ['node1', 'node2']) {
    for (const r of rdma[node] || []) {
      if ((r.state || '').includes('ACTIVE')) {
        rdmaRows.push([node === 'node1' ? 'gx10-01' : 'gx10-02', r.device, r.netdev, r.rate,
          `${num((r.xmit_bytes || 0) / 2 ** 30, 2)} / ${num((r.rcv_bytes || 0) / 2 ** 30, 2)} GiB`]);
      }
    }
  }
  return h('div', { class: 'gxmax-panel' },
    h('h3', {}, 'Two-node engine'),
    kv([
      ['Model', m.model],
      ['Revision', m.revision || '—'],
      ['Engine', 'SGLang · lmsysorg/sglang:dev-v4f-2dgx-v2'],
      ['Topology', 'TP=2 · nnodes=2 · rank 0 on gx10-01 · rank 1 on gx10-02'],
      ['Bootstrap', '192.168.100.10:5000 (RoCE rail 1)'],
      ['Lifecycle state', h('span', {}, stateBadge(lc.state), lc.phase ? ` · phase ${lc.phase}` : '')],
      ['In state for', duration(lc.seconds_in_state)],
      ['Waiting requests', String(lc.waiters ?? 0)],
      ['Readiness (SGLang /health)', lv.sglang_health && lv.sglang_health.ok ? stateBadge('ready', 'healthy') : stateBadge('idle', 'not serving')],
      ['Served model id', served || '—'],
      ['Live tp_size / nnodes', info.tp_size ? `${info.tp_size} / ${info.nnodes ?? '—'}` : '—'],
      ['Last measured startup', lc.last_startup_seconds ? `${lc.last_startup_seconds} s` : 'not recorded yet (reference: 508–559 s cold)'],
      ['Idle auto-release', lc.idle_ttl ? duration(lc.idle_ttl) : '—'],
    ]),
    table(['Rank', 'Container'], [rankRow('rank 0 (gx10-01)', lv.rank0), rankRow('rank 1 (gx10-02)', lv.rank1)], { caption: 'Ranks' }),
    kv([
      ['rank 0 watcher', lv.rank0_watcher && lv.rank0_watcher.alive ? stateBadge('running', `pid ${lv.rank0_watcher.pid}`) : stateBadge('idle', 'not running')],
      ['rank 1 deadman', lv.rank1_deadman && lv.rank1_deadman.alive ? stateBadge('running', `pid ${lv.rank1_deadman.pid}`) : stateBadge('idle', 'not running')],
      ['gx10-01 admission lock', stateBadge(lv.node1_lock || 'unknown')],
      ['gx10-02 admission lock', stateBadge(lv.node2_lock || 'unknown')],
      ['Swap gx10-01', swap.node1 ? `${gib(swap.node1.used)} / ${gib(swap.node1.total)}` : '—'],
      ['Swap gx10-02', swap.node2 ? `${gib(swap.node2.used)} / ${gib(swap.node2.total)}` : '—'],
    ]),
    h('aside', { class: 'callout callout-warning' },
      h('strong', {}, 'Startup transient: '),
      'while the weights stage, gx10-01 drops to ~2.5–3.3 GiB MemAvailable and swap can briefly reach the full ~64 GiB; ',
      'gx10-02 peaks at ~51–55 GiB swap. This is expected and policed live by gx-max-safety.sh on both nodes. ',
      'It settles to ~15–18 GiB MemAvailable per node once serving.'),
    h('h3', {}, 'RDMA proof'),
    table(['Node', 'Device', 'Netdev', 'Rate', 'Cumulative tx / rx'], rdmaRows, { caption: 'RDMA counters', empty: 'No active RDMA ports reported.' }),
    h('p', { class: 'muted small' }, 'Counters are cumulative since boot. During a gx-max load and generation both rails move by gigabytes.'),
  );
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

function modelCard(m) {
  const res = m.results || {};
  const facts = kv([
    ['Purpose', m.purpose],
    ['Model', repoLink(m)],
    ['Revision (pinned)', m.revision ? h('code', {}, m.revision) : '—'],
    ['Family', m.family || undefined],
    ['Parameters', m.parameters || undefined],
    ['Active parameters (MoE)', m.active_parameters || undefined],
    ['Quantization', m.quantization || undefined],
    ['Uncensored / abliterated', m.uncensored || '—'],
    ['Engine / runtime', m.engine],
    ['Node(s)', (m.nodes || []).join(', ') || '—'],
    ['Local path', m.path ? h('code', {}, m.path) : undefined],
    ['Context window', m.context ? `${m.context.toLocaleString()} tokens` : 'n/a'],
    ['Max output', m.max_output ? `${m.max_output.toLocaleString()} tokens` : 'n/a'],
    ['Vision', yesno(m.vision)],
    ['Tool calling', yesno(m.tools)],
    ['Reasoning output', typeof m.reasoning === 'string' ? m.reasoning : yesno(m.reasoning)],
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
  componentsTable(m),
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
      h('p', { class: 'lead' }, 'The eight public aliases. Controls call the sanctioned lifecycle only: llama-swap for gx-mini / gx-fast / gx-reason, ',
        'the orchestrator for gx-max, the media router for gx-image / gx-video, the gx-music supervisor for gx-music. ',
        'There is no direct docker start for gx-max. Profiles, pins and Maintenance live in ', h('a', { href: '#/resources' }, 'Resource Control'), '.'),
      h('div', { class: 'btn-row' },
        h('a', { class: 'btn btn-ghost btn-sm', href: '#/playground' }, 'Try a model in the playground →'),
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
