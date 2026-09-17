// Models: the creative-facing catalogue of every gx alias with live state,
// node, measured memory, startup time, capabilities and the exact
// repository/revision. Read-only; admin actions open the Control Center.
import { api } from '../api.js';
import { h, replace, toast } from '../dom.js';
import {
  badge, button, callout, card, chips, disclosure, emptyState, kv, linkButton, pageHeader, skeletonGrid, statusDot,
} from '../ui.js';

const GROUPS = [['all', 'All'], ['create', 'Create'], ['realtime', 'Realtime'], ['text', 'Text']];
const TONE = {
  READY: 'ok', UNLOADED: 'neutral', LOADING: 'info', GENERATING: 'info', WAITING: 'warn', DRAINING: 'warn',
  BLOCKED: 'warn', ERROR: 'danger',
};
const REFRESH_MS = 20_000;

function shortRev(rev) {
  return typeof rev === 'string' && /^[0-9a-f]{12,}$/.test(rev) ? rev.slice(0, 12) : rev;
}

function copyText(text, what) {
  if (!navigator.clipboard) {
    toast('Copying needs a secure context (HTTPS or localhost).', 'warn');
    return;
  }
  navigator.clipboard.writeText(text).then(() => toast(`${what} copied`, 'ok'), () => toast('Could not copy', 'danger'));
}

function memoryText(fp) {
  if (!fp) return null;
  if (fp.measured === false) return h('span', { class: 'muted' }, fp.label || 'not measured yet');
  if (typeof fp.cold_gib === 'number') {
    return `${fp.resident_gib} GiB loaded · needs ${fp.cold_gib} GiB to start (measured ${fp.measured})`;
  }
  return fp.summary || null;
}

function startupText(m) {
  const fp = m.footprint || {};
  if (typeof fp.startup_s === 'number') return `${Math.round(fp.startup_s)} s cold start (measured)`;
  return m.startup || null;
}

function repoLine(repo, rev) {
  if (!repo) return null;
  const label = rev ? `${repo} @ ${shortRev(rev)}` : repo;
  return h('span', { class: 'repo-line' },
    h('code', { class: 'code-inline', title: rev ? `${repo} @ ${rev}` : repo }, label),
    button('', {
      icon: 'copy', size: 'sm', variant: 'ghost', title: 'Copy repository and revision',
      attrs: { 'aria-label': `Copy ${repo}${rev ? ' and revision' : ''}` },
      onClick: () => copyText(rev ? `${repo}@${rev}` : repo, 'Repository'),
    }));
}

function componentList(items) {
  return h('ul', { class: 'model-components' }, items.map((c) => h('li', {},
    h('p', { class: 'model-comp-role' }, c.label || c.role || c.kind || 'component',
      c.default ? badge('default', 'info') : null, c.status ? badge(c.status, 'neutral') : null),
    kv([
      ['Repository', c.repository ? repoLine(c.repository, c.revision) : null],
      ['File', c.file ? h('code', { class: 'code-inline' }, c.file) : null],
      ['Family', c.family], ['Base', c.base_match || c.base], ['Licence', c.licence],
      ['Capabilities', Array.isArray(c.capabilities) ? c.capabilities.join(', ') : c.capabilities],
      ['Workflows', Array.isArray(c.workflows) ? c.workflows.join(', ') : c.workflows],
      ['Measured', c.measured],
    ]))));
}

function modelCard(m) {
  const tone = TONE[m.state] || (m.registered ? 'neutral' : 'warn');
  const caps = (m.capabilities || []).slice(0, 24);
  const body = h('div', { class: 'stack' },
    h('p', { class: 'model-purpose' }, m.purpose),
    h('div', { class: 'stack-sm' },
      h('p', { class: `status status-${tone}` }, statusDot(tone), h('span', {}, m.state_label)),
      m.state_detail ? h('p', { class: 'muted small model-detail' }, m.state_detail) : null),
    kv([
      ['Node', m.node],
      ['Model', repoLine(m.repository, m.revision)],
      ['Family', m.family],
      ['Runtime', m.runtime],
      ['Runtime source', m.runtime_repository ? repoLine(m.runtime_repository, m.runtime_revision) : null],
      ['Quantization', m.quantization],
      ['Context', m.context ? `${Number(m.context).toLocaleString()} tokens` : null],
      ['Memory', memoryText(m.footprint)],
      ['Startup', startupText(m)],
      ['Queue', m.queue ? String(m.queue) : null],
      ['Endpoint', m.endpoint],
      ['Licence', m.licence],
    ]),
    caps.length ? h('ul', { class: 'caps-chips', 'aria-label': `${m.alias} capabilities` }, caps.map((c) => h('li', { class: 'chip chip-static' }, c))) : null,
    (m.not_supported || []).length ? h('p', { class: 'muted small' }, `Not supported: ${m.not_supported.join(', ')}`) : null,
    (m.variants || []).length ? disclosure(`Model variants (${m.variants.length})`, componentList(m.variants), { ic: 'layers' }) : null,
    (m.components || []).length ? disclosure(`Components (${m.components.length})`, componentList(m.components), { ic: 'layers' }) : null,
    m.registered ? null : callout('info', 'Not installed yet', 'This alias is part of the platform but its model is not registered on the cluster yet.'));
  return card(m.alias, body, {
    cls: 'model-card', level: 2, sub: m.group === 'realtime' ? 'Realtime' : m.group === 'create' ? 'Create' : 'Text',
    actions: linkButton('Control Center', m.admin_url, { icon: 'external', size: 'sm', variant: 'ghost', external: true, attrs: { 'aria-label': `Manage ${m.alias} in the Control Center (opens in a new tab)` } }),
  });
}

