// Tiny hand-rolled SVG charts (no libraries, no CDN). Only numbers from our
// own API responses ever enter these helpers, and everything is built with
// createElementNS — no string is parsed as HTML.

const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

function noData(width, height, label) {
  const s = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart chart-empty',
    role: 'img', 'aria-label': `${label}: no data yet` });
  const t = svgEl('text', { x: 4, y: height / 2 + 4, class: 'chart-empty-text' });
  t.textContent = 'no data yet';
  s.append(t);
  return s;
}

// A one-series line chart over time. `values`: [{x (epoch s), y}] or numbers.
export function sparkline(values, { width = 240, height = 56, label = 'trend', stroke = 2 } = {}) {
  const pts = (values || [])
    .map((v) => (typeof v === 'number' ? { y: v } : { x: Number(v && v.x), y: Number(v && v.y) }))
    .filter((p) => Number.isFinite(p.y));
  if (pts.length < 2) return noData(width, height, label);
  const ys = pts.map((p) => p.y);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = max - min || 1;
  const n = pts.length;
  const px = (i) => (n === 1 ? 0 : (i / (n - 1)) * (width - 8)) + 4;
  const py = (v) => height - 6 - ((v - min) / span) * (height - 14);
  const path = pts.map((p, i) => `${px(i).toFixed(1)},${py(p.y).toFixed(1)}`).join(' ');
  const s = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart chart-line',
    role: 'img', 'aria-label': `${label}: last ${ys[n - 1]}, min ${min}, max ${max} over ${n} points` });
  const title = svgEl('title');
  title.textContent = `${label}: ${min} – ${max} (${n} points, latest ${ys[n - 1]})`;
  s.append(title);
  s.append(svgEl('polyline', { points: path, class: 'chart-line-stroke',
    'stroke-width': stroke, fill: 'none' }));
  const last = svgEl('circle', { cx: px(n - 1), cy: py(ys[n - 1]), r: 3, class: 'chart-line-dot' });
  s.append(last);
  return s;
}

// Horizontal bars: entries = [{label, value, text?}]. One row per entry,
// value-scaled; text (or the raw value) is printed at the bar's end.
export function hbars(entries, { width = 320, label = 'distribution', row = 22, bar = 14 } = {}) {
  const rows = (entries || []).filter((e) => e && Number.isFinite(Number(e.value)));
  if (!rows.length) return noData(width, Math.max(height0(rows), 24), label);
  const max = Math.max(...rows.map((e) => Math.abs(Number(e.value)))) || 1;
  const labelW = Math.round(width * 0.38);
  const valueW = width - labelW - 8;
  const height = rows.length * row;
  const s = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart chart-bars',
    role: 'img', 'aria-label': `${label}: ${rows.map((e) => `${e.label} ${e.text ?? e.value}`).join(', ')}` });
  rows.forEach((e, i) => {
    const v = Number(e.value);
    const y = i * row + (row - bar) / 2;
    const w = Math.max(2, (Math.abs(v) / max) * (valueW - 64));
    const t = svgEl('text', { x: 0, y: y + bar - 3, class: 'chart-bar-label' });
    t.textContent = e.label;
    s.append(t, svgEl('rect', { x: labelW, y, width: w, height: bar,
      class: `chart-bar-rect${v < 0 ? ' chart-bar-neg' : ''}`, rx: 2 }));
    const val = svgEl('text', { x: labelW + w + 5, y: y + bar - 3, class: 'chart-bar-value' });
    val.textContent = e.text !== undefined ? e.text : String(v);
    s.append(val);
  });
  return s;
}

function height0(rows) { return (rows.length || 1) * 22; }
