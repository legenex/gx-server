// Library: every image, video and track, with search, filters, bulk actions.
import { api, searchAssets, updateAsset } from '../api.js';
import { audioPlayer } from '../audio.js';
import { OP_LABEL, TYPE_LABEL, deleteWithConfirm, detailsDrawer, downloadButtons, duplicateAsset, emitAsset, lightbox, lineageDrawer, onAsset, recipeOf, renameAsset, toggleFavourite } from '../assets.js';
import { ago, bytes, clear, debounce, h, mmss, plural, storeGet, storeSet, titleOf, toast, replace } from '../dom.js';
import { icon } from '../icons.js';
import { openAsset } from '../nav.js';
import { assetThumb, button, callout, chips, emptyState, iconButton, pageHeader, select, skeletonGrid, textInput, toggle } from '../ui.js';

const PAGE = 24;
const SORTS = [['newest', 'Newest first'], ['oldest', 'Oldest first'], ['title', 'Title A–Z'], ['size', 'Largest first'], ['duration', 'Longest first']];

export default {
  title: 'Library',
  async mount(root, ctx) {
    const f = {
      q: ctx.query.q || '', type: ['image', 'video', 'audio'].includes(ctx.query.type) ? ctx.query.type : '',
      model: '', operation: '', favourite: false, sort: storeGet('lib.sort', 'newest'),
    };
    let view = storeGet('lib.view', 'grid');
    let items = [];
    let total = 0;
    let offset = 0;
    let facets = { models: [], operations: [] };
    let counts = { image: 0, video: 0, audio: 0 };
    const selected = new Map();
    let alive = true;
    let seq = 0;

    const countLine = h('p', { class: 'page-sub', id: 'lib-counts' }, '…');
    const viewBtns = h('div', { class: 'seg', role: 'group', 'aria-label': 'View' },
      iconButton('grid', 'Grid view', () => setView('grid'), { pressed: view === 'grid', attrs: { 'data-view': 'grid' } }),
      iconButton('list', 'List view', () => setView('list'), { pressed: view === 'list', attrs: { 'data-view': 'list' } }));

    const search = textInput({ value: f.q, placeholder: 'Search prompts, titles, lyrics, tags…', attrs: { type: 'search', id: 'lib-search', 'aria-label': 'Search the Library' } });
    const typeChips = chips([['', 'All'], ['image', 'Images'], ['video', 'Videos'], ['audio', 'Audio']], { value: f.type, label: 'Type', cls: 'chips-type' });
    const modelSel = select([['', 'All models']], '', { attrs: { id: 'lib-model', 'aria-label': 'Model' } });
    const opSel = select([['', 'All operations']], '', { attrs: { id: 'lib-op', 'aria-label': 'Operation' } });
    const sortSel = select(SORTS, f.sort, { attrs: { id: 'lib-sort', 'aria-label': 'Sort' } });
    const favToggle = toggle('Favourites only', false);

    const selectAll = h('input', { type: 'checkbox', id: 'lib-select-all', class: 'check' });
    const selCount = h('span', { class: 'sel-count', 'aria-live': 'polite' }, 'Nothing selected');
    const bulk = {
      clear: button('Clear selection', { size: 'sm', variant: 'ghost', icon: 'x', attrs: { id: 'bulk-clear' }, onClick: () => { selected.clear(); syncSelection(); } }),
      fav: button('Favourite', { size: 'sm', icon: 'heart', attrs: { id: 'bulk-fav' }, onClick: () => bulkFavourite(true) }),
      unfav: button('Unfavourite', { size: 'sm', icon: 'heart', variant: 'ghost', attrs: { id: 'bulk-unfav' }, onClick: () => bulkFavourite(false) }),
      zip: button('Download ZIP', { size: 'sm', icon: 'download', attrs: { id: 'bulk-zip' }, onClick: () => bulkZip() }),
      del: button('Delete', { size: 'sm', icon: 'trash', variant: 'danger-ghost', attrs: { id: 'bulk-delete' }, onClick: () => bulkDelete() }),
    };
    const selBar = h('div', { class: 'selbar', role: 'region', 'aria-label': 'Selection' },
      h('label', { class: 'check-label', for: 'lib-select-all' }, selectAll, h('span', {}, 'Select all visible')),
      selCount, h('div', { class: 'selbar-actions' }, Object.values(bulk)));

    const gridBox = h('div', { id: 'lib-results', class: 'lib-results' });
    const more = button('Load more', { variant: 'secondary', attrs: { id: 'lib-more' }, onClick: () => load(false) });
    const moreNote = h('p', { class: 'muted small center' });

    replace(root,
      pageHeader('Library', null, viewBtns),
      countLine,
      h('div', { class: 'filters', role: 'search' },
        h('div', { class: 'filters-row' }, h('div', { class: 'search-wrap' }, icon('search', { size: 16 }), search), typeChips),
        h('div', { class: 'filters-row' }, modelSel, opSel, sortSel, favToggle)),
      selBar, gridBox, h('div', { class: 'center stack-sm' }, moreNote, more));

    function setView(v) {
      view = v;
      storeSet('lib.view', v);
      for (const b of viewBtns.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.view === v));
      render();
    }

    function syncSelection() {
      const n = selected.size;
      selCount.textContent = n ? `${plural(n, 'item')} selected` : 'Nothing selected';
      for (const b of [bulk.clear, bulk.fav, bulk.unfav, bulk.zip, bulk.del]) b.disabled = !n;
      const visible = items.length;
      const allOn = visible > 0 && items.every((a) => selected.has(a.id));
      selectAll.checked = allOn;
      selectAll.indeterminate = !allOn && items.some((a) => selected.has(a.id));
      for (const cb of gridBox.querySelectorAll('input.item-check')) {
        cb.checked = selected.has(cb.dataset.id);
        cb.closest('.lib-item').classList.toggle('is-selected', cb.checked);
      }
    }

    selectAll.addEventListener('change', () => {
      if (selectAll.checked) for (const a of items) selected.set(a.id, a);
      else for (const a of items) selected.delete(a.id);
      syncSelection();
    });

    function itemActions(a) {
      return [
        iconButton('sparkles', 'Open in workspace', () => openAsset(a), { attrs: { 'data-action': 'open' } }),
        a.type !== 'audio' ? iconButton('expand', 'Preview', () => lightbox([a], 0), { attrs: { 'data-action': 'preview' } }) : null,
        iconButton('edit', 'Rename', () => renameAsset(a), { attrs: { 'data-action': 'rename' } }),
        recipeOf(a) ? iconButton('copy', 'Duplicate (run the recipe again)', () => duplicateAsset(a), { attrs: { 'data-action': 'duplicate' } }) : null,
        iconButton('layers', 'Lineage', () => lineageDrawer(a), { attrs: { 'data-action': 'lineage' } }),
        iconButton('info', 'Details', () => detailsDrawer(a), { attrs: { 'data-action': 'details' } }),
        iconButton('trash', 'Delete', () => deleteWithConfirm(a), { attrs: { class: 'icon-btn icon-btn-ghost danger', 'data-action': 'delete' } }),
      ].filter(Boolean);
    }

    function favBtn(a) {
      return iconButton('heart', a.favourite ? `Remove ${titleOf(a)} from favourites` : `Add ${titleOf(a)} to favourites`, () => toggleFavourite(a),
        { pressed: a.favourite, attrs: { class: `icon-btn icon-btn-ghost fav-btn${a.favourite ? ' is-on' : ''}`, 'data-action': 'favourite' } });
    }

    function checkbox(a) {
      const cb = h('input', { type: 'checkbox', class: 'check item-check', dataset: { id: a.id }, 'aria-label': `Select ${titleOf(a)}` });
      cb.checked = selected.has(a.id);
      cb.addEventListener('change', () => {
        if (cb.checked) selected.set(a.id, a); else selected.delete(a.id);
        syncSelection();
      });
      return cb;
    }

    function meta(a) {
      return [TYPE_LABEL[a.type], OP_LABEL[a.operation] || a.operation,
        a.duration ? mmss(a.duration) : null, a.width && a.height ? `${a.width}×${a.height}` : null,
        bytes(a.file_size), ago(a.created_at)].filter(Boolean).join(' · ');
    }

    function gridItem(a) {
      const media = a.type === 'audio'
        ? h('div', { class: 'lib-audio' }, audioPlayer(a, { label: titleOf(a) }))
        : h('button', { type: 'button', class: 'lib-media', 'aria-label': `Preview ${titleOf(a)}`, onclick: () => lightbox([a], 0) },
          assetThumb(a), a.type === 'video' ? h('span', { class: 'tile-play', 'aria-hidden': 'true' }, icon('play', { size: 16 })) : null);
      return h('li', { class: `lib-item lib-card lib-type-${a.type}`, dataset: { asset: a.id } },
        h('div', { class: 'lib-card-top' }, checkbox(a), h('span', { class: `type-pill type-${a.type}` }, TYPE_LABEL[a.type]), favBtn(a)),
        media,
        h('div', { class: 'lib-card-body' },
          h('p', { class: 'lib-title' }, titleOf(a)),
          h('p', { class: 'muted xsmall' }, meta(a))),
        h('div', { class: 'lib-card-actions' }, itemActions(a), a.type !== 'audio' ? downloadButtons(a) : downloadButtons(a).slice(0, 1)));
    }

    function listItem(a) {
      return h('li', { class: `lib-item lib-row lib-type-${a.type}`, dataset: { asset: a.id } },
        checkbox(a),
        h('div', { class: 'lib-row-thumb' }, a.type === 'audio' ? h('div', { class: 'thumb-ph thumb-ph-audio' }, icon('music', { size: 18 })) : assetThumb(a)),
        h('div', { class: 'lib-row-main' },
          h('p', { class: 'lib-title' }, titleOf(a)),
          h('p', { class: 'muted xsmall' }, meta(a)),
          a.type === 'audio' ? audioPlayer(a, { label: titleOf(a) }) : null),
        h('div', { class: 'lib-row-actions' }, favBtn(a), itemActions(a), downloadButtons(a).slice(0, 1)));
    }

    function render() {
      clear(gridBox);
      if (!items.length) {
        const filtered = f.q || f.type || f.model || f.operation || f.favourite;
        gridBox.append(emptyState({
          icon: filtered ? 'search' : 'library',
          title: filtered ? 'Nothing matches these filters' : 'Your Library is empty',
          text: filtered ? 'Try a different search or clear the filters.' : 'Everything you create or upload lands here.',
          action: filtered ? button('Clear filters', { size: 'sm', onClick: () => resetFilters() }) : null,
        }));
      } else {
        gridBox.append(h('ul', { class: view === 'grid' ? 'lib-grid' : 'lib-list', 'aria-label': 'Library items' }, items.map(view === 'grid' ? gridItem : listItem)));
      }
      more.hidden = offset >= total;
      moreNote.textContent = total ? `Showing ${items.length} of ${total}` : '';
      syncSelection();
    }

    function renderCounts() {
      countLine.textContent = `${plural(counts.image || 0, 'image')} · ${plural(counts.video || 0, 'video')} · ${plural(counts.audio || 0, 'track')}`;
      const labels = { '': 'All', image: 'Images', video: 'Videos', audio: 'Audio' };
      const totalAll = (counts.image || 0) + (counts.video || 0) + (counts.audio || 0);
      for (const b of typeChips.children) {
        const v = b.dataset.value;
        b.textContent = labels[v];
        b.append(h('span', { class: 'chip-extra' }, String(v ? counts[v] || 0 : totalAll)));
      }
    }

    function fillSelect(sel, values, allLabel, labeler) {
      const cur = sel.value;
      replace(sel, h('option', { value: '' }, allLabel), ...values.map((v) => h('option', { value: v }, labeler(v))));
      sel.value = values.includes(cur) ? cur : '';
    }

    async function load(reset) {
      const my = ++seq;
      if (reset) { offset = 0; replace(gridBox, skeletonGrid(8, 'lib-grid')); }
      more.disabled = true;
      try {
        const res = await searchAssets({
          q: f.q, type: f.type, model: f.model, operation: f.operation, favourite: f.favourite ? 1 : '',
          sort: f.sort, limit: PAGE, offset,
        });
        if (!alive || my !== seq) return;
        items = reset ? res.items : [...items, ...res.items.filter((x) => !items.find((y) => y.id === x.id))];
        total = res.total;
        offset = items.length;
        facets = res.facets || facets;
        counts = res.counts || counts;
        fillSelect(modelSel, facets.models || [], 'All models', (v) => v);
        fillSelect(opSel, facets.operations || [], 'All operations', (v) => OP_LABEL[v] || v);
        renderCounts();
        render();
      } catch (err) {
        if (!alive || my !== seq) return;
        replace(gridBox, callout('danger', 'The Library could not be loaded', err.message, [button('Try again', { size: 'sm', onClick: () => load(true) })]));
      } finally {
        more.disabled = false;
      }
    }

    function resetFilters() {
      Object.assign(f, { q: '', type: '', model: '', operation: '', favourite: false });
      search.value = '';
      typeChips.setValue('');
      modelSel.value = '';
      opSel.value = '';
      favToggle.input.checked = false;
      load(true);
    }

    async function bulkFavourite(on) {
      const list = [...selected.values()];
      let done = 0;
      for (const a of list) {
        try { const next = await updateAsset(a.id, { favourite: on }); emitAsset(next); done += 1; } catch (err) { toast(err.message, 'danger'); break; }
      }
      toast(`${plural(done, 'item')} ${on ? 'added to' : 'removed from'} favourites.`, 'ok');
      if (f.favourite) load(true);
    }

    async function bulkZip() {
      const ids = [...selected.keys()];
      if (!ids.length) return;
      bulk.zip.disabled = true;
      try {
        const res = await api.post('/api/media/zip', { ids }, { timeout: 300_000 });
        toast(`ZIP ready: ${plural(res.count, 'item')}, ${bytes(res.bytes)}. Downloading…`, 'ok');
        const a = h('a', { href: res.url, download: '', hidden: true });
        document.body.append(a);
        a.click();
        a.remove();
      } catch (err) {
        toast(err.message, 'danger');
      } finally {
        bulk.zip.disabled = !selected.size;
      }
    }

    async function bulkDelete() {
      const list = [...selected.values()];
      if (await deleteWithConfirm(list)) {
        selected.clear();
        load(true);
      }
    }

    search.addEventListener('input', debounce(() => { f.q = search.value.trim(); load(true); }, 300));
    typeChips.addEventListener('click', () => {
      const v = typeChips.getValue();
      if (v !== f.type) { f.type = v; load(true); }
    });
    modelSel.addEventListener('change', () => { f.model = modelSel.value; load(true); });
    opSel.addEventListener('change', () => { f.operation = opSel.value; load(true); });
    sortSel.addEventListener('change', () => { f.sort = sortSel.value; storeSet('lib.sort', f.sort); load(true); });
    favToggle.input.addEventListener('change', () => { f.favourite = favToggle.input.checked; load(true); });

    const unsub = onAsset((a, deleted) => {
      if (!alive) return;
      if (deleted) {
        items = items.filter((x) => x.id !== a.id);
        selected.delete(a.id);
        total = Math.max(0, total - 1);
        offset = items.length;
        if (counts[a.type]) counts[a.type] -= 1;
        renderCounts();
        render();
        return;
      }
      const i = items.findIndex((x) => x.id === a.id);
      if (i >= 0) {
        items[i] = a;
        if (selected.has(a.id)) selected.set(a.id, a);
        const node = gridBox.querySelector(`[data-asset="${a.id}"]`);
        if (node) node.replaceWith((view === 'grid' ? gridItem : listItem)(a));
        syncSelection();
      }
    });

    await load(true);
    return () => { alive = false; unsub(); };
  },
};
