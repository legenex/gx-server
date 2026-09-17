// History: every media and music job, newest first, live.
import { clear, h, plural, replace } from '../dom.js';
import { center, isMusic, isVoice, jobCard, jobKind, jobStarted, phaseOf } from '../jobs.js';
import { navigate } from '../nav.js';
import { button, callout, chips, emptyState, pageHeader, skeletonLines } from '../ui.js';

const FILTERS = [['all', 'All'], ['active', 'Active'], ['done', 'Completed'], ['failed', 'Failed']];
const KINDS = [['', 'Everything'], ['image', 'Images'], ['video', 'Video'], ['music', 'Music'], ['voice', 'Voice']];

function openResult(job) {
  const kind = jobKind(job);
  if (isVoice(job)) { navigate('voice', { job: job.id }); return; }
  const ids = isMusic(job) ? job.library_assets || [] : job.assets || [];
  if (ids.length) navigate(kind === 'music' ? 'music' : kind === 'video' ? 'video' : 'images', { asset: ids[0] });
  else navigate('library');
}

export default {
  title: 'History',
  async mount(root) {
    let filter = 'all';
    let kind = '';
    let alive = true;
    let jobs = [];
    const note = h('div', { class: 'stack-sm' });
    const list = h('div', { class: 'history-list', id: 'history-list' }, skeletonLines(4));
    const summary = h('p', { class: 'page-sub', 'aria-live': 'polite' }, 'Loading…');
    const filterChips = chips(FILTERS, { value: filter, label: 'Status', onChange: (v) => { filter = v; render(); } });
    const kindChips = chips(KINDS, { value: kind, label: 'Kind', onChange: (v) => { kind = v; render(); } });
    replace(root,
      pageHeader('History', null, button('Refresh', { icon: 'refresh', size: 'sm', variant: 'ghost', onClick: () => load() })),
      summary,
      h('div', { class: 'filters' }, h('div', { class: 'filters-row' }, filterChips, kindChips)),
      note, list);

    const cards = new Map();
    function render() {
      const merged = new Map(jobs.map((j) => [j.id, j]));
      for (const j of center.all()) merged.set(j.id, j);
      const all = [...merged.values()].sort((a, b) => (jobStarted(b) || 0) - (jobStarted(a) || 0));
      const active = all.filter((j) => !phaseOf(j).terminal).length;
      summary.textContent = `${plural(all.length, 'job')} · ${active} active`;
      const shown = all.filter((j) => {
        const p = phaseOf(j).key;
        if (kind && jobKind(j) !== kind) return false;
        if (filter === 'active') return !phaseOf(j).terminal;
        if (filter === 'done') return p === 'COMPLETE';
        if (filter === 'failed') return p === 'FAILED' || p === 'CANCELLED';
        return true;
      });
      clear(list);
      if (!shown.length) {
        list.append(emptyState({ icon: 'history', title: all.length ? 'No jobs match this filter' : 'No jobs yet', text: all.length ? 'Choose another filter.' : 'Jobs you start in Images, Video, Music or Voice show up here.' }));
        return;
      }
      for (const j of shown.slice(0, 150)) {
        let card = cards.get(j.id);
        if (!card) {
          card = jobCard(j, { onOpen: openResult, onRetry: () => load() });
          cards.set(j.id, card);
        } else {
          card.update(j);
        }
        list.append(card);
      }
    }

    async function load() {
      const res = await center.refreshLists();
      if (!alive || !res) return;
      jobs = res.jobs;
      clear(note);
      if (res.musicError) note.append(callout('warn', 'Music jobs could not be loaded', res.musicError));
      if (res.voiceError) note.append(callout('warn', 'Voice jobs could not be loaded', res.voiceError));
      if (res.mediaError) note.append(callout('warn', 'Image and video jobs could not be loaded', res.mediaError));
      render();
    }

    let pending = null;
    const unsub = center.subscribe(() => {
      if (!alive || pending) return;
      pending = setTimeout(() => { pending = null; if (alive) render(); }, 300);
    });
    await load();
    return () => { alive = false; unsub(); clearTimeout(pending); };
  },
};
