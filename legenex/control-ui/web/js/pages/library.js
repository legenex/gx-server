// MEDIA LIBRARY (D-034): every generated, edited and uploaded image and video.
import { api } from '../api.js';
import {
  h, clear, toast, kv, bytes, ago, confirmDialog, copyButton,
} from '../dom.js';
import { OP_TEXT, assetBadge, mediaEl, metaRows } from './media-common.js';

const PAGE = 48;
let root;
let gridEl;
let barEl;
let statusEl;
let viewer;
const filters = { q: '', type: '', model: '', operation: '', favourite: '', sort: 'newest' };
let items = [];
let total = 0;
let offset = 0;
const selected = new Set();
let facets = { models: [], operations: [] };
let openId = null;

function qs() {
  const p = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
  for (const [k, v] of Object.entries(filters)) if (v !== '') p.set(k, v);
  return p.toString();
}

async function load({ append = false } = {}) {
  const data = await api.get(`/api/media/assets?${qs()}`);
  items = append ? items.concat(data.items) : data.items;
  total = data.total;
  facets = data.facets;
  statusEl.textContent = `${total} item${total === 1 ? '' : 's'} (${data.counts.image} images, ${data.counts.video} videos)`;
  renderGrid();
  renderBar();
  renderFacetOptions();
}

function renderFacetOptions() {
  const fill = (id, values, labels = {}) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const current = sel.value;
    clear(sel).append(h('option', { value: '' }, 'All'),
      ...values.map((v) => h('option', { value: v, selected: v === current }, labels[v] || v)));
  };
  fill('lib-model', facets.models);
  fill('lib-op', facets.operations, OP_TEXT);
}

function tile(a) {
  const checked = selected.has(a.id);
  const box = h('input', {
    type: 'checkbox', class: 'tile-check', checked, 'aria-label': `Select ${a.title || a.prompt || a.id}`,
  });
  box.addEventListener('change', () => {
    if (box.checked) selected.add(a.id); else selected.delete(a.id);
    card.classList.toggle('selected', box.checked);
    renderBar();
  });
  const open = h('button', { type: 'button', class: 'tile-open', 'aria-label': `Open ${a.title || a.prompt || a.id}` },
    mediaEl(a, { thumb: true }),
    a.type === 'video' ? h('span', { class: 'tile-play', 'aria-hidden': 'true' }, '▶') : null);
  open.addEventListener('click', () => openViewer(a.id));
  const card = h('article', { class: `media-tile${checked ? ' selected' : ''}`, role: 'listitem', 'data-id': a.id },
    open, box,
    h('div', { class: 'tile-meta' },
      h('span', { class: 'tile-caption' }, a.title || a.prompt || a.id),
      h('span', { class: 'muted small' }, `${OP_TEXT[a.operation] || a.operation} · ${ago(a.created_at)}`),
      a.favourite ? h('span', { class: 'fav', 'aria-label': 'favourite' }, '★') : null));
  return card;
}

function renderGrid() {
  clear(gridEl);
  if (!items.length) {
    gridEl.append(h('p', { class: 'muted empty' }, 'Nothing here yet. Create something on the ',
      h('a', { href: '#/create' }, 'Create'), ' page.'));
  }
  gridEl.append(...items.map(tile));
  if (items.length < total) {
    gridEl.append(h('button', {
      type: 'button', class: 'btn more',
      onclick: async () => { offset += PAGE; await load({ append: true }); },
    }, `Load more (${total - items.length} left)`));
  }
}

async function bulkDelete(ids) {
  const res = await confirmDialog({
    title: `Delete ${ids.length} item${ids.length === 1 ? '' : 's'}?`,
    body: h('div', {}, h('p', {}, 'The files are removed from gx10-01 permanently. Items created from them stay, '
      + 'but lose the link to their source.'), h('p', {}, `Type DELETE to confirm.`)),
    phrase: 'DELETE', okLabel: 'Delete permanently',
  });
  if (!res.ok) return false;
  const out = await api.post('/api/media/delete', { ids, confirm: true });
  toast(`Deleted ${out.deleted.length}`, 'ok');
  for (const id of out.deleted) selected.delete(id);
  return true;
}

async function zip(ids) {
  const res = await api.post('/api/media/zip', { ids });
  const a = h('a', { href: res.url, download: '' });
  document.body.append(a);
  a.click();
  a.remove();
  toast(`ZIP with ${res.count} item${res.count === 1 ? '' : 's'} (${bytes(res.bytes)})`, 'ok');
}

