// Asset actions and views shared by the workspaces and the Library:
// favourite, rename, delete, download, duplicate, lightbox, compare,
// details drawer with lineage, and the Library picker.
import { api, deleteAssets, getAsset, searchAssets, updateAsset } from './api.js';
import { audioPlayer, miniWave } from './audio.js';
import { ago, bytes, clear, confirmDialog, dateTime, debounce, h, mmss, openDialog, openDrawer, promptDialog, titleOf, toast, truncate, replace } from './dom.js';
import { friendlyError, submitMedia, submitMusic, submitVideo, submitVoice } from './jobs.js';
import { musicBodyFromRequest } from './music-recipe.js';
import { openAsset } from './nav.js';
import { assetThumb, button, emptyState, hasThumb, iconButton, kv, linkButton, loading, skeletonGrid, textInput } from './ui.js';

export const OP_LABEL = {
  generate: 'Generated', edit: 'Edit', variation: 'Variation', i2v: 'Image to video', v2v: 'Video edit',
  upload: 'Upload', remix: 'Remix', repaint: 'Repaint', extend: 'Extend',
  tts: 'Speech', voice_design: 'Voice design', voice_clone: 'Voice clone',
};
export const TYPE_LABEL = { image: 'Image', video: 'Video', audio: 'Audio' };

export function emitAsset(asset, deleted = false) {
  window.dispatchEvent(new CustomEvent('gx-asset', { detail: { asset, deleted } }));
}

export function onAsset(fn) {
  const handler = (ev) => fn(ev.detail.asset, ev.detail.deleted);
  window.addEventListener('gx-asset', handler);
  return () => window.removeEventListener('gx-asset', handler);
}

export async function toggleFavourite(asset) {
  try {
    const next = await updateAsset(asset.id, { favourite: !asset.favourite });
    emitAsset(next);
    toast(next.favourite ? 'Added to favourites.' : 'Removed from favourites.', 'ok', 2500);
    return next;
  } catch (err) {
    toast(err.message, 'danger');
    return asset;
  }
}

export async function renameAsset(asset) {
  const title = await promptDialog({ title: 'Rename', label: 'Title', value: asset.title || '' });
  if (title === null) return asset;
  try {
    const next = await updateAsset(asset.id, { title });
    emitAsset(next);
    toast('Renamed.', 'ok', 2500);
    return next;
  } catch (err) {
    toast(err.message, 'danger');
    return asset;
  }
}

export async function deleteWithConfirm(assets) {
  const list = Array.isArray(assets) ? assets : [assets];
  if (!list.length) return false;
  const one = list.length === 1;
  const ok = await confirmDialog({
    title: one ? 'Delete this item?' : `Delete ${list.length} items?`,
    message: one
      ? `“${titleOf(list[0])}” will be removed from the Library, including every format of it. This cannot be undone.`
      : `${list.length} items will be removed from the Library. This cannot be undone.`,
    okLabel: 'Delete', danger: true,
  });
  if (!ok) return false;
  try {
    const res = await deleteAssets(list.map((a) => a.id));
    for (const a of list) emitAsset(a, true);
    toast(`${(res.deleted || []).length || list.length} deleted.`, 'ok');
    return true;
  } catch (err) {
    toast(err.message, 'danger');
    return false;
  }
}

export function downloadButtons(asset, { size = 'sm' } = {}) {
  if (asset.type === 'audio') {
    const d = asset.downloads || {};
    return ['wav', 'flac', 'mp3'].filter((f) => d[f]).map((f) => linkButton(f.toUpperCase(), d[f], {
      icon: 'download', size, variant: 'secondary', download: '', attrs: { 'aria-label': `Download ${f.toUpperCase()}`, 'data-format': f },
    }));
  }
  return [linkButton('Download', asset.download_url, { icon: 'download', size, variant: 'secondary', download: '' })];
}

