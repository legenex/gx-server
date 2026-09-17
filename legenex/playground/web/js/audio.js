// Waveform canvas, audio player and waveform range selection.
import { h, mmss } from './dom.js';
import { icon } from './icons.js';

const players = new Set();

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function normalise(peaks) {
  const list = Array.isArray(peaks) ? peaks.filter((p) => Array.isArray(p) && p.length >= 2) : [];
  if (!list.length) return [];
  const top = Math.max(0.05, ...list.map(([a, b]) => Math.max(Math.abs(a), Math.abs(b))));
  return list.map(([a, b]) => [Math.max(-1, a / top), Math.min(1, b / top)]);
}

// Draw bars into a canvas sized to its CSS box (device-pixel aware).
export function drawWaveform(canvas, peaks, { progress = 0, selection = null, duration = 0 } = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  const hgt = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== hgt) canvas.height = hgt;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.clearRect(0, 0, w, hgt);
  const data = normalise(peaks);
  const base = cssVar('--wave', '#5b6178');
  const played = cssVar('--wave-played', '#8b7bff');
  const selFill = cssVar('--wave-select', 'rgba(34,211,238,.18)');
  const selEdge = cssVar('--accent-2', '#22d3ee');
  if (selection && duration > 0) {
    const x0 = (Math.min(selection[0], selection[1]) / duration) * w;
    const x1 = (Math.max(selection[0], selection[1]) / duration) * w;
    ctx.fillStyle = selFill;
    ctx.fillRect(x0, 0, Math.max(1, x1 - x0), hgt);
    ctx.fillStyle = selEdge;
    ctx.fillRect(x0, 0, Math.max(1, dpr), hgt);
    ctx.fillRect(Math.max(0, x1 - dpr), 0, Math.max(1, dpr), hgt);
  }
  const bar = Math.max(2, Math.round(3 * dpr));
  const gap = Math.max(1, Math.round(1.5 * dpr));
  const count = Math.max(1, Math.floor(w / (bar + gap)));
  const mid = hgt / 2;
  for (let i = 0; i < count; i += 1) {
    let lo = -0.04;
    let hi = 0.04;
    if (data.length) {
      const [a, b] = data[Math.min(data.length - 1, Math.floor((i / count) * data.length))];
      lo = Math.min(lo, a);
      hi = Math.max(hi, b);
    }
    const x = i * (bar + gap);
    ctx.fillStyle = x / w < progress ? played : base;
    const top = mid - hi * (mid - dpr);
    const bottom = mid - lo * (mid - dpr);
    ctx.fillRect(x, top, bar, Math.max(dpr, bottom - top));
  }
  if (progress > 0 && progress < 1) {
    ctx.fillStyle = cssVar('--text', '#fff');
    ctx.fillRect(progress * w, 0, Math.max(1, dpr), hgt);
  }
}

// A static waveform (thumbnails, cards).
export function miniWave(peaks, { cls = '' } = {}) {
  const canvas = h('canvas', { class: `wave-mini ${cls}`, 'aria-hidden': 'true' });
  observe(canvas, () => drawWaveform(canvas, peaks));
  return canvas;
}

const redrawers = new Set();
window.addEventListener('gx-theme', () => {
  for (const fn of redrawers) fn();
});

function observe(canvas, draw) {
  const ro = new ResizeObserver(() => {
    if (!canvas.isConnected) { ro.disconnect(); redrawers.delete(draw); return; }
    draw();
  });
  ro.observe(canvas);
  redrawers.add(draw);
}