function renderBar() {
  const n = selected.size;
  clear(barEl).append(
    h('span', { class: 'sel-count', 'aria-live': 'polite' }, `${n} selected`),
    h('button', {
      type: 'button', class: 'btn btn-sm', id: 'lib-select-all',
      onclick: () => { items.forEach((a) => selected.add(a.id)); renderGrid(); renderBar(); },
    }, 'Select all'),
    h('button', {
      type: 'button', class: 'btn btn-sm', id: 'lib-select-none', disabled: !n,
      onclick: () => { selected.clear(); renderGrid(); renderBar(); },
    }, 'Unselect all'),
    h('button', {
      type: 'button', class: 'btn btn-sm', id: 'lib-download-selected', disabled: !n,
      onclick: () => zip([...selected]).catch((e) => toast(e.message, 'crit')),
    }, 'Download selected (ZIP)'),
    h('button', {
      type: 'button', class: 'btn btn-sm btn-danger', id: 'lib-delete-selected', disabled: !n,
      onclick: async () => { try { if (await bulkDelete([...selected])) await reload(); } catch (e) { toast(e.message, 'crit'); } },
    }, 'Delete selected'));
}

async function reload() {
  offset = 0;
  await load();
}

function lineageList(title, rows) {
  if (!rows || !rows.length) return null;
  return h('div', { class: 'lineage' }, h('h3', {}, title), h('ul', {}, rows.map((r) => h('li', {},
    r.deleted ? h('span', { class: 'muted' }, `${r.id} (deleted)`)
      : h('button', { type: 'button', class: 'link-btn', onclick: () => openViewer(r.id) },
        `${OP_TEXT[r.operation] || r.operation}: ${r.title || r.prompt || r.id}`)))));
}