// The recipe that re-creates an asset, or null (uploads, unknown origin).
export function recipeOf(asset) {
  const s = asset.settings || {};
  if (asset.source_kind === 'voice_take') {
    // Build V3 VOI. A clone is never re-run from the Library: it needs a fresh permission confirmation.
    if (!s.request || s.operation === 'voice_clone') return null;
    return { type: 'voice', body: { ...s.request } };
  }
  if (asset.type === 'audio') {
    const req = s.request;
    if (!req || !s.operation) return null;
    const body = musicBodyFromRequest(req, s.operation);
    if (s.operation !== 'generate') {
      if (!asset.parent_id || asset.parent_deleted) return null;
      body.source_asset_id = asset.parent_id;
    }
    if (s.reference_asset) body.reference_asset_id = s.reference_asset;
    if (asset.title) body.title = asset.title.replace(/ \(\d+\)$/, '');
    return { type: 'music', body };
  }
  const req = s.requested;
  if (!req || !req.kind) return null;
  if (req.wan && req.wan.request) return { type: 'video', body: { ...req.wan.request } };
  const body = { ...req };
  if (body.source_id && asset.parent_deleted) return null;
  // Only the mask's metadata is stored: a masked edit is repeated from the Images page.
  if (body.mask && typeof body.mask === 'object') return null;
  return { type: 'media', body };
}

export async function duplicateAsset(asset, { newSeed = false } = {}) {
  const r = recipeOf(asset);
  if (!r) {
    const masked = asset.settings && asset.settings.requested && asset.settings.requested.mask;
    toast(masked ? 'This edit used a mask: open it in Images, paint the mask again and apply.' : 'This item has no recipe to run again.', 'warn');
    return null;
  }
  const body = { ...r.body };
  if (newSeed) delete body.seed;
  try {
    const submit = { music: submitMusic, video: submitVideo, voice: submitVoice }[r.type] || submitMedia;
    const job = await submit(body);
    toast('Submitted. Follow it in Activity.', 'ok');
    return job;
  } catch (err) {
    toast(friendlyError(err.message).text, 'danger');
    return null;
  }
}

// ------------------------------------------------------------ media views
export function mediaView(asset, { autoplay = false } = {}) {
  if (asset.type === 'video') {
    return h('video', {
      class: 'media-video', src: asset.url, controls: true, preload: 'metadata', playsinline: true,
      poster: hasThumb(asset) ? asset.thumbnail_url : undefined, autoplay, 'aria-label': `Video: ${titleOf(asset)}`,
    });
  }
  if (asset.type === 'audio') return audioPlayer(asset);
  return h('img', { class: 'media-img', src: asset.url, alt: asset.prompt ? truncate(asset.prompt, 140) : titleOf(asset), decoding: 'async' });
}

export function lightbox(list, index = 0) {
  let i = index;
  const stage = h('div', { class: 'lightbox-stage' });
  const caption = h('p', { class: 'lightbox-caption' });
  const prev = iconButton('chevronLeft', 'Previous', () => show(i - 1), { variant: 'glass' });
  const next = iconButton('chevronRight', 'Next', () => show(i + 1), { variant: 'glass' });
  const body = h('div', { class: 'lightbox' }, stage, caption, list.length > 1 ? h('div', { class: 'lightbox-nav' }, prev, next) : null);
  function show(n) {
    i = (n + list.length) % list.length;
    const a = list[i];
    replace(stage, mediaView(a, { autoplay: false }));
    caption.textContent = `${titleOf(a)}${list.length > 1 ? ` · ${i + 1} of ${list.length}` : ''}`;
  }
  const dlg = openDialog({ body, className: 'dialog-lightbox', label: 'Fullscreen preview' });
  dlg.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowRight' && list.length > 1) { ev.preventDefault(); show(i + 1); }
    if (ev.key === 'ArrowLeft' && list.length > 1) { ev.preventDefault(); show(i - 1); }
  });
  show(i);
  return dlg;
}

