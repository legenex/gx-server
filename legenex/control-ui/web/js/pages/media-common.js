// Shared helpers for the Create and Media Library pages.
import { api, upload } from '../api.js';
import { h, clear, toast, bytes, clock, stateBadge } from '../dom.js';

export const PHASES = ['queued', 'loading', 'generating', 'saving', 'ready'];
export const PHASE_TEXT = {
  queued: 'Queued', loading: 'Loading model', generating: 'Generating', saving: 'Saving', ready: 'Ready', failed: 'Failed',
};
export const OP_TEXT = {
  generate: 'Generated', edit: 'Edited', variation: 'Variation', i2v: 'Image → video', v2v: 'Edited video', upload: 'Uploaded',
};

export function phaseTrack(job) {
  const failed = job.phase === 'failed';
  const current = PHASES.indexOf(job.phase);
  return h('ol', { class: 'phase-track', 'aria-label': 'Job progress' },
    PHASES.map((p, i) => {
      let cls = 'todo';
      if (failed && i === Math.max(0, current)) cls = 'failed';
      else if (i < current || job.phase === 'ready') cls = 'done';
      else if (i === current) cls = 'active';
      return h('li', { class: `phase ${cls}`, 'aria-current': i === current ? 'step' : undefined }, PHASE_TEXT[p]);
    }),
    failed ? h('li', { class: 'phase failed' }, 'Failed') : null);
}

export function mediaEl(asset, { controls = true, thumb = false } = {}) {
  if (asset.type === 'video' && !thumb) {
    return h('video', {
      src: asset.url, controls, preload: 'metadata', playsinline: true, class: 'media-view',
      poster: asset.thumbnail_url, 'aria-label': asset.title || asset.prompt || 'video',
    });
  }
  return h('img', {
    src: thumb ? asset.thumbnail_url : asset.url, loading: 'lazy', decoding: 'async', class: thumb ? 'media-thumb' : 'media-view',
    alt: asset.title || asset.prompt || (asset.type === 'video' ? 'video thumbnail' : 'image'),
  });
}

export function metaRows(a) {
  const rows = [
    ['Asset', a.id], ['Type', `${a.type} (${a.ext})`], ['Operation', OP_TEXT[a.operation] || a.operation],
    ['Created', clock(a.created_at)], ['Model', a.model_alias || '—'], ['Model repository', a.model_repo || '—'],
    ['Model revision', a.model_revision || '—'], ['Workflow', a.workflow || '—'],
    ['Seed', a.seed ?? '—'], ['Steps', a.steps ?? '—'], ['Guidance', a.guidance ?? '—'], ['Strength', a.strength ?? '—'],
    ['Size', a.width && a.height ? `${a.width} × ${a.height}` : '—'],
    ['Duration', a.duration ? `${Number(a.duration).toFixed(2)} s` : (a.type === 'video' ? '—' : undefined)],
    ['FPS', a.fps ?? (a.type === 'video' ? '—' : undefined)],
    ['Frames', a.frame_count ? `${a.frame_count}${a.distinct_frames ? ` (${a.distinct_frames} distinct)` : ''}` : (a.type === 'video' ? '—' : undefined)],
    ['File size', bytes(a.file_size)], ['SHA-256', a.sha256 ? `${a.sha256.slice(0, 16)}…` : '—'],
  ];
  return rows.filter((r) => r[1] !== undefined);
}

export async function uploadSource(file, type) {
  if (!file) throw new Error('Choose a file first.');
  const want = type === 'video' ? /^video\// : /^image\//;
  if (!want.test(file.type)) throw new Error(`Choose ${type === 'video' ? 'a video (MP4, MOV, WebM)' : 'an image (PNG, JPEG, WebP)'}.`);
  const limit = type === 'video' ? 150 : 25;
  if (file.size > limit * 1024 * 1024) throw new Error(`The file is larger than ${limit} MB.`);
  return upload('/api/media/upload', file, { title: file.name });
}

// A modal picker over the library, filtered by type. Resolves to an asset or null.
export function pickAsset(type) {
  return new Promise((resolve) => {
    const dlg = h('dialog', { class: 'picker', 'aria-labelledby': 'picker-title' });
    const grid = h('div', { class: 'media-grid compact', role: 'list' });
    const search = h('input', { type: 'search', placeholder: 'Search prompts or titles', 'aria-label': 'Search library' });
    const close = (value) => { dlg.close(); dlg.remove(); resolve(value); };
    const load = async () => {
      const data = await api.get(`/api/media/assets?type=${type}&limit=60&q=${encodeURIComponent(search.value)}`);
      clear(grid);
      if (!data.items.length) grid.append(h('p', { class: 'muted' }, `No ${type}s in the library yet.`));
      for (const a of data.items) {
        grid.append(h('button', {
          type: 'button', class: 'media-tile', role: 'listitem', onclick: () => close(a),
          'aria-label': `Use ${a.title || a.prompt || a.id}`,
        }, mediaEl(a, { thumb: true }), h('span', { class: 'tile-caption' }, a.title || a.prompt || a.id)));
      }
    };
    let timer;
    search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 250); });
    dlg.append(
      h('h2', { id: 'picker-title' }, `Choose a source ${type}`),
      search, grid,
      h('div', { class: 'dialog-actions' }, h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => close(null) }, 'Cancel')),
    );
    dlg.addEventListener('cancel', () => resolve(null));
    document.body.append(dlg);
    dlg.showModal();
    search.focus();
    load().catch((err) => toast(err.message, 'crit'));
  });
}

export async function pollMediaJob(id, onUpdate, { signal } = {}) {
  for (;;) {
    if (signal && signal.aborted) return null;
    const job = await api.get(`/api/media/jobs/${id}`, { signal });
    onUpdate(job);
    if (job.phase === 'ready' || job.phase === 'failed') return job;
    await new Promise((r) => setTimeout(r, 2500));
  }
}

export function assetBadge(a) {
  return stateBadge(a.type === 'video' ? 'running' : 'ready', `${a.type} · ${OP_TEXT[a.operation] || a.operation}`);
}
