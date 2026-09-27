// PERFORMANCE: aggregates computed client-side over the scheduler's request
// history (TTFT, tokens/s, queue wait, token counts). Charts are hand-rolled
// SVG (js/chart.js). Memory low-water marks live in the ops/bench history on
// disk; no API exposes them yet, so this page says so instead of inventing.
import { api } from '../api.js';
import {
  h, clear, card, kv, table, errorBox, spinner, num,
} from '../dom.js';
import { sparkline, hbars } from '../chart.js';

let root;
let range = '24h';
const RANGES = [['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800], ['all', 0]];

function percentile(sorted, p) {
  if (!sorted.length) return null;
  const i = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[i];
}

function stats(values) {
  const nums = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!nums.length) return { count: 0, avg: null, p50: null, p95: null, min: null, max: null };
  const sum = nums.reduce((a, b) => a + b, 0);
  return {
    count: nums.length, avg: sum / nums.length, p50: percentile(nums, 50),
    p95: percentile(nums, 95), min: nums[0], max: nums[nums.length - 1],
  };
}

function ms(v) { return v === null || v === undefined ? '—' : `${Math.round(v)} ms`; }

function groupBy(records, key) {
  const out = {};
  for (const r of records) {
    const k = String(r[key] || 'unknown');
    (out[k] = out[k] || []).push(r);
  }
  return out;
}

function aggregateTable(records, key, title) {
  const groups = groupBy(records, key);
  const names = Object.keys(groups).sort();
  return table([title, 'Requests', 'avg TTFT', 'p95 TTFT', 'avg tok/s', 'avg wait', 'tokens (p+c)'],
    names.map((name) => {
      const g = groups[name];
      const ttft = stats(g.map((r) => r.ttft_ms));
      const tps = stats(g.map((r) => r.tps));
      const wait = stats(g.filter((r) => r.start_ts && r.enqueue_ts)
        .map((r) => (r.start_ts - r.enqueue_ts) * 1000));
      const tokens = g.reduce((a, r) => a + (r.prompt_tokens || 0) + (r.completion_tokens || 0), 0);
      return [name, String(g.length), ms(ttft.avg), ms(ttft.p95),
        tps.avg ? num(tps.avg, 1) : '—', wait.avg ? ms(wait.avg) : '—',
        tokens ? tokens.toLocaleString() : '—'];
    }), { caption: `${title} aggregates`, empty: 'No requests in this window.' });
}

function render(records, hist) {
  const done = records.filter((r) => ['done', 'error', 'timeout', 'cancelled'].includes(r.state));
  const ttft = stats(done.map((r) => r.ttft_ms));
  const tps = stats(done.map((r) => r.tps));
  const wait = stats(records.filter((r) => r.start_ts && r.enqueue_ts)
    .map((r) => (r.start_ts - r.enqueue_ts) * 1000));
  const tokensP = records.reduce((a, r) => a + (r.prompt_tokens || 0), 0);
  const tokensC = records.reduce((a, r) => a + (r.completion_tokens || 0), 0);
  const errors = records.filter((r) => ['error', 'timeout'].includes(r.state));

  const byTs = [...done].filter((r) => r.ttft_ms && r.enqueue_ts).sort((a, b) => a.enqueue_ts - b.enqueue_ts);

  clear(root).append(
    h('div', { class: 'grid grid-2' },
      card('Live metrics (from request history)',
        kv([
          ['Requests in window', String(records.length)],
          ['TTFT avg / p50 / p95', `${ms(ttft.avg)} / ${ms(ttft.p50)} / ${ms(ttft.p95)}`],
          ['Tokens/s avg / p50 / p95', `${tps.avg ? num(tps.avg, 1) : '—'} / ${tps.p50 ? num(tps.p50, 1) : '—'} / ${tps.p95 ? num(tps.p95, 1) : '—'}`],
          ['Queue wait avg / p95', `${ms(wait.avg)} / ${ms(wait.p95)}`],
          ['Tokens', `prompt ${tokensP.toLocaleString()} + completion ${tokensC.toLocaleString()}`],
          ['Errors / timeouts', String(errors.length)],
        ]),
        h('p', { class: 'muted small' }, hist.note || '')),
      card('TTFT over time',
        byTs.length ? sparkline(byTs.map((r) => ({ x: r.enqueue_ts, y: r.ttft_ms })),
          { label: 'TTFT in the selected window' })
          : h('p', { class: 'muted' }, 'No completed requests with timing in this window.'),
        h('p', { class: 'muted small' }, 'Each point is one finished request; the dot is the latest.'))),
    card('Requests per profile',
      hbars(Object.entries(groupBy(records, 'profile')).map(([label, g]) => ({
        label, value: g.length, text: String(g.length),
      })), { label: 'requests per profile' }),
      aggregateTable(records, 'profile', 'Profile')),
    card('Per-reasoning aggregates', aggregateTable(records, 'reasoning', 'Reasoning level')),
    card('Memory low-water marks',
      h('p', { class: 'callout callout-warning', role: 'note' },
        'unavailable: memory low-water marks are recorded by the ops/bench suite into its on-disk '
        + 'history JSONL, which no API exposes yet. Nothing is shown here rather than a guess. '
        + 'Run benchmarks from Jobs / Actions (benchmark_run) meanwhile; results land in the bench history.'),
      h('p', { class: 'muted small' }, 'The scheduler history that feeds every other number on this page '
        + 'is metadata only — prompt bodies are never stored.')));
}

export default {
  title: 'Performance',
  interval: 0,
  async mount(el) {
    root = el;
    const sel = h('select', { id: 'perf-range', 'aria-label': 'Time range' },
      RANGES.map(([k]) => h('option', { value: k, selected: k === range }, k)));
    sel.addEventListener('change', () => { range = sel.value; load(); });
    clear(root).append(
      h('p', { class: 'lead' }, 'Latency and throughput aggregates over the scheduler\'s request history. '
        + 'Prompt bodies are never stored; these are timing and token counts only.'),
      h('div', { class: 'toolbar' },
        h('label', { class: 'inline', for: 'perf-range' }, 'range ', sel)),
      h('div', { class: 'perf-holder' }, spinner()));
    await load();
  },
};

async function load() {
  const holder = root.querySelector('.perf-holder');
  try {
    const params = new URLSearchParams({ limit: '500' });
    const found = RANGES.find(([k]) => k === range);
    if (found && found[1]) params.set('since', String(Math.floor(Date.now() / 1000 - found[1])));
    const data = await api.get(`/api/requests?${params}`);
    const hist = data.history || {};
    clear(holder);
    if (hist.available === false) {
      holder.append(h('p', { class: 'callout callout-warning' },
        `unavailable: ${hist.reason || 'the scheduler history is not reachable'}`));
      return;
    }
    render(hist.records || [], hist);
  } catch (err) {
    clear(holder).append(errorBox(err));
  }
}