export function compareDialog(a, b) {
  const range = h('input', { type: 'range', min: '0', max: '100', value: '50', class: 'range compare-range', 'aria-label': 'Compare position' });
  const top = h('img', { class: 'compare-img compare-top', src: b.url, alt: `B: ${titleOf(b)}` });
  const bottom = h('img', { class: 'compare-img', src: a.url, alt: `A: ${titleOf(a)}` });
  const handle = h('div', { class: 'compare-handle', 'aria-hidden': 'true' });
  const frame = h('div', { class: 'compare-frame' }, bottom, top, handle);
  const sync = () => {
    const v = Number(range.value);
    top.style.clipPath = `inset(0 0 0 ${v}%)`;
    handle.style.left = `${v}%`;
  };
  range.addEventListener('input', sync);
  sync();
  let side = false;
  const grid = h('div', { class: 'compare-side', hidden: true },
    h('figure', {}, h('img', { src: a.url, alt: '' }), h('figcaption', {}, `A · ${titleOf(a)}`)),
    h('figure', {}, h('img', { src: b.url, alt: '' }), h('figcaption', {}, `B · ${titleOf(b)}`)));
  const modeBtn = button('Side by side', { icon: 'compare', size: 'sm', variant: 'ghost' });
  modeBtn.addEventListener('click', () => {
    side = !side;
    grid.hidden = !side;
    frame.hidden = side;
    range.hidden = side;
    modeBtn.querySelector('span').textContent = side ? 'Slider' : 'Side by side';
  });
  return openDialog({
    title: 'Compare', className: 'dialog-wide',
    body: h('div', { class: 'stack' },
      h('div', { class: 'row-between' }, h('p', { class: 'muted' }, `A: ${titleOf(a)} · B: ${titleOf(b)}`), modeBtn),
      frame, range, grid),
  });
}

// ------------------------------------------------------------ lineage
function treeList(nodes, currentId, onPick) {
  if (!nodes || !nodes.length) return null;
  return h('ul', { class: 'tree' }, nodes.map((n) => {
    const b = n.brief || n;
    const kids = n.children || [];
    return h('li', {},
      h('button', { type: 'button', class: `tree-node${b.id === currentId ? ' is-current' : ''}`, 'aria-current': b.id === currentId ? 'true' : undefined, onclick: () => onPick(b) },
        h('span', { class: 'tree-op' }, OP_LABEL[b.operation] || b.operation || ''),
        h('span', { class: 'tree-title' }, b.title || truncate(b.prompt, 48) || b.id)),
      treeList(kids, currentId, onPick));
  }));
}

export async function lineageView(asset, onPick) {
  const box = h('div', { class: 'lineage' }, loading('Loading lineage…'));
  try {
    const data = await api.get(`/api/media/assets/${asset.id}/lineage`);
    const rootAsset = data.root === asset.id ? data.asset : null;
    const ancestors = (data.asset.ancestors || []).slice().reverse();
    const rootBrief = rootAsset ? rootAsset : ancestors.find((x) => x.id === data.root) || { id: data.root, title: 'Original' };
    clear(box);
    const hasFamily = ancestors.length || (data.tree && data.tree.length);
    if (!hasFamily) {
      box.append(h('p', { class: 'muted' }, 'This item has no parent and no derived versions yet.'));
      return box;
    }
    if (ancestors.some((x) => x.deleted)) box.append(h('p', { class: 'muted' }, 'An earlier version was deleted.'));
    box.append(treeList([{ ...rootBrief, children: data.tree || [] }], asset.id, onPick));
  } catch (err) {
    replace(box, h('p', { class: 'muted' }, `Lineage unavailable: ${err.message}`));
  }
  return box;
}

