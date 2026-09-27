// FILES: the safe file manager over the six allowed roots. DELETE never
// deletes: it moves items to the trash (manifest + restore). PURGE is a
// separate, typed-confirmation, audited operation. All path validation is
// server-side (filemanager.py); the browser only names paths.
import { api, uploadFile } from '../api.js';
import {
  h, clear, card, table, errorBox, spinner, bytes, clock, confirmDialog, toast,
} from '../dom.js';

let root;
let cwd = '';
let roots = [];
let sort = { key: 'name', dir: 1 };
let mode = 'browse'; // browse | trash
let trash = { entries: [] };

const SORTS = [['name', 'Name'], ['size', 'Size'], ['mtime', 'Modified']];

function promptDialog(title, label, value = '', { phrase } = {}) {
  // Small inline prompt built with the same dialog element as confirmations.
  return new Promise((resolve) => {
    const input = h('input', { type: 'text', value, spellcheck: 'false', autocomplete: 'off' });
    const dlg = h('dialog', { 'aria-labelledby': 'fp-t' },
      h('form', { method: 'dialog' },
        h('h2', { id: 'fp-t' }, title),
        h('label', {}, label), input,
        h('p', { class: 'form-error', role: 'alert', hidden: true }),
        h('div', { class: 'dialog-actions' },
          h('button', { value: 'cancel', class: 'btn btn-ghost', type: 'submit' }, 'Cancel'),
          h('button', { value: 'ok', class: 'btn btn-primary', type: 'submit' }, 'OK'))));
    root.append(dlg);
    const err = dlg.querySelector('.form-error');
    dlg.querySelector('form').addEventListener('submit', (ev) => {
      if (dlg.returnValue !== 'ok') return;
      if (phrase && input.value !== phrase) {
        ev.preventDefault();
        err.hidden = false;
        err.textContent = `Type “${phrase}” exactly to confirm.`;
        return;
      }
      if (!input.value.trim() && !phrase) {
        ev.preventDefault();
        err.hidden = false;
        err.textContent = 'This field cannot be empty.';
      }
    });
    dlg.addEventListener('close', () => {
      const ok = dlg.returnValue === 'ok';
      resolve(ok ? input.value : null);
      dlg.remove();
    });
    dlg.showModal();
    input.focus();
  });
}

function crumbs(path) {
  if (!path) return h('span', { class: 'muted' }, '—');
  const parts = path.split('/').filter(Boolean);
  const out = [];
  let acc = '';
  for (const p of parts) {
    acc += `/${p}`;
    const target = acc;
    out.push(h('button', {
      type: 'button', class: 'link-btn', 'data-dir': target,
      style: { display: 'inline' },
    }, `${p}/`));
  }
  return h('span', { class: 'crumbs' }, out);
}

function sortedEntries(entries) {
  const key = sort.key;
  return [...entries].sort((a, b) => {
    let cmp = 0;
    if (key === 'size') cmp = (a.size ?? -1) - (b.size ?? -1);
    else if (key === 'mtime') cmp = (a.mtime ?? 0) - (b.mtime ?? 0);
    else cmp = a.name.toLowerCase() < b.name.toLowerCase() ? -1 : (a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0);
    if (!cmp) cmp = a.type === b.type ? 0 : (a.type === 'dir' ? -1 : 1);
    return cmp * sort.dir;
  });
}