// Interactive waveform with keyboard seek; optional drag selection.
// opts: { peaks, duration, label, selectable, onSeek(sec), onSelect([a,b]) }
export function waveform(opts) {
  const state = { progress: 0, selection: null, duration: Number(opts.duration) || 0 };
  const canvas = h('canvas', { class: 'wave-canvas', 'aria-hidden': 'true' });
  const wrap = h('div', {
    class: `wave${opts.selectable ? ' wave-selectable' : ''}`, role: 'slider', tabindex: '0',
    'aria-label': opts.label || 'Seek', 'aria-valuemin': '0',
  }, canvas);
  const draw = () => drawWaveform(canvas, opts.peaks, { progress: state.progress, selection: state.selection, duration: state.duration });
  const syncAria = () => {
    wrap.setAttribute('aria-valuemax', String(Math.round(state.duration)));
    wrap.setAttribute('aria-valuenow', String(Math.round(state.progress * state.duration)));
    wrap.setAttribute('aria-valuetext', `${mmss(state.progress * state.duration)} of ${mmss(state.duration)}`);
  };
  observe(canvas, draw);
  syncAria();
  const secAt = (ev) => {
    const r = canvas.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (ev.clientX - r.left) / (r.width || 1)));
    return f * state.duration;
  };
  let dragStart = null;
  let moved = false;
  wrap.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0 || !state.duration) return;
    dragStart = secAt(ev);
    moved = false;
    wrap.setPointerCapture(ev.pointerId);
  });
  wrap.addEventListener('pointermove', (ev) => {
    if (dragStart === null || !opts.selectable) return;
    const now = secAt(ev);
    if (Math.abs(now - dragStart) > state.duration * 0.01) moved = true;
    if (moved) {
      state.selection = [dragStart, now];
      draw();
    }
  });
  wrap.addEventListener('pointerup', (ev) => {
    if (dragStart === null) return;
    const end = secAt(ev);
    if (moved && opts.selectable) {
      const sel = [Math.min(dragStart, end), Math.max(dragStart, end)];
      state.selection = sel;
      draw();
      if (opts.onSelect) opts.onSelect(sel);
    } else if (opts.onSeek) {
      opts.onSeek(end);
    }
    dragStart = null;
  });
  wrap.addEventListener('keydown', (ev) => {
    if (!state.duration || !opts.onSeek) return;
    const cur = state.progress * state.duration;
    const step = Math.max(1, state.duration / 20);
    let next = null;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowUp') next = cur + step;
    else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowDown') next = cur - step;
    else if (ev.key === 'Home') next = 0;
    else if (ev.key === 'End') next = state.duration;
    if (next === null) return;
    ev.preventDefault();
    opts.onSeek(Math.max(0, Math.min(state.duration, next)));
  });
  wrap.setProgress = (f) => {
    state.progress = Math.max(0, Math.min(1, f || 0));
    syncAria();
    draw();
  };
  wrap.setDuration = (d) => {
    if (d && Number.isFinite(d)) { state.duration = d; syncAria(); draw(); }
  };
  wrap.setSelection = (sel) => { state.selection = sel; draw(); };
  wrap.getDuration = () => state.duration;
  return wrap;
}

// Full audio player: play/pause, waveform seek, time.
export function audioPlayer(asset, { peaks, label, selectable = false, onSelect } = {}) {
  const src = asset.stream_url || asset.url;
  const audio = h('audio', { preload: 'none', src });
  const title = label || asset.title || 'track';
  const playBtn = h('button', { type: 'button', class: 'play-btn', 'aria-label': `Play ${title}` }, icon('play', { size: 18 }));
  const time = h('span', { class: 'player-time' }, `0:00 / ${mmss(asset.duration || 0)}`);
  const wave = waveform({
    peaks: peaks || asset.waveform, duration: asset.duration || 0, label: `Seek in ${title}`, selectable, onSelect,
    onSeek: (sec) => {
      if (audio.readyState === 0) audio.preload = 'auto';
      try { audio.currentTime = sec; } catch { /* not seekable yet */ }
      if (wave.getDuration()) wave.setProgress(sec / wave.getDuration());
    },
  });
  const setPlaying = (on) => {
    playBtn.replaceChildren(icon(on ? 'pause' : 'play', { size: 18 }));
    playBtn.setAttribute('aria-label', `${on ? 'Pause' : 'Play'} ${title}`);
    playBtn.classList.toggle('is-playing', on);
  };
  playBtn.addEventListener('click', async () => {
    if (audio.paused) {
      for (const p of [...players]) if (!p.isConnected) players.delete(p);
      for (const p of players) if (p !== audio && !p.paused) p.pause();
      try { await audio.play(); } catch { /* blocked or failed; the error event reports */ }
    } else {
      audio.pause();
    }
  });
  audio.addEventListener('play', () => setPlaying(true));
  audio.addEventListener('pause', () => setPlaying(false));
  audio.addEventListener('ended', () => setPlaying(false));
  audio.addEventListener('loadedmetadata', () => wave.setDuration(audio.duration));
  audio.addEventListener('timeupdate', () => {
    const d = Number.isFinite(audio.duration) ? audio.duration : (asset.duration || 0);
    time.textContent = `${mmss(audio.currentTime)} / ${mmss(d)}`;
    if (d) wave.setProgress(audio.currentTime / d);
  });
  players.add(audio);
  const el = h('div', { class: 'player' }, playBtn, h('div', { class: 'player-main' }, wave, time), audio);
  el.audio = audio;
  el.wave = wave;
  return el;
}