async function openViewer(id) {
  openId = id;
  history.replaceState(null, '', `#/library/${id}`);
  let a;
  try {
    a = await api.get(`/api/media/assets/${id}`);
  } catch (e) {
    toast(e.message, 'crit');
    return;
  }
  const titleInput = h('input', { id: 'viewer-title', value: a.title || '', maxlength: 200, 'aria-label': 'Title' });
  const saveTitle = h('button', {
    type: 'button', class: 'btn btn-sm',
    onclick: async () => {
      try { await api.post(`/api/media/assets/${a.id}`, { title: titleInput.value }); toast('Title saved'); await load(); } catch (e) { toast(e.message, 'crit'); }
    },
  }, 'Rename');
  const fav = h('button', {
    type: 'button', class: 'btn btn-sm', id: 'viewer-fav', 'aria-pressed': String(a.favourite),
    onclick: async () => {
      a = await api.post(`/api/media/assets/${a.id}`, { favourite: !a.favourite });
      fav.textContent = a.favourite ? '★ Unfavourite' : '☆ Favourite';
      fav.setAttribute('aria-pressed', String(a.favourite));
      await load();
    },
  }, a.favourite ? '★ Unfavourite' : '☆ Favourite');
  const actions = h('div', { class: 'btn-row' },
    h('a', { class: 'btn btn-sm', href: a.download_url, download: '', id: 'viewer-download' }, 'Download'),
    fav,
    a.prompt ? copyButton(a.prompt, 'Copy prompt') : null);
  if (a.type === 'image') {
    actions.append(
      h('a', { class: 'btn btn-sm btn-primary', href: `#/create/edit?source=${a.id}` }, 'Edit with AI'),
      h('button', {
        type: 'button', class: 'btn btn-sm', id: 'viewer-variation',
        onclick: async () => {
          try {
            await api.post('/api/media/jobs', { kind: 'variation', source_id: a.id });
            toast('Variation queued: follow it on the Create page', 'ok', 7000);
          } catch (e) { toast(e.message, 'crit'); }
        },
      }, 'Generate variation'),
      h('a', { class: 'btn btn-sm', href: `#/create/i2v?source=${a.id}` }, 'Make video'));
  } else {
    actions.append(h('a', { class: 'btn btn-sm btn-primary', href: `#/create/vedit?source=${a.id}` }, 'Edit with AI'));
  }
  actions.append(h('button', {
    type: 'button', class: 'btn btn-sm btn-danger', id: 'viewer-delete',
    onclick: async () => {
      try { if (await bulkDelete([a.id])) { viewer.close(); await reload(); } } catch (e) { toast(e.message, 'crit'); }
    },
  }, 'Delete'));
  const parentNote = a.parent_id
    ? (a.parent_deleted ? h('p', { class: 'muted small' }, `Source ${a.parent_id} was deleted.`)
      : h('button', { type: 'button', class: 'btn btn-sm btn-ghost', id: 'viewer-parent', onclick: () => openViewer(a.parent_id) }, 'Open parent'))
    : null;
  clear(viewer).append(
    h('div', { class: 'viewer-head' },
      h('h2', { id: 'viewer-heading' }, a.title || OP_TEXT[a.operation] || a.id),
      assetBadge(a),
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Close', onclick: () => viewer.close() }, '✕')),
    h('div', { class: 'viewer-body' },
      h('div', { class: 'viewer-media' }, mediaEl(a)),
      h('div', { class: 'viewer-side' },
        a.prompt ? h('div', {}, h('h3', {}, a.operation === 'upload' ? 'Note' : 'Prompt'), h('p', { class: 'prompt-line' }, a.prompt)) : null,
        a.negative_prompt ? h('p', { class: 'small' }, `Negative: ${a.negative_prompt}`) : null,
        h('div', { class: 'btn-row' }, titleInput, saveTitle),
        actions,
        parentNote,
        lineageList('Source chain', a.ancestors),
        lineageList(`Versions made from this (${(a.children || []).length})`, a.children),
        h('details', { open: true }, h('summary', {}, 'Metadata'), kv(metaRows(a))),
        a.settings && Object.keys(a.settings).length
          ? h('details', {}, h('summary', {}, 'Generation settings'), h('pre', { class: 'code' }, JSON.stringify(a.settings, null, 2)))
          : null)));
  if (!viewer.open) viewer.showModal();
}

export default {
  title: 'Media Library',
  interval: 0,
  async mount(el, { params }) {
    root = el;
    selected.clear();
    offset = 0;
    viewer = h('dialog', { class: 'viewer', 'aria-labelledby': 'viewer-heading' });
    viewer.addEventListener('close', () => { if (openId) { openId = null; history.replaceState(null, '', '#/library'); } });
    const search = h('input', { type: 'search', id: 'lib-search', placeholder: 'Search prompts and titles', value: filters.q });
    let t;
    search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { filters.q = search.value.trim(); reload(); }, 300); });
    const sel = (id, label, key, opts) => {
      const s = h('select', { id }, opts.map(([v, l]) => h('option', { value: v, selected: filters[key] === v }, l)));
      s.addEventListener('change', () => { filters[key] = s.value; reload(); });
      return h('div', { class: 'field inline' }, h('label', { for: id }, label), s);
    };
    statusEl = h('p', { class: 'muted', role: 'status' });
    barEl = h('div', { class: 'selection-bar', role: 'toolbar', 'aria-label': 'Selection' });
    gridEl = h('div', { class: 'media-grid', role: 'list', 'aria-label': 'Media' });
    root.append(
      h('p', { class: 'lead' }, 'Everything created or uploaded through this interface, stored on gx10-01 in ',
        h('code', {}, '/srv/projects/gx-cluster/media'), '. Nothing is ever overwritten: edits are new items linked to their source.'),
      h('div', { class: 'filters' },
        h('div', { class: 'field inline grow' }, h('label', { for: 'lib-search' }, 'Search'), search),
        sel('lib-type', 'Type', 'type', [['', 'All'], ['image', 'Images'], ['video', 'Videos']]),
        sel('lib-model', 'Model', 'model', [['', 'All']]),
        sel('lib-op', 'Operation', 'operation', [['', 'All']]),
        sel('lib-fav', 'Favourites', 'favourite', [['', 'All'], ['1', 'Favourites only']]),
        sel('lib-sort', 'Sort', 'sort', [['newest', 'Newest'], ['oldest', 'Oldest'], ['title', 'Title'], ['size', 'Largest']])),
      statusEl, barEl, gridEl, viewer);
    await load();
    const target = params && params[0] ? params[0].split('?')[0] : null;
    if (target && /^a_[0-9a-f]{24}$/.test(target)) openViewer(target);
  },
  unmount() {
    if (viewer && viewer.open) viewer.close();
  },
};
