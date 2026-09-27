// PROJECTS: the projects_scanner output (depth-2 scan of the projects root)
// with scheduler attribution per project. Git facts and sizes are measured,
// never guessed; a size can be re-measured on demand (audited POST).
import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, table, errorBox, spinner, bytes, clock, ago, toast,
} from '../dom.js';

let root;

function gitFacts(p) {
  if (!p.is_git) return h('span', { class: 'muted small' }, 'not a git repository');
  return kv([
    ['Branch', p.branch || '—'],
    ['HEAD', p.head ? h('code', {}, String(p.head).slice(0, 12)) : '—'],
    ['Remote', p.remote ? h('code', { class: 'small' }, p.remote) : '—'],
    ['Dirty', p.dirty ? stateBadge('warn', `${p.dirty_files} file(s)`) : stateBadge('ok', 'clean')],
    ['Last commit', p.last_commit ? `${p.last_commit.subject}${p.last_commit.ts ? ` (${clock(p.last_commit.ts)})` : ''}` : '—'],
  ]);
}

function projectCard(p) {
  const sizeBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'data-size': p.name },
    'Re-measure size');
  sizeBtn.addEventListener('click', async () => {
    sizeBtn.disabled = true;
    sizeBtn.textContent = 'Measuring…';
    try {
      const rec = await api.post(`/api/projects/${encodeURIComponent(p.name)}/size`, {});
      toast(`Measured ${p.name}: ${bytes(rec.bytes)}`, 'ok');
    } catch (err) {
      toast(`Size refresh failed: ${err.message}`, 'crit');
    } finally {
      sizeBtn.disabled = false;
      sizeBtn.textContent = 'Re-measure size';
    }
  });
  const sched = p.scheduler
    ? `active ${p.scheduler.active} · queued ${p.scheduler.queued}`
    : 'no scheduler attribution right now';
  return h('section', { class: 'card project-card', 'aria-labelledby': `pj-${p.name}` },
    h('div', { class: 'card-head' },
      h('h2', { class: 'card-title', id: `pj-${p.name}` }, p.name),
      p.scheduler && (p.scheduler.active || p.scheduler.queued)
        ? stateBadge('ok', `${p.scheduler.active} active / ${p.scheduler.queued} queued`) : null),
    h('p', { class: 'muted small' }, h('code', {}, p.path)),
    kv([
      ['Size on disk', p.size_bytes !== null && p.size_bytes !== undefined
        ? `${bytes(p.size_bytes)}${p.size_measured_at ? ` (measured ${ago(p.size_measured_at)})` : ''}` : 'not measured yet'],
      ['Last modified', p.mtime ? clock(p.mtime) : '—'],
      ['Scheduler requests', sched],
      ['Children', Array.isArray(p.children) && p.children.length ? p.children.join(', ') : (p.is_git ? '— (git repository)' : '—')],
    ]),
    gitFacts(p),
    h('div', { class: 'btn-row' },
      h('a', { class: 'btn btn-ghost btn-sm', href: `#/files` }, 'Browse in Files →'), sizeBtn));
}

export default {
  title: 'Projects',
  interval: 0,
  async mount(el) {
    root = el;
    clear(root).append(
      h('p', { class: 'lead' }, 'Every project under the projects root (depth 2): git state, last commit, '
        + 'cached size and the scheduler\'s per-project request attribution.'),
      spinner());
    const d = await api.get('/api/projects');
    const grid = h('div', { class: 'grid grid-2' });
    if (!(d.projects || []).length) {
      grid.append(h('p', { class: 'muted' }, 'No projects found under the scanned root.'));
    }
    for (const p of d.projects) grid.append(projectCard(p));
    clear(root).append(
      h('p', { class: 'muted small' }, `Root: ${d.root}`),
      grid,
      card('Per-project request stats',
        table(['Project', 'Active', 'Queued'], (d.projects || []).map((p) => [
          p.name,
          p.scheduler ? String(p.scheduler.active ?? '—') : '—',
          p.scheduler ? String(p.scheduler.queued ?? '—') : '—',
        ]), { caption: 'Scheduler attribution per project',
          empty: 'The scheduler is not attributing requests to any project right now.' })));
  },
};
