// Logs: the signed-in user's own activity and errors across generations,
// jobs, flows, realtime sessions and account actions. The server merges the
// sources, redacts every string and never returns prompts or secrets.
import { api, qs } from '../api.js';
import { ago, debounce, h, replace, toast } from '../dom.js';
import {
  badge, button, callout, chips, emptyState, field, kv, pageHeader, select, skeletonLines, statusDot, textInput,
} from '../ui.js';

const STATUS = [['', 'All'], ['failed', 'Errors'], ['running', 'Running'], ['waiting', 'Waiting'], ['ok', 'Done'], ['cancelled', 'Cancelled']];
const RANGES = [['3600', 'Last hour'], ['86400', 'Last 24 hours'], ['604800', 'Last 7 days'], ['0', 'Everything kept']];
const TONE = { ok: 'ok', failed: 'danger', running: 'info', waiting: 'warn', cancelled: 'neutral' };
const WORD = { ok: 'Done', failed: 'Error', running: 'Running', waiting: 'Waiting', cancelled: 'Cancelled' };
const KIND_LABEL = {
  media: 'Images & video', music: 'Music', realtime: 'Realtime', account: 'Account', metrics: 'Service events',
  voice: 'Voice', flows: 'Creative Flows', call: 'Call Agents', live: 'Live',
};
const REFRESH_MS = 15_000;

function duration(ms) {
  if (ms === null || ms === undefined) return null;
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  return s < 120 ? `${s.toFixed(1)} s` : `${Math.round(s / 60)} min`;
}

function row(item) {
  const tone = TONE[item.status] || 'neutral';
  const when = new Date(item.at * 1000);
  const details = Object.entries(item.detail || {}).filter(([, v]) => v !== null && v !== '');
  const body = h('div', { class: 'log-body' },
    h('div', { class: 'log-head' },
      h('span', { class: `status status-${tone}` }, statusDot(tone), WORD[item.status] || item.status),
      h('span', { class: 'log-title' }, item.title),
      badge(KIND_LABEL[item.kind] || item.kind, 'neutral'),
      h('time', { class: 'muted small', datetime: when.toISOString(), title: when.toLocaleString() }, ago(item.at)),
      item.duration_ms !== null ? h('span', { class: 'muted small' }, duration(item.duration_ms)) : null),
    item.error ? h('p', { class: 'log-error' }, item.error) : null,
    details.length ? h('details', { class: 'log-details' },
      h('summary', {}, 'Details'),
      kv([['Reference', h('code', { class: 'code-inline' }, item.id)], ...details.map(([k, v]) => [k.replace(/_/g, ' '), String(v)])])) : null);
  return h('li', { class: `log-row log-${tone}`, dataset: { kind: item.kind, status: item.status } }, body,
    item.link ? h('a', { class: 'btn btn-ghost btn-sm log-open', href: item.link, 'aria-label': `Open ${item.title}` }, 'Open') : null);
}

export default {
  title: 'Logs',
  async mount(root, ctx) {
    const state = { kind: ctx.query.kind || '', status: ctx.query.status || '', q: '', range: '86400' };
    let alive = true;
    let timer = null;
    let lastItems = [];
    const list = h('ul', { class: 'log-list', id: 'log-list', 'aria-busy': 'true', 'aria-label': 'Activity entries' }, h('li', {}, skeletonLines(5)));
    const note = h('div', { class: 'stack-sm' });
    const summary = h('p', { class: 'page-sub', 'aria-live': 'polite' }, 'Loading your activity…');
    const kindSel = select([['', 'All sources']], state.kind, { onChange: (v) => { state.kind = v; load(); }, attrs: { name: 'kind' } });
    const rangeSel = select(RANGES, state.range, { onChange: (v) => { state.range = v; load(); }, attrs: { name: 'range' } });
    const search = textInput({ placeholder: 'Search titles, errors and ids', maxLength: 100, type: 'search', attrs: { name: 'q' } });
    search.addEventListener('input', debounce(() => { state.q = search.value.trim(); load(); }, 350));
    const statusChips = chips(STATUS, { value: state.status, label: 'Status', onChange: (v) => { state.status = v; load(); } });
    const exportBtn = button('Download', { icon: 'download', size: 'sm', variant: 'ghost', title: 'Download the entries shown as JSON', onClick: () => download() });
    replace(root,
      pageHeader('Logs', 'Your generations, jobs, flows, realtime sessions and account activity. Sensitive values are removed on the server.',
        h('div', { class: 'row-wrap' }, exportBtn, button('Refresh', { icon: 'refresh', size: 'sm', variant: 'ghost', onClick: () => load(true) }))),
      summary,
      h('div', { class: 'filters' },
        h('div', { class: 'filters-row' }, statusChips),
        h('div', { class: 'filters-row log-filters' },
          field('Source', kindSel), field('Time', rangeSel), field('Search', search))),
      note, list);

    function download() {
      if (!lastItems.length) {
        toast('Nothing to download for these filters', 'warn');
        return;
      }
      const blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), filters: state, items: lastItems }, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = h('a', { href: url, download: `gx-playground-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.json`, hidden: true });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    }

    async function load(manual = false) {
      clearTimeout(timer);
      const since = Number(state.range) ? Date.now() / 1000 - Number(state.range) : 0;
      list.setAttribute('aria-busy', 'true');
      try {
        const res = await api.get(`/api/activity${qs({ kind: state.kind, status: state.status, q: state.q, since: since ? since.toFixed(0) : '', limit: 300 })}`);
        if (!alive) return;
        replace(kindSel, h('option', { value: '' }, 'All sources'),
          ...(res.kinds || []).map((k) => h('option', { value: k }, KIND_LABEL[k] || k)));
        kindSel.value = state.kind;
        lastItems = res.items || [];
        const c = res.counts || {};
        summary.textContent = `${res.total} entr${res.total === 1 ? 'y' : 'ies'} · ${c.failed || 0} error${c.failed === 1 ? '' : 's'} · ${c.running || 0} running`;
        replace(note, (res.unavailable || []).length
          ? callout('warn', 'Some sources could not be read', `Not shown right now: ${res.unavailable.map((k) => KIND_LABEL[k] || k).join(', ')}.`)
          : '');
        replace(list, lastItems.length ? lastItems.map(row)
          : h('li', {}, emptyState({ icon: 'logs', title: 'Nothing to show', text: state.status || state.kind || state.q ? 'No entries match these filters.' : 'Your activity appears here as you create.' })));
        if (manual) toast('Logs refreshed', 'ok');
      } catch (err) {
        if (!alive) return;
        replace(note, callout('danger', 'Your logs could not be loaded', err.message,
          [button('Try again', { icon: 'refresh', size: 'sm', onClick: () => load(true) })]));
      } finally {
        list.setAttribute('aria-busy', 'false');
      }
      if (alive) timer = setTimeout(tick, REFRESH_MS);
    }

    // background refresh only while the tab is visible
    function tick() {
      if (!alive) return;
      if (document.hidden) timer = setTimeout(tick, REFRESH_MS);
      else load();
    }

    await load();
    return () => { alive = false; clearTimeout(timer); };
  },
};