function actionsFor(entry) {
  const row = h('div', { class: 'btn-row' });
  const add = (label, fn, cls = 'btn btn-ghost btn-sm', title = '') => {
    const b = h('button', { type: 'button', class: cls, title }, label);
    b.addEventListener('click', fn);
    row.append(b);
    return b;
  };
  add('Preview', async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const data = await api.get(`/api/files/preview?path=${encodeURIComponent(entry.path)}`);
      cardOpen('Preview', h('div', {},
        h('p', { class: 'muted small' }, `${entry.path} · ${bytes(data.size)}${data.truncated ? ' · first 64 KiB' : ''}`),
        h('pre', { class: 'code', tabindex: '0' }, data.text)));
    } catch (err) {
      toast(`Preview refused: ${err.message}`, 'warn');
    } finally { btn.disabled = false; }
  }, 'btn btn-ghost btn-sm', 'Text preview (first 64 KiB; binary refused)');
  row.append(h('a', {
    class: 'btn btn-ghost btn-sm', href: `/api/files/download?path=${encodeURIComponent(entry.path)}`,
    download: entry.name,
  }, 'Download'));
  add('Rename', async () => {
    const name = await promptDialog(`Rename ${entry.name}`, 'New name', entry.name);
    if (!name) return;
    try {
      await api.post('/api/files/rename', { path: entry.path, name });
      toast(`Renamed to ${name}`, 'ok');
      loadDir(cwd);
    } catch (err) { toast(`Rename failed: ${err.message}`, 'crit'); }
  });
  add('Move', async () => {
    const to = await promptDialog(`Move ${entry.name}`, 'Destination directory (inside the allowed roots)', cwd);
    if (!to) return;
    try {
      await api.post('/api/files/move', { path: entry.path, to });
      toast(`Moved to ${to}`, 'ok');
      loadDir(cwd);
    } catch (err) { toast(`Move failed: ${err.message}`, 'crit'); }
  });
  add('Move to trash', async () => {
    const res = await confirmDialog({
      title: `Move ${entry.name} to trash?`,
      body: 'The item is moved to /srv/cache/trash with a manifest entry. It can be restored from the Trash tab; purge is separate and permanent.',
      okLabel: 'Move to trash',
      danger: true,
    });
    if (!res.ok) return;
    try {
      await api.post('/api/files/delete', { path: entry.path });
      toast(`${entry.name} moved to trash`, 'ok');
      loadDir(cwd);
    } catch (err) { toast(`Delete refused: ${err.message}`, 'crit'); }
  }, 'btn btn-danger btn-sm');
  return row;
}

function browseTable(data) {
  const entries = sortedEntries(data.entries || []);
  return table(['Name', 'Type', 'Size', 'Modified', 'Mode', 'Actions'],
    entries.map((e) => [
      e.type === 'dir'
        ? h('button', { type: 'button', class: 'link-btn', 'data-dir': e.path }, `${e.name}/`)
        : h('span', {}, e.name),
      e.type,
      e.type === 'file' ? bytes(e.size) : '—',
      clock(e.mtime),
      h('code', {}, e.mode || '—'),
      e.type === 'dir'
        ? (() => {
          const row = h('div', { class: 'btn-row' });
          const b = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'data-dir': e.path }, 'Open');
          const d = h('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, 'Dir size');
          d.addEventListener('click', async () => {
            d.disabled = true;
            try {
              const info = await api.get(`/api/files/dirsize?path=${encodeURIComponent(e.path)}`);
              cardOpen(`Size of ${e.path}`,
                h('p', {}, `${bytes(info.bytes)} · ${info.files ?? '—'} file(s), ${info.dirs ?? '—'} subdirectory(s)`));
            } catch (err) { toast(`Dir size failed: ${err.message}`, 'crit'); } finally { d.disabled = false; }
          });
          const t = h('button', { type: 'button', class: 'btn btn-danger btn-sm' }, 'Move to trash');
          t.addEventListener('click', async () => {
            const res = await confirmDialog({
              title: `Move ${e.name} to trash?`,
              body: 'The whole directory moves to the trash. It can be restored from the Trash tab.',
              okLabel: 'Move to trash', danger: true,
            });
            if (!res.ok) return;
            try {
              await api.post('/api/files/delete', { path: e.path });
              toast(`${e.name} moved to trash`, 'ok');
              loadDir(cwd);
            } catch (err) { toast(`Delete refused: ${err.message}`, 'crit'); }
          });
          row.append(b, d, t);
          return row;
        })()
        : actionsFor(e),
    ]), { caption: `Contents of ${data.path}`, empty: 'This directory is empty.' });
}

let panelEl;

function cardOpen(title, ...body) {
  // The single detail panel (preview / dir size) at the bottom of the page.
  clear(panelEl).append(card(title, h('button', {
    type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => clear(panelEl),
  }, 'Close'), ...body));
  panelEl.scrollIntoView({ block: 'nearest' });
}