export function recipeRows(a) {
  const s = a.settings || {};
  const req = s.requested || s.request || {};
  const params = req.parameters || {};
  return [
    ['Prompt', a.prompt || req.prompt],
    ['Negative prompt', a.negative_prompt],
    ['Type', `${TYPE_LABEL[a.type] || a.type} · ${(a.ext || '').toUpperCase()}`],
    ['Operation', OP_LABEL[a.operation] || a.operation],
    ['Created', dateTime(a.created_at)],
    ['Seed', a.seed ?? req.seed ?? params.seed],
    ['Steps', a.steps ?? req.steps ?? params.inference_steps],
    ['Guidance', a.guidance ?? req.guidance],
    ['Strength', a.strength ?? req.strength ?? params.strength],
    ['Size', a.width && a.height ? `${a.width} × ${a.height}` : req.size],
    ['Quality', req.quality],
    ['Image model', s.image_model_label || req.image_model],
    ['Edit mode', s.edit ? `${s.edit.edit_mode}${s.edit.masked ? ' (masked)' : ''}` : req.edit_mode],
    ['Edit quality', req.edit_quality],
    ['Denoise', s.edit && typeof s.edit.denoise === 'number' ? s.edit.denoise.toFixed(2) : undefined],
    ['Mask', s.mask ? `${Math.round(s.mask.coverage * 1000) / 10}% of the image (${s.mask.source})` : undefined],
    ['Duration', a.duration ? mmss(a.duration) : (req.seconds ? `${req.seconds} s` : undefined)],
    ['FPS', a.fps ?? req.fps],
    ['BPM', a.bpm ? Math.round(a.bpm) : undefined],
    ['Key', a.music_key],
    ['Time signature', a.time_signature],
    ['Tags', a.tags && a.tags.length ? a.tags.join(', ') : undefined],
    ['Model', a.model_alias],
    ['Model repository', a.model_repo],
    ['Model revision', a.model_revision ? truncate(a.model_revision, 14) : undefined],
    ['Workflow', a.workflow],
    ['LoRAs', s.wan && s.wan.loras && s.wan.loras.length ? s.wan.loras.filter((l) => l.enabled).map((l) => `${l.display_name} (${[l.strength_high, l.strength_low].filter((x) => x !== null && x !== undefined).map((x) => Number(x).toFixed(2)).join(' / ')})`).join(', ') || 'none enabled' : undefined],
    ['Workflow version', s.wan ? s.wan.workflow_version : undefined],
    ['ComfyUI prompt id', s.wan ? s.wan.comfy_prompt_id : undefined],
    ['Parent', a.parent_id ? (a.parent_deleted ? `${a.parent_id} (deleted)` : a.parent_id) : undefined],
    ['File size', a.file_size ? bytes(a.file_size) : undefined],
    ['Asset ID', a.id],
  ];
}

// Details drawer: preview, actions, recipe, lineage.
export async function detailsDrawer(assetOrId, { actions } = {}) {
  let asset = typeof assetOrId === 'string' ? null : assetOrId;
  const body = h('div', { class: 'details' }, loading());
  const dlg = openDrawer({ title: 'Details', body, wide: true });
  try {
    asset = await getAsset(asset ? asset.id : assetOrId);
  } catch (err) {
    replace(body, emptyState({ icon: 'alert', title: 'Could not load this item', text: err.message }));
    return dlg;
  }
  const render = async () => {
    const lineage = await lineageView(asset, (b) => {
      if (b.id === asset.id) return;
      dlg.close();
      detailsDrawer(b.id, { actions });
    });
    const lyrics = asset.lyrics && asset.lyrics.trim() ? h('div', { class: 'details-section' },
      h('h3', { class: 'section-title' }, 'Lyrics'), h('pre', { class: 'lyrics', tabindex: '0' }, asset.lyrics)) : null;
    replace(body,
      h('div', { class: 'details-preview' }, mediaView(asset)),
      h('div', { class: 'details-head' },
        h('div', {}, h('p', { class: 'details-title' }, titleOf(asset)),
          h('p', { class: 'muted small' }, `${OP_LABEL[asset.operation] || asset.operation} · ${ago(asset.created_at)}`)),
        h('div', { class: 'row-wrap' },
          iconButton('heart', asset.favourite ? 'Remove from favourites' : 'Add to favourites', async () => {
            asset = await toggleFavourite(asset); render();
          }, { pressed: asset.favourite, attrs: { class: `icon-btn icon-btn-ghost fav-btn${asset.favourite ? ' is-on' : ''}` } }),
          iconButton('edit', 'Rename', async () => { asset = await renameAsset(asset); render(); }))),
      h('div', { class: 'row-wrap' },
        button('Open in workspace', { icon: 'sparkles', size: 'sm', variant: 'primary', onClick: () => { dlg.close(); openAsset(asset); } }),
        ...(actions ? actions(asset, dlg) : []),
        ...downloadButtons(asset),
        button('Delete', { icon: 'trash', size: 'sm', variant: 'danger-ghost', onClick: async () => {
          if (await deleteWithConfirm(asset)) dlg.close();
        } })),
      h('div', { class: 'details-section' }, h('h3', { class: 'section-title' }, 'Recipe'), kv(recipeRows(asset))),
      lyrics,
      h('div', { class: 'details-section' }, h('h3', { class: 'section-title' }, 'Lineage'), lineage));
  };
  await render();
  return dlg;
}

