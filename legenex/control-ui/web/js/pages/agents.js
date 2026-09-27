// AGENTS: AgentOS adapter data (coarse state only) merged with scheduler
// attribution. Upstream AgentOS has NO pause/resume/cancel and no per-agent
// metrics (state/AGENTOS-MAP.md) — this page never fabricates either.
import { api } from '../api.js';
import {
  h, clear, card, table, errorBox, spinner, ago,
} from '../dom.js';

let root;

function agentBadge(state, label) {
  const s = String(state || '').toLowerCase();
  const lvl = { running: 'ok', working: 'ok', stopped: 'idle', idle: 'idle' }[s] || 'unknown';
  return h('span', { class: `badge badge-${lvl}` }, h('span', { class: 'dot', 'aria-hidden': 'true' }),
    label || state || 'unknown');
}

function requestsCell(reqs) {
  if (!reqs) return h('span', { class: 'muted' }, 'no scheduler attribution');
  return `active ${reqs.active ?? 0} · queued ${reqs.queued ?? 0} · done ${reqs.done ?? 0} · error ${reqs.error ?? 0}`;
}

function banner(d) {
  const ao = d.agentos || {};
  if (ao.connected !== true) {
    return h('p', { class: 'callout callout-warning', role: 'note' },
      h('strong', {}, 'AgentOS NOT CONNECTED — '),
      `unavailable: ${ao.reason || 'the AgentOS Control Center did not answer'}. `
      + 'Only scheduler attribution rows are shown below; nothing is invented.');
  }
  return h('p', { class: 'muted small' },
    `AgentOS Control Center connected${ao.checked_at ? ` (checked ${ago(ao.checked_at)})` : ''}. `);
}

export default {
  title: 'Agents',
  interval: 10,
  async mount(el) {
    root = el;
    clear(root).append(
      h('p', { class: 'lead' }, 'Coarse agent states from AgentOS plus request attribution from the '
        + 'orchestrator scheduler. Fine-grained states (queued / tool-use / reviewing) and per-agent '
        + 'LLM metrics are not available upstream and are not shown.'),
      spinner());
  },
  async refresh({ signal }) {
    try {
      const d = await api.get('/api/agents', { signal });
      const body = h('div', {});
      body.append(banner(d));
      if ((d.supported_controls || []).length === 0) {
        body.append(h('p', { class: 'muted small' }, 'pause / resume / cancel are not supported by AgentOS, '
          + 'so no agent controls are offered here.'));
      }
      body.append(card('Agents',
        table(['Agent', 'State', 'Source', 'Model / alias', 'Gateway', 'Requests (scheduler)'],
          (d.agents || []).map((a) => [
            h('strong', {}, a.name),
            h('span', { title: a.state_detail || '' }, agentBadge(a.state), ` ${a.state_detail || ''}`.trim()),
            a.source || '—',
            a.model || a.alias || '—',
            a.gateway || '—',
            requestsCell(a.requests),
          ]), { caption: 'Agents', empty: 'No agent is named by either source right now.' })));
      if (d.scheduler_available === false) {
        body.append(h('p', { class: 'muted small' }, 'Scheduler attribution is unavailable right now '
          + '(the orchestrator did not answer its status probe).'));
      }
      if ((d.notes || []).length) {
        body.append(h('details', {}, h('summary', {}, 'Data-source notes'),
          h('ul', { class: 'problems' }, d.notes.map((n) => h('li', { class: 'muted small' }, n)))));
      }
      body.append(h('p', {}, h('a', { class: 'btn btn-ghost btn-sm', href: '#/tasks' }, 'Kanban tasks →')));
      clear(root).append(body);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