async function loadDir(path, { signal } = {}) {
  if (!path) return;
  const data = await api.get(`/api/files/browse?path=${encodeURIComponent(path)}`, { signal });
  cwd = data.path;
  renderBrowse(data);
}

function renderBrowse(data) {
  const holder = root.querySelector('.files-main');
  const header = root.querySelector('.files-head');
  clear(header).append(crumbs(cwd));
  clear(holder).append(browseTable(data));
}

async function search(root0, q) {
  if (!q) { toast('Type something to search for.', 'warn'); return; }
  try {
    const data = await api.get(`/api/files/search?root=${encodeURIComponent(root0)}&q=${encodeURIComponent(q)}`);
    cardOpen(`Search results: “${q}” in ${data.root} (${data.results.length})`,
      table(['Path', 'Type', 'Size', 'Modified'],
        (data.results || []).map((e) => [h('code', { class: 'small' }, e.path), e.type, bytes(e.size), clock(e.mtime)]),
        { caption: 'Search results', empty: 'No matches.' }));
  } catch (err) {
    toast(`Search failed: ${err.message}`, 'crit');
  }
}

async function uploadTo(dir) {
  const input = h('input', { type: 'file', 'aria-label': 'File to upload' });
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    try {
      const entry = await uploadFile(dir, file, {
        onProgress: (frac) => { toast(`Uploading… ${Math.round(frac * 100)}%`, 'ok', 1500); },
      });
      toast(`Uploaded ${entry.name} (${bytes(entry.size)})`, 'ok');
      loadDir(cwd);
    } catch (err) {
      toast(`Upload failed: ${err.message}`, 'crit', 9000);
    }
  });
  input.click();
}

function renderTrash() {
  const holder = root.querySelector('.files-main');
  clear(holder).append(
    h('p', { class: 'muted small' }, `Trash root: ${trash.trash_root || '—'}. `
      + (trash.purge_token_hint || '')),
    table(['Trashed', 'Original path', 'Type', 'Size', 'When', 'Still present', 'Actions'],
      (trash.entries || []).map((e) => [
        h('code', { class: 'small' }, e.trashed_name || '—'),
        h('code', { class: 'small' }, e.original || '—'),
        e.type || '—',
        e.size !== null && e.size !== undefined ? bytes(e.size) : '—',
        clock(e.ts),
        e.still_present ? h('span', { class: 'badge badge-ok' }, 'yes') : h('span', { class: 'badge badge-crit' }, 'no'),
        h('div', { class: 'btn-row' },
          (() => {
            const b = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'data-restore': e.id }, 'Restore');
            b.addEventListener('click', async () => {
              try {
                await api.post('/api/files/trash/restore', { id: e.id });
                toast(`Restored ${e.original}`, 'ok');
                loadTrash();
              } catch (err) { toast(`Restore failed: ${err.message}`, 'crit'); }
            });
            return b;
          })()),
      ]), { caption: 'Trash entries', empty: 'The trash is empty.' }),
    (() => {
      const b = h('button', { type: 'button', class: 'btn btn-danger', id: 'purge-trash' }, 'Purge the trash (permanent)');
      b.addEventListener('click', async () => {
        const res = await confirmDialog({
          title: 'Purge the trash?',
          body: 'PERMANENTLY deletes every trashed item. This is the only permanent deletion in this UI; it is audit-logged.',
          phrase: 'PURGE TRASH',
          okLabel: 'Purge the trash',
          danger: true,
        });
        if (!res.ok) return;
        try {
          const out = await api.post('/api/files/trash/purge', { confirm: 'PURGE TRASH' });
          toast(`Purged ${out.count} item(s), ${bytes(out.bytes)} freed`, 'ok');
          loadTrash();
        } catch (err) { toast(`Purge refused: ${err.message}`, 'crit'); }
      });
      return b;
    })());
}

async function loadTrash() {
  trash = await api.get('/api/files/trash');
  renderTrash();
}

function switchMode(next) {
  mode = next;
  for (const b of root.querySelectorAll('[data-mode]')) {
    b.setAttribute('aria-pressed', String(b.dataset.mode === mode));
  }
  root.querySelector('.files-tools').hidden = mode !== 'browse';
  root.querySelector('.files-head').hidden = mode !== 'browse';
  if (mode === 'trash') loadTrash().catch((err) => {
    clear(root.querySelector('.files-main')).append(errorBox(err));
  });
  else loadDir(cwd).catch((err) => {
    clear(root.querySelector('.files-main')).append(errorBox(err));
  });
}

