// Command bar (Ctrl/Cmd+K): quick create, navigation and Library search.
import { clear, h, openDialog, toast } from './dom.js';
import { icon } from './icons.js';
import { friendlyError, submitMedia, submitMusic } from './jobs.js';
import { navigate } from './nav.js';

const PAGES = [
  ['dashboard', 'Go to Dashboard', 'home'],
  ['images', 'Open Images', 'image'],
  ['video', 'Open Video', 'video'],
  ['music', 'Open Music', 'music'],
  ['library', 'Open Library', 'library'],
  ['history', 'Open History', 'history'],
];

async function quick(kind, text) {
  try {
    if (kind === 'image') await submitMedia({ kind: 't2i', prompt: text });
    else if (kind === 'video') await submitMedia({ kind: 't2v', prompt: text });
    else await submitMusic({ operation: 'generate', prompt: text });
    toast('Started. Follow it in the workspace.', 'ok');
    navigate(kind === 'image' ? 'images' : kind);
  } catch (err) {
    toast(friendlyError(err.message).text, 'danger');
  }
}

export function openCommandBar() {
  if (document.querySelector('.dialog-command')) return;
  const input = h('input', {
    class: 'cmd-input', type: 'text', placeholder: 'Describe something to create, or type a page…',
    'aria-label': 'Command', role: 'combobox', 'aria-expanded': 'true', 'aria-controls': 'cmd-list', autocomplete: 'off',
  });
  const list = h('ul', { class: 'cmd-list', id: 'cmd-list', role: 'listbox', 'aria-label': 'Commands' });
  let items = [];
  let active = 0;
  const build = () => {
    const text = input.value.trim();
    const q = text.toLowerCase();
    items = [];
    if (text) {
      items.push({ label: `Create image: “${text}”`, ic: 'image', run: () => quick('image', text) });
      items.push({ label: `Create video: “${text}”`, ic: 'video', run: () => quick('video', text) });
      items.push({ label: `Create music: “${text}”`, ic: 'music', run: () => quick('music', text) });
      items.push({ label: `Search Library for “${text}”`, ic: 'search', run: () => navigate('library', { q: text }) });
    }
    for (const [page, label, ic] of PAGES) {
      if (!q || label.toLowerCase().includes(q) || page.includes(q)) items.push({ label, ic, run: () => navigate(page) });
    }
    active = Math.min(active, Math.max(0, items.length - 1));
    clear(list);
    items.forEach((it, i) => {
      const li = h('li', {
        id: `cmd-opt-${i}`, role: 'option', class: 'cmd-item', 'aria-selected': String(i === active),
        onclick: () => { dlg.close(); it.run(); },
      }, icon(it.ic, { size: 18 }), h('span', {}, it.label));
      list.append(li);
    });
    input.setAttribute('aria-activedescendant', items.length ? `cmd-opt-${active}` : '');
  };
  input.addEventListener('input', () => { active = 0; build(); });
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); active = (active + 1) % Math.max(1, items.length); build(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); active = (active - 1 + items.length) % Math.max(1, items.length); build(); }
    else if (ev.key === 'Enter' && items[active]) { ev.preventDefault(); const it = items[active]; dlg.close(); it.run(); }
  });
  const dlg = openDialog({
    body: h('div', { class: 'cmd' }, h('div', { class: 'cmd-head' }, icon('sparkles', { size: 18 }), input), list,
      h('p', { class: 'cmd-foot' }, h('kbd', {}, '↑'), h('kbd', {}, '↓'), ' to move · ', h('kbd', {}, 'Enter'), ' to run · ', h('kbd', {}, 'Esc'), ' to close')),
    className: 'dialog-command', label: 'Command bar',
  });
  build();
  input.focus();
}
