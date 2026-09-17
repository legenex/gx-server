// The stage shared by the Images and Video workspaces: job list, viewer,
// session results and the history filmstrip.
import { getAsset, searchAssets } from './api.js';
import { OP_LABEL, compareDialog, deleteWithConfirm, detailsDrawer, downloadButtons, lightbox, mediaView, onAsset, renameAsset, toggleFavourite } from './assets.js';
import { ago, clear, h, titleOf, toast, append } from './dom.js';
import { icon } from './icons.js';
import { center, isTerminal, jobKind, phaseOf, jobCard } from './jobs.js';
import { assetThumb, emptyState, iconButton, skeletonGrid } from './ui.js';

// session: module-level { results: [asset], jobs: [jobId], done: Set(jobId), selectedId, compareId }
export function createStage({ type, session, actions, emptyTitle, emptyText }) {
  const alias = type === 'video' ? 'video' : 'image';
  const jobsBox = h('div', { class: 'ws-jobs', id: 'ws-jobs', 'aria-live': 'polite' });
  const viewer = h('section', { class: 'viewer', id: 'viewer', 'aria-label': 'Selected result' });
  const grid = h('ul', { class: 'result-grid', id: 'session-results', 'aria-label': 'Results from this session' });
  const gridWrap = h('section', { class: 'ws-section', 'aria-labelledby': `${type}-session-h` },
    h('h2', { class: 'section-title', id: `${type}-session-h` }, 'This session'), grid);
  const strip = h('ul', { class: 'filmstrip', id: 'filmstrip', 'aria-label': `Recent ${type}s` });
  const stripWrap = h('section', { class: 'ws-section', 'aria-labelledby': `${type}-strip-h` },
    h('div', { class: 'row-between' }, h('h2', { class: 'section-title', id: `${type}-strip-h` }, 'History'),
      h('a', { class: 'link small', href: `#/library?type=${type}` }, 'Open in Library')),
    strip);
  const el = h('div', { class: 'stage' }, jobsBox, viewer, gridWrap, stripWrap);
  const cards = new Map();
  let recent = [];
  let alive = true;

  const selected = () => session.results.find((a) => a.id === session.selectedId)
    || recent.find((a) => a.id === session.selectedId) || null;

  function renderViewer() {
    const a = selected();
    clear(viewer);
    if (!a) {
      viewer.append(emptyState({ icon: type === 'video' ? 'film' : 'image', title: emptyTitle, text: emptyText }));
      viewer.classList.add('viewer-empty');
      return;
    }
    viewer.classList.remove('viewer-empty');
    const compareSrc = session.compareId ? [...session.results, ...recent].find((x) => x.id === session.compareId) : null;
    const tools = [
      iconButton('heart', a.favourite ? 'Remove from favourites' : 'Add to favourites', async () => update(await toggleFavourite(a)),
        { pressed: a.favourite, attrs: { class: `icon-btn icon-btn-ghost fav-btn${a.favourite ? ' is-on' : ''}`, 'data-action': 'favourite' } }),
      iconButton('edit', 'Rename', async () => update(await renameAsset(a)), { attrs: { 'data-action': 'rename' } }),
      type === 'image' ? iconButton('compare', compareSrc && compareSrc.id !== a.id ? `Compare with ${titleOf(compareSrc)}` : 'Compare: mark this image, then pick another', () => {
        if (session.compareId && session.compareId !== a.id && compareSrc) {
          compareDialog(compareSrc, a);
          session.compareId = null;
        } else {
          session.compareId = a.id;
          toast('Marked for comparison. Select another image and press Compare again.', 'ok');
        }
        renderViewer();
      }, { pressed: session.compareId === a.id, attrs: { 'data-action': 'compare' } }) : null,
      iconButton('expand', 'Fullscreen', () => {
        const inSession = session.results.some((x) => x.id === a.id);
        const list = inSession ? session.results : [a];
        lightbox(list, list.findIndex((x) => x.id === a.id));
      }, { attrs: { 'data-action': 'fullscreen' } }),
      iconButton('info', 'Details and lineage', () => detailsDrawer(a), { attrs: { 'data-action': 'details' } }),
      iconButton('trash', 'Delete', async () => { await deleteWithConfirm(a); }, { attrs: { class: 'icon-btn icon-btn-ghost danger', 'data-action': 'delete' } }),
    ].filter(Boolean);
    const media = type === 'image'
      ? h('button', { type: 'button', class: 'viewer-media-btn', 'aria-label': `Open ${titleOf(a)} fullscreen`, onclick: () => lightbox([a], 0) }, mediaView(a))
      : mediaView(a);
    append(viewer, [
      h('div', { class: 'viewer-media' }, media),
      h('div', { class: 'viewer-bar' },
        h('div', { class: 'viewer-meta' },
          h('p', { class: 'viewer-title', id: 'viewer-title' }, titleOf(a)),
          h('p', { class: 'muted small' }, [OP_LABEL[a.operation] || a.operation,
            a.width && a.height ? `${a.width}×${a.height}` : null,
            a.seed !== null && a.seed !== undefined ? `seed ${a.seed}` : null, ago(a.created_at)].filter(Boolean).join(' · '))),
        h('div', { class: 'viewer-tools', role: 'toolbar', 'aria-label': 'Result actions' }, tools)),
      h('div', { class: 'viewer-actions' }, actions(a), downloadButtons(a)),
      a.prompt ? h('p', { class: 'viewer-prompt' }, a.prompt) : null,
      session.compareId === a.id ? h('p', { class: 'hint' }, icon('compare', { size: 14 }), ' Marked for comparison: select another image and press Compare.') : null]);
  }

  function tile(a, list) {
    const on = a.id === session.selectedId;
    return h('li', { class: `result-tile${on ? ' is-selected' : ''}`, dataset: { asset: a.id } },
      h('button', { type: 'button', class: 'result-btn', 'aria-pressed': String(on), 'aria-label': `Select ${titleOf(a)}`, onclick: () => select(a) },
        assetThumb(a), a.type === 'video' ? h('span', { class: 'tile-play', 'aria-hidden': 'true' }, icon('play', { size: 14 })) : null,
        a.favourite ? h('span', { class: 'tile-fav', 'aria-hidden': 'true' }, icon('heart', { size: 12, cls: 'ic-fill' })) : null),
      list === 'grid' ? h('span', { class: 'result-cap' }, titleOf(a)) : null);
  }

  function renderGrid() {
    clear(grid);
    gridWrap.hidden = !session.results.length;
    for (const a of session.results) grid.append(tile(a, 'grid'));
  }

  function renderStrip() {
    clear(strip);
    if (!recent.length) {
      strip.append(h('li', { class: 'muted small strip-empty' }, `No ${type}s in the Library yet.`));
      return;
    }
    for (const a of recent) strip.append(tile(a, 'strip'));
  }

  async function loadStrip() {
    strip.replaceChildren(skeletonGrid(8, 'strip-skel'));
    const keep = selected();
    try {
      const res = await searchAssets({ type, limit: 30, sort: 'newest' });
      if (!alive) return;
      recent = res.items || [];
    } catch {
      recent = [];
    }
    if (keep && !recent.find((x) => x.id === keep.id) && !session.results.find((x) => x.id === keep.id)) recent.unshift(keep);
    renderStrip();
    if (!selected() && session.selectedId) renderViewer();
  }

  function select(a) {
    session.selectedId = a.id;
    if (!session.results.find((x) => x.id === a.id) && !recent.find((x) => x.id === a.id)) recent.unshift(a);
    renderViewer();
    renderGrid();
    renderStrip();
  }

  function update(a) {
    if (!a) return;
    const swap = (list) => list.map((x) => (x.id === a.id ? a : x));
    session.results = swap(session.results);
    recent = swap(recent);
    renderViewer();
    renderGrid();
    renderStrip();
  }

  function remove(id) {
    session.results = session.results.filter((x) => x.id !== id);
    recent = recent.filter((x) => x.id !== id);
    if (session.selectedId === id) session.selectedId = session.results[0] ? session.results[0].id : (recent[0] ? recent[0].id : null);
    if (session.compareId === id) session.compareId = null;
    renderViewer();
    renderGrid();
    renderStrip();
  }

  async function addResults(ids) {
    const fresh = [];
    for (const id of ids) {
      if (session.results.find((x) => x.id === id)) continue;
      try { fresh.push(await getAsset(id)); } catch { /* deleted meanwhile */ }
    }
    if (!fresh.length || !alive) return;
    session.results = [...fresh, ...session.results].slice(0, 48);
    recent = [...fresh.filter((f) => !recent.find((r) => r.id === f.id)), ...recent];
    select(fresh[0]);
  }

  function showJob(job) {
    if (cards.has(job.id)) return;
    const card = jobCard(job, { onRetry: (next) => trackJob(next) });
    cards.set(job.id, card);
    jobsBox.prepend(card);
    while (jobsBox.children.length > 4) {
      const last = jobsBox.lastElementChild;
      cards.delete(last.dataset.job);
      last.remove();
    }
  }

  function trackJob(job) {
    if (!session.jobs.includes(job.id)) session.jobs.unshift(job.id);
    session.jobs = session.jobs.slice(0, 20);
    showJob(job);
  }

  const handled = session.done;
  const onJob = (job) => {
    if (!alive || !job || jobKind(job) !== alias) return;
    if (!session.jobs.includes(job.id)) {
      if (isTerminal(job)) return;
      trackJob(job); // started elsewhere (dashboard, command bar, library)
    }
    if (phaseOf(job).key === 'COMPLETE' && !handled.has(job.id)) {
      handled.add(job.id);
      addResults(job.assets || []);
    }
  };
  const unsubJobs = center.subscribe(onJob);
  const unsubAssets = onAsset((a, deleted) => { if (deleted) remove(a.id); else if (a.type === type) update(a); });

  // Restore the session.
  for (const id of session.jobs.slice().reverse()) {
    const j = center.jobs.get(id);
    if (j) {
      showJob(j);
      onJob(j);
    }
  }
  for (const j of center.active()) onJob(j);
  renderViewer();
  renderGrid();
  loadStrip();

  return {
    el,
    trackJob,
    select,
    selected,
    update,
    destroy() { alive = false; unsubJobs(); unsubAssets(); },
  };
}