function toolsBar() {
  const sortSel = h('select', { id: 'files-sort', 'aria-label': 'Sort by' },
    SORTS.map(([k, label]) => h('option', { value: k, selected: k === sort.key }, label)));
  const dirBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', id: 'files-sort-dir' },
    sort.dir > 0 ? 'ascending' : 'descending');
  const searchInput = h('input', { id: 'files-search', type: 'search', placeholder: 'search names under this root',
    'aria-label': 'Search file names under the current root' });
  const searchBtn = h('button', { type: 'button', class: 'btn btn-primary btn-sm' }, 'Search');
  const upBtn = h('button', { type: 'button', class: 'btn btn-sm', id: 'files-upload' }, 'Upload here');
  const mkBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', id: 'files-mkdir' }, 'New folder');
  sortSel.addEventListener('change', () => {
    sort.key = sortSel.value;
    loadDir(cwd).catch(() => {});
  });
  dirBtn.addEventListener('click', () => {
    sort.dir = -sort.dir;
    dirBtn.textContent = sort.dir > 0 ? 'ascending' : 'descending';
    loadDir(cwd).catch(() => {});
  });
  searchBtn.addEventListener('click', () => search(cwd, searchInput.value.trim()));
  searchInput.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') searchBtn.click(); });
  upBtn.addEventListener('click', () => uploadTo(cwd));
  mkBtn.addEventListener('click', async () => {
    const name = await promptDialog('New folder', 'Folder name (inside the current directory)');
    if (!name) return;
    try {
      await api.post('/api/files/mkdir', { path: `${cwd}/${name}` });
      toast(`Created ${name}`, 'ok');
      loadDir(cwd);
    } catch (err) { toast(`Mkdir failed: ${err.message}`, 'crit'); }
  });
  return h('div', { class: 'toolbar files-tools' }, sortSel, dirBtn, searchInput, searchBtn, mkBtn, upBtn);
}

export default {
  title: 'Files',
  interval: 0,
  async mount(el, { params } = {}) {
    root = el;
    mode = 'browse';
    clear(root).append(
      h('p', { class: 'lead' }, 'Browse, transfer and organise files inside the allowed roots. '
        + 'Delete means move to trash — the only permanent deletion is a separate, typed-confirmation purge. '
        + 'Protected paths and active model files are refused server-side.'),
      spinner());
    const rootsData = await api.get('/api/files/roots');
    roots = rootsData.roots || [];
    const browseBtn = h('button', { type: 'button', class: 'btn btn-sm', 'data-mode': 'browse', 'aria-pressed': 'true' }, 'Browse');
    const trashBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'data-mode': 'trash', 'aria-pressed': 'false' }, 'Trash');
    browseBtn.addEventListener('click', () => switchMode('browse'));
    trashBtn.addEventListener('click', () => switchMode('trash'));
    const rootBtns = roots.map((r) => {
      const b = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'data-root': r }, r);
      b.addEventListener('click', () => loadDir(r).catch((err) => {
        clear(root.querySelector('.files-main')).append(errorBox(err));
      }));
      return b;
    });
    panelEl = h('div', { class: 'files-panel' });
    const deep = params && params[0] ? decodeURIComponent(params[0]) : '';
    clear(root).append(
      h('div', { class: 'toolbar' }, browseBtn, trashBtn, h('span', { class: 'muted small' }, 'Roots: '), rootBtns),
      h('p', { class: 'files-head crumbs muted small', role: 'navigation', 'aria-label': 'Current directory' }),
      toolsBar(),
      h('div', { class: 'files-main' }, spinner()),
      panelEl);
    root.addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-dir]');
      if (!b) return;
      loadDir(b.dataset.dir).catch((err) => {
        clear(root.querySelector('.files-main')).append(errorBox(err));
      });
    });
    await loadDir(deep && roots.some((r) => deep === r || deep.startsWith(`${r}/`)) ? deep : (roots[0] || ''));
  },
  unmount() { panelEl = null; },
};
