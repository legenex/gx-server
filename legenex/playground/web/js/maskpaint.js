// Edit-mask painter for the Images page (Build V3, gx-image).
//
// White in the exported mask = the area the edit may change. The mask is
// painted over the source image with a pointer (mouse, pen, touch), with the
// keyboard (arrow keys move a crosshair, Space/Enter paints a dab), or entered
// as rectangles in percent (the accessible alternative to drawing). The
// export is a small, thresholded black/white PNG (at most 512 px on the long
// side), which the server decodes, measures and forwards to gx10-02.
import { h, uid } from './dom.js';
import { button, field, iconButton, numberInput, slider } from './ui.js';

const MAX_SIDE = 512;
const HISTORY = 20;
const PAINT = 'rgb(255, 64, 160)';

export function maskEditor({ onChange } = {}) {
  const canvas = h('canvas', {
    class: 'mask-canvas', tabindex: '0', role: 'application',
    'aria-label': 'Mask painter. Arrow keys move the brush, Shift moves faster, Space or Enter paints, E toggles the eraser.',
  });
  const img = h('img', { class: 'mask-image', alt: '', draggable: 'false' });
  const cursor = h('div', { class: 'mask-cursor', hidden: true, 'aria-hidden': 'true' });
  const stageBox = h('div', { class: 'mask-stage' }, img, canvas, cursor);
  const status = h('p', { class: 'field-hint mask-status', 'aria-live': 'polite' }, 'Nothing selected yet: the edit may change the whole image.');
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const history = [];
  const rects = [];
  let tool = 'brush';
  let painted = false;
  let drawing = false;
  let last = null;
  let kx = 0.5;
  let ky = 0.5;
  let coverage = 0;

  const brushBtn = iconButton('brush', 'Brush', () => setTool('brush'), { pressed: true, attrs: { 'data-tool': 'brush' } });
  const eraserBtn = iconButton('eraser', 'Eraser', () => setTool('eraser'), { pressed: false, attrs: { 'data-tool': 'eraser' } });
  const undoBtn = iconButton('undo', 'Undo last mask change', () => undo(), { attrs: { 'data-action': 'mask-undo' } });
  const invertBtn = iconButton('invert', 'Invert mask', () => { snapshot(); invert(); changed(); }, { attrs: { 'data-action': 'mask-invert' } });
  const clearBtn = button('Clear', { icon: 'trash', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'mask-clear' }, onClick: () => clear() });
  const size = slider({ label: 'Brush size', min: 2, max: 30, step: 1, value: 8, format: (v) => `${v}%`, hint: 'Percent of the image width.' });

  // Rectangle entry: the keyboard / screen-reader friendly way to build a mask.
  const rx = numberInput({ min: 0, max: 100, step: 1, value: 25, attrs: { 'data-rect': 'x' } });
  const ry = numberInput({ min: 0, max: 100, step: 1, value: 25, attrs: { 'data-rect': 'y' } });
  const rw = numberInput({ min: 1, max: 100, step: 1, value: 50, attrs: { 'data-rect': 'w' } });
  const rh = numberInput({ min: 1, max: 100, step: 1, value: 50, attrs: { 'data-rect': 'h' } });
  const rectError = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
  const rectList = h('ul', { class: 'mask-rects', 'aria-label': 'Rectangles in the mask' });
  const addRect = button('Add rectangle', { icon: 'square', size: 'sm', attrs: { 'data-action': 'mask-add-rect' }, onClick: () => addRectangle() });
  const wholeBtn = button('Select everything', { size: 'sm', variant: 'ghost', attrs: { 'data-action': 'mask-all' }, onClick: () => { snapshot(); ctx.fillStyle = PAINT; ctx.fillRect(0, 0, canvas.width, canvas.height); rects.push({ x: 0, y: 0, w: 1, h: 1 }); renderRects(); changed(); } });
  const rectBox = h('fieldset', { class: 'mask-rect-entry' },
    h('legend', { class: 'field-label' }, 'Add a rectangle (percent of the image)'),
    h('div', { class: 'grid-4' },
      field('Left', rx), field('Top', ry), field('Width', rw), field('Height', rh)),
    rectError,
    h('div', { class: 'row-wrap' }, addRect, wholeBtn),
    rectList);

  const el = h('div', { class: 'mask-editor' },
    h('p', { class: 'field-hint' }, 'Paint the area the edit may change. Everything outside the mask keeps the source pixels.'),
    stageBox,
    h('div', { class: 'row-wrap mask-tools', role: 'toolbar', 'aria-label': 'Mask tools' }, brushBtn, eraserBtn, undoBtn, invertBtn, clearBtn),
    size, status, rectBox);

  function setTool(t) {
    tool = t;
    brushBtn.setAttribute('aria-pressed', String(t === 'brush'));
    eraserBtn.setAttribute('aria-pressed', String(t === 'eraser'));
  }

  function snapshot() {
    if (!canvas.width) return;
    history.push({ data: ctx.getImageData(0, 0, canvas.width, canvas.height), rects: rects.length, painted });
    if (history.length > HISTORY) history.shift();
  }

  function undo() {
    const prev = history.pop();
    if (!prev) return;
    ctx.putImageData(prev.data, 0, 0);
    rects.length = Math.min(rects.length, prev.rects);
    painted = prev.painted;
    renderRects();
    changed();
  }

  function radius() { return Math.max(1, (size.getValue() / 100) * canvas.width / 2); }

  function dab(x, y) {
    ctx.globalCompositeOperation = tool === 'eraser' ? 'destination-out' : 'source-over';
    ctx.fillStyle = PAINT;
    ctx.beginPath();
    ctx.arc(x, y, radius(), 0, Math.PI * 2);
    ctx.fill();
    ctx.globalCompositeOperation = 'source-over';
    painted = true;
  }

  function line(a, b) {
    ctx.globalCompositeOperation = tool === 'eraser' ? 'destination-out' : 'source-over';
    ctx.strokeStyle = PAINT;
    ctx.lineWidth = radius() * 2;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.globalCompositeOperation = 'source-over';
    painted = true;
  }

  function invert() {
    const d = ctx.getImageData(0, 0, canvas.width, canvas.height);
    for (let i = 3; i < d.data.length; i += 4) {
      const on = d.data[i] >= 128;
      d.data[i - 3] = 255; d.data[i - 2] = 64; d.data[i - 1] = 160;
      d.data[i] = on ? 0 : 255;
    }
    ctx.putImageData(d, 0, 0);
    painted = true;
  }

  function measure() {
    if (!canvas.width) return 0;
    const d = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let on = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] >= 128) on += 1;
    return on / (canvas.width * canvas.height);
  }

  function changed({ silent = false } = {}) {
    coverage = measure();
    const pct = Math.round(coverage * 1000) / 10;
    status.textContent = coverage > 0
      ? `${pct}% of the image is selected; only that area may change.`
      : 'Nothing selected yet: the edit may change the whole image.';
    if (onChange && !silent) onChange(coverage);
  }

  function point(ev) {
    const r = canvas.getBoundingClientRect();
    return { x: ((ev.clientX - r.left) / r.width) * canvas.width, y: ((ev.clientY - r.top) / r.height) * canvas.height };
  }

  canvas.addEventListener('pointerdown', (ev) => {
    if (!canvas.width || (ev.pointerType === 'mouse' && ev.button !== 0)) return;
    ev.preventDefault();
    canvas.setPointerCapture(ev.pointerId);
    snapshot();
    drawing = true;
    last = point(ev);
    dab(last.x, last.y);
  });
  canvas.addEventListener('pointermove', (ev) => {
    if (!drawing) return;
    const p = point(ev);
    line(last, p);
    last = p;
  });
  const stop = () => { if (drawing) { drawing = false; last = null; changed(); } };
  canvas.addEventListener('pointerup', stop);
  canvas.addEventListener('pointercancel', stop);
  canvas.addEventListener('lostpointercapture', stop);

  function showCursor() {
    cursor.hidden = false;
    const d = (size.getValue() / 100) * 100;
    cursor.style.left = `${kx * 100}%`;
    cursor.style.top = `${ky * 100}%`;
    cursor.style.width = `${d}%`;
    cursor.style.aspectRatio = '1';
  }
  canvas.addEventListener('focus', () => { if (canvas.width) showCursor(); });
  canvas.addEventListener('blur', () => { cursor.hidden = true; });
  canvas.addEventListener('keydown', (ev) => {
    if (!canvas.width) return;
    const step = ev.shiftKey ? 0.1 : 0.02;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (moves[ev.key]) {
      ev.preventDefault();
      kx = Math.min(1, Math.max(0, kx + moves[ev.key][0]));
      ky = Math.min(1, Math.max(0, ky + moves[ev.key][1]));
      showCursor();
    } else if (ev.key === ' ' || ev.key === 'Enter') {
      ev.preventDefault();
      snapshot();
      dab(kx * canvas.width, ky * canvas.height);
      changed();
    } else if (ev.key === 'e' || ev.key === 'E') {
      ev.preventDefault();
      setTool(tool === 'brush' ? 'eraser' : 'brush');
      status.textContent = tool === 'brush' ? 'Brush selected.' : 'Eraser selected.';
    } else if ((ev.key === 'z' || ev.key === 'Z') && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      undo();
    }
  });
  size.input.addEventListener('input', () => { if (document.activeElement === canvas) showCursor(); });

  function addRectangle() {
    rectError.hidden = true;
    const vals = [rx, ry, rw, rh].map((i) => Number(i.value));
    const [x, y, w, hh] = vals;
    if (vals.some((v) => !Number.isFinite(v)) || x < 0 || y < 0 || w <= 0 || hh <= 0 || x + w > 100 || y + hh > 100) {
      rectError.hidden = false;
      rectError.textContent = 'The rectangle must lie inside the image: left + width and top + height may not exceed 100.';
      rx.focus();
      return;
    }
    if (!canvas.width) return;
    snapshot();
    const r = { x: x / 100, y: y / 100, w: w / 100, h: hh / 100 };
    ctx.fillStyle = PAINT;
    ctx.fillRect(r.x * canvas.width, r.y * canvas.height, r.w * canvas.width, r.h * canvas.height);
    rects.push(r);
    renderRects();
    changed();
  }

  function renderRects() {
    rectList.replaceChildren(...rects.map((r, i) => h('li', { class: 'mask-rect-item' },
      h('span', {}, `Left ${Math.round(r.x * 100)}%, top ${Math.round(r.y * 100)}%, ${Math.round(r.w * 100)}% × ${Math.round(r.h * 100)}%`),
      iconButton('x', `Remove rectangle ${i + 1}`, () => removeRect(i)))));
  }

  function removeRect(i) {
    snapshot();
    const r = rects[i];
    ctx.clearRect(r.x * canvas.width, r.y * canvas.height, r.w * canvas.width, r.h * canvas.height);
    rects.splice(i, 1);
    renderRects();
    changed();
  }

  function clear() {
    if (canvas.width) {
      snapshot();
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
    rects.length = 0;
    painted = false;
    renderRects();
    changed();
  }

  el.setSource = (a, { silent = false } = {}) => {
    history.length = 0;
    rects.length = 0;
    painted = false;
    renderRects();
    if (!a) {
      canvas.width = 0; canvas.height = 0;
      img.removeAttribute('src');
      stageBox.hidden = true;
      changed({ silent });
      return;
    }
    const w = a.width || 1024;
    const hh = a.height || 1024;
    const scale = Math.min(1, MAX_SIDE / Math.max(w, hh));
    canvas.width = Math.max(16, Math.round(w * scale));
    canvas.height = Math.max(16, Math.round(hh * scale));
    stageBox.style.aspectRatio = `${w} / ${hh}`;
    stageBox.hidden = false;
    img.src = a.url || `/api/media/assets/${encodeURIComponent(a.id)}/file`;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    changed({ silent });
  };
  el.coverage = () => coverage;
  el.hasMask = () => coverage > 0;
  // { dataUrl, source, rects } or null. Thresholded: white = may change.
  el.exportMask = () => {
    if (!canvas.width || coverage <= 0) return null;
    const src = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    const out = document.createElement('canvas');
    out.width = canvas.width; out.height = canvas.height;
    const octx = out.getContext('2d');
    const img2 = octx.createImageData(out.width, out.height);
    for (let i = 0; i < src.length; i += 4) {
      const v = src[i + 3] >= 128 ? 255 : 0;
      img2.data[i] = v; img2.data[i + 1] = v; img2.data[i + 2] = v; img2.data[i + 3] = 255;
    }
    octx.putImageData(img2, 0, 0);
    const kind = painted && rects.length ? 'painted+rectangles' : (rects.length && !painted ? 'rectangles' : 'painted');
    return { dataUrl: out.toDataURL('image/png'), source: kind, rects: rects.map((r) => ({ ...r })) };
  };
  el.clear = clear;
  el.id = uid('mask');
  el.setSource(null, { silent: true });
  return el;
}