function extrasCard(name, data) {
  const title = name.replace(/[_-]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
  if (!data || typeof data !== 'object') return null;
  if (data.error) return card(title, callout('warn', 'Not available right now', data.error), { level: 2 });
  const items = Array.isArray(data.items) ? data.items : null;
  const rows = Object.entries(data).filter(([k, v]) => k !== 'items' && (typeof v !== 'object' || v === null));
  const body = h('div', { class: 'stack-sm' },
    rows.length ? kv(rows.map(([k, v]) => [k.replace(/_/g, ' '), String(v)])) : null,
    items ? (items.length
      ? h('ul', { class: 'extras-list' }, items.slice(0, 60).map((it) => h('li', {},
        kv(Object.entries(it || {}).filter(([, v]) => typeof v !== 'object' || v === null).map(([k, v]) => [k.replace(/_/g, ' '), String(v)])))))
      : h('p', { class: 'muted' }, 'Nothing here yet.')) : null);
  return card(title, body, { level: 2 });
}

export default {
  title: 'Models',
  async mount(root) {
    let group = 'all';
    let data = null;
    let alive = true;
    let timer = null;
    const grid = h('div', { class: 'model-grid', id: 'model-grid' }, skeletonGrid(4, 'model-grid'));
    const extras = h('div', { class: 'stack' });
    const note = h('div', { class: 'stack-sm' });
    const summary = h('p', { class: 'page-sub', 'aria-live': 'polite' }, 'Loading models…');
    const groupChips = chips(GROUPS, { value: group, label: 'Show', onChange: (v) => { group = v; render(); } });
    const ccLink = h('span');
    replace(root,
      pageHeader('Models', 'What each gx model does, where it runs and whether it is ready. Loading, unloading and pinning live in the Control Center.',
        h('div', { class: 'row-wrap' }, ccLink, button('Refresh', { icon: 'refresh', size: 'sm', variant: 'ghost', onClick: () => load(true) }))),
      summary, h('div', { class: 'filters' }, h('div', { class: 'filters-row' }, groupChips)), note, grid, extras);

    function render() {
      if (!data) return;
      const models = data.models.filter((m) => group === 'all' || m.group === group);
      const ready = data.models.filter((m) => ['READY', 'UNLOADED', 'GENERATING'].includes(m.state)).length;
      summary.textContent = `${data.models.length} models · ${ready} ready to use${data.profile ? ` · profile ${data.profile}` : ''}`;
      replace(grid, models.length ? models.map(modelCard) : emptyState({ icon: 'cpu', title: 'No models in this group' }));
      const extraCards = Object.entries(data.extras || {}).map(([k, v]) => extrasCard(k, v)).filter(Boolean);
      const wf = (data.workflows || []);
      replace(extras, ...extraCards,
        wf.length ? card('Workflows', h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
          h('caption', { class: 'sr-only' }, 'Which alias runs each media workflow'),
          h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Workflow'), h('th', { scope: 'col' }, 'Alias'))),
          h('tbody', {}, wf.map((w) => h('tr', {}, h('td', {}, h('code', { class: 'code-inline' }, w.workflow)), h('td', {}, w.alias)))))),
        { level: 2, sub: 'The media pipelines behind gx-image and gx-video' }) : null);
    }

    async function load(manual = false) {
      clearTimeout(timer);
      try {
        const res = await api.get('/api/catalog');
        if (!alive) return;
        data = res;
        replace(note);
        replace(ccLink, res.control_center_url ? linkButton('Resource Control', `${res.control_center_url}/#/resources`, { icon: 'external', size: 'sm', variant: 'ghost', external: true, attrs: { 'aria-label': 'Resource Control in the Control Center (opens in a new tab)' } }) : '');
        render();
        if (manual) toast('Models refreshed', 'ok');
      } catch (err) {
        if (!alive) return;
        replace(note, callout('danger', 'The model list could not be loaded', err.message,
          [button('Try again', { icon: 'refresh', size: 'sm', onClick: () => load(true) })]));
        if (!data) replace(grid);
      }
      if (alive) timer = setTimeout(tick, REFRESH_MS);
    }

    // live state refresh only while the tab is visible
    function tick() {
      if (!alive) return;
      if (document.hidden) timer = setTimeout(tick, REFRESH_MS);
      else load();
    }

    await load();
    return () => { alive = false; clearTimeout(timer); };
  },
};
