// TASKS: AgentOS kanban boards + cards, plus the scheduler's active/queued
// request records. Upstream has NO dependency graph — cards are flat kanban
// entries and this page keeps them flat (state/AGENTOS-MAP.md).
import { api } from '../api.js';
import {
  h, clear, card, stateBadge, table, errorBox, spinner, ago,
} from '../dom.js';

let root;

const STATUS_ORDER = ['ready', 'todo', 'blocked', 'in_progress', 'done', 'archived'];

function kanbanBoards(cards, counts) {
  const byStatus = new Map(STATUS_ORDER.map((s) => [s, []]));
  for (const c of cards) {
    const s = String(c.status || '').toLowerCase();
    if (!byStatus.has(s)) byStatus.set(s, []);
    byStatus.get(s).push(c);
  }
  const cols = [];
  for (const [status, list] of byStatus) {
    cols.push(h('section', { class: `kanban-col kb-${status.replace(/[^a-z]/g, '_')}`, 'aria-label': `${status} (${list.length})` },
      h('h3', { class: 'kanban-col-title' }, `${status} `, h('span', { class: 'muted' }, `(${list.length})`)),
      h('ul', { class: 'kanban-list' }, list.map((c) => h('li', { class: 'kanban-card' },
        h('strong', {}, c.title || c.id || '(untitled)'),
        h('span', { class: 'muted small' }, [
          c.board ? `board: ${c.board}` : '',
          c.assignee ? ` · assignee: ${c.assignee}` : '',
          c.priority ? ` · priority: ${c.priority}` : '',
        ].join('').replace(/^ ·/, '')),
      )))));
  }
  if (counts) {
    const chips = Object.entries(counts).map(([k, v]) => h('span', { class: 'chip' }, `${k}: ${v}`));
    cols.unshift(h('p', {}, chips));
  }
  return h('div', { class: 'kanban-board' }, cols);
}

export default {
  title: 'Tasks',
  interval: 15,
  async mount(el) {
    root = el;
    clear(root).append(
      h('p', { class: 'lead' }, 'Kanban boards and cards from the AgentOS Control Center, plus the '
        + 'scheduler\'s live request records. There is no dependency graph upstream — cards are flat.'),
      spinner());
  },
  async refresh({ signal }) {
    try {
      const d = await api.get('/api/agents/tasks', { signal });
      const body = h('div', {});
      if (d.agentos_connected !== true) {
        body.append(h('p', { class: 'callout callout-warning', role: 'note' },
          h('strong', {}, 'AgentOS NOT CONNECTED — '),
          'kanban data is unavailable. Only scheduler records are shown below.'));
      }
      const cards = d.kanban_cards || [];
      if (d.agentos_connected === true) {
        body.append(card('Kanban boards',
          cards.length ? kanbanBoards(cards, d.kanban_counts)
            : h('p', { class: 'muted' }, 'No cards on any board right now.'),
          h('p', { class: 'muted small' }, d.note || '')));
      }
      body.append(card('Scheduler request records (active + queued)',
        table(['ID', 'Project', 'Agent', 'Task', 'Priority', 'Profile', 'Reasoning', 'State'],
          (d.scheduler_records || []).map((r) => [
            h('code', {}, r.id || '—'),
            r.project || 'unknown',
            r.agent || '—',
            r.task || '—',
            r.priority ?? '—',
            r.profile || '—',
            r.reasoning || '—',
            stateBadge(r.state || 'unknown'),
          ]), { caption: 'Scheduler records',
            empty: 'No active or queued requests right now (or the scheduler is unavailable).' }),
        d.generated_at ? h('p', { class: 'muted small' }, `Generated ${ago(d.generated_at)}.`) : null));
      clear(root).append(body);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