export async function lineageDrawer(asset) {
  const body = h('div', { class: 'details' });
  const dlg = openDrawer({ title: `Lineage · ${titleOf(asset)}`, body });
  body.append(await lineageView(asset, (b) => { dlg.close(); detailsDrawer(b.id); }));
  return dlg;
}

// ------------------------------------------------------------ picker
export function pickAsset({ type, title = 'Choose from Library' }) {
  return new Promise((resolve) => {
    let chosen = null;
    let offset = 0;
    const grid = h('div', { class: 'grid-media grid-pick', role: 'list' });
    const more = button('Load more', { variant: 'ghost', size: 'sm' });
    more.hidden = true;
    const search = textInput({ placeholder: `Search ${type === 'audio' ? 'tracks' : `${type}s`}…`, attrs: { type: 'search', 'aria-label': 'Search the Library' } });
    const load = async (reset) => {
      if (reset) { offset = 0; replace(grid, skeletonGrid(6)); }
      try {
        const res = await searchAssets({ type, q: search.value.trim(), limit: 24, offset });
        if (reset) clear(grid);
        for (const a of res.items) {
          grid.append(h('div', { role: 'listitem' }, h('button', {
            type: 'button', class: 'pick-tile', 'aria-label': `Choose ${titleOf(a)}`,
            onclick: () => { chosen = a; dlg.close('ok'); },
          }, a.type === 'audio' ? h('div', { class: 'pick-audio' }, miniWave(a.waveform), h('span', {}, a.duration ? mmss(a.duration) : '')) : assetThumb(a),
          h('span', { class: 'pick-title' }, titleOf(a)))));
        }
        offset += res.items.length;
        more.hidden = offset >= res.total;
        if (!res.total) grid.append(emptyState({ icon: type === 'audio' ? 'music' : 'image', title: 'Nothing here yet', text: 'Create or upload something first.' }));
      } catch (err) {
        replace(grid, emptyState({ icon: 'alert', title: 'Could not load the Library', text: err.message }));
      }
    };
    search.addEventListener('input', debounce(() => load(true), 300));
    more.addEventListener('click', () => load(false));
    const dlg = openDialog({
      title, className: 'dialog-wide',
      body: h('div', { class: 'stack' }, search, grid, h('div', { class: 'center' }, more)),
      onClose: () => resolve(chosen),
    });
    search.focus();
    load(true);
  });
}

// A compact "source" preview with change/clear controls.
export function sourceChip(asset, { onClear, label = 'Source' } = {}) {
  if (!asset) return null;
  return h('div', { class: 'source-chip' },
    h('div', { class: 'source-thumb' }, asset.type === 'audio' ? miniWave(asset.waveform) : assetThumb(asset, { lazy: false })),
    h('div', { class: 'source-meta' }, h('span', { class: 'source-label' }, label), h('span', { class: 'source-title' }, titleOf(asset)),
      asset.type === 'audio' && asset.duration ? h('span', { class: 'muted small' }, mmss(asset.duration)) : null),
    onClear ? iconButton('x', `Remove ${label.toLowerCase()}`, onClear) : null);
}

