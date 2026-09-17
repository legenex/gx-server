// Music studio (gx-music, ACE-Step): create, remix, repaint, extend.
// Controls are built from GET /api/music/model -> capabilities; anything the
// model does not list is not rendered.
import { api, getAsset, getMusicModel, searchAssets, upload } from '../api.js';
import { audioPlayer } from '../audio.js';
import { OP_LABEL, deleteWithConfirm, detailsDrawer, downloadButtons, emitAsset, onAsset, pickAsset, recipeOf, renameAsset, sourceChip, toggleFavourite } from '../assets.js';
import { clear, h, mmss, titleOf, toast, truncate, uid, replace } from '../dom.js';
import { icon } from '../icons.js';
import { center, friendlyError, isMusic, jobCard, phaseOf, submitMusic } from '../jobs.js';
import { navigate } from '../nav.js';
import {
  badge, button, callout, chips, composer, disclosure, dropzone, emptyState, field, iconButton, kv, numberInput,
  pageHeader, readNumber, seedField, select, skeletonLines, slider, tabs, textInput, toggle,
} from '../ui.js';

const SECTIONS = ['Intro', 'Verse', 'Pre-Chorus', 'Chorus', 'Post-Chorus', 'Bridge', 'Hook', 'Breakdown', 'Drop', 'Build',
  'Interlude', 'Instrumental', 'Solo', 'Guitar Solo', 'Outro', 'Fade Out'];
const COMMON_KEYS = ['C major', 'G major', 'D major', 'A major', 'E major', 'F major', 'Bb major', 'A minor', 'E minor', 'D minor', 'B minor', 'F# minor', 'C# minor', 'G minor', 'C minor'];
const TS_LABEL = { 2: '2/4', 3: '3/4', 4: '4/4', 6: '6/8' };
const TS_BACK = { '2/4': '2', '3/4': '3', '4/4': '4', '6/8': '6' };
const GROUP_LABEL = { genre: 'Genre', mood: 'Mood', instrument: 'Instruments', vocal: 'Vocals', production: 'Production', tempo: 'Tempo', era: 'Era' };
const LANG_LABEL = { en: 'English', de: 'German', es: 'Spanish', fr: 'French', ja: 'Japanese', ko: 'Korean', zh: 'Chinese', it: 'Italian', pt: 'Portuguese', ru: 'Russian', unknown: 'Any / unspecified' };
const AUDIO_EXT = { wav: 'audio/wav', flac: 'audio/flac', mp3: 'audio/mpeg', ogg: 'audio/ogg', m4a: 'audio/mp4' };
const MODE_OPS = [['create', 'generate', 'Create', 'sparkles'], ['remix', 'remix', 'Remix/Cover', 'remix'], ['repaint', 'edit', 'Repaint', 'scissors'], ['extend', 'extend', 'Extend', 'extend']];

const session = { jobs: [], done: new Set(), tracks: [], mode: 'create', sources: {}, reference: null, focusId: null, locks: new Set() };
const VOCAL_WORDS = /\b(vocals?|vocalist|singers?|singing|voices?|rap|rapper|choir|choral|duet|spoken word)\b/i;
const NO_VOCALS = /\b(no|without|non)[ -]+(\w+[ -]+)?(vocals?|voices?|singing|lyrics)\b/gi;

// ------------------------------------------------------------ components
function lyricsEditor({ maxLength = 4096, label = 'Lyrics', placeholder = '[Verse]\nWrite your lines here…' } = {}) {
  const id = uid('lyrics');
  const ta = h('textarea', { id, class: 'input lyrics-input', rows: 8, maxlength: maxLength, placeholder, spellcheck: 'true' });
  const count = h('span', { class: 'composer-count' }, `0 / ${maxLength}`);
  ta.addEventListener('input', () => { count.textContent = `${ta.value.length} / ${maxLength}`; });
  const insert = (name) => {
    const start = ta.selectionStart ?? ta.value.length;
    const end = ta.selectionEnd ?? start;
    const before = ta.value.slice(0, start);
    const after = ta.value.slice(end);
    const prefix = before && !before.endsWith('\n') ? '\n' : '';
    const tag = `${prefix}[${name}]\n`;
    ta.value = `${before}${tag}${after}`;
    const pos = before.length + tag.length;
    ta.focus();
    ta.setSelectionRange(pos, pos);
    ta.dispatchEvent(new Event('input'));
  };
  const bar = h('div', { class: 'section-bar', role: 'toolbar', 'aria-label': 'Insert a song section at the cursor' },
    SECTIONS.map((s) => {
      const b = h('button', { type: 'button', class: 'chip chip-sm section-btn', dataset: { section: s } }, `[${s}]`);
      // Keep the textarea's cursor: do not steal focus on mousedown.
      b.addEventListener('mousedown', (ev) => ev.preventDefault());
      b.addEventListener('click', () => insert(s));
      return b;
    }));
  const el = h('div', { class: 'field lyrics-field' },
    h('div', { class: 'field-row' }, h('label', { class: 'field-label', for: id }, label), count),
    bar, ta);
  el.textarea = ta;
  el.get = () => ta.value;
  el.set = (v) => { ta.value = v || ''; ta.dispatchEvent(new Event('input')); };
  return el;
}

function numberField(label, spec, { hint, placeholder = 'Auto', unit } = {}) {
  const input = numberInput({ min: spec.min, max: spec.max, step: spec.type === 'integer' ? 1 : 'any', placeholder, value: spec.value });
  const range = spec.min !== undefined && spec.max !== undefined ? `${spec.min}–${spec.max}${unit ? ` ${unit}` : ''}` : '';
  const el = field(label, input, { hint: [hint, range].filter(Boolean).join(' · ') || undefined });
  el.input = input;
  el.read = () => {
    const v = readNumber(input, { integer: spec.type === 'integer' });
    if (v === null) return null;
    if (spec.min !== undefined && v < spec.min) throw new Error(`${label} must be at least ${spec.min}.`);
    if (spec.max !== undefined && v > spec.max) throw new Error(`${label} must be at most ${spec.max}.`);
    return v;
  };
  return el;
}

function engineBadge(model) {
  const st = (model.engine && model.engine.state) || 'unknown';
  const tone = { ready: 'ok', loading: 'info', unloading: 'warn', failed: 'danger', unloaded: 'neutral' }[st] || 'neutral';
  const words = { ready: 'Engine ready', loading: 'Engine loading', unloading: 'Engine unloading', failed: 'Engine failed', unloaded: 'Engine idle · loads with your first job' }[st] || `Engine ${st}`;
  return badge(words, tone);
}

function guessAudioType(file) {
  if (file.type && file.type.startsWith('audio/')) return file.type;
  const ext = (file.name.split('.').pop() || '').toLowerCase();
  return AUDIO_EXT[ext] || '';
}

// ------------------------------------------------------------ page
export default {
  title: 'Music',
  async mount(root, ctx) {
    replace(root, pageHeader('Music', 'gx-music · songs, remixes, repaint and extend'), skeletonLines(6));
    let model;
    try {
      model = await getMusicModel();
    } catch (err) {
      replace(root, pageHeader('Music', 'gx-music'),
        callout('danger', 'The music studio is not reachable', friendlyError(err.message).text, [
          button('Try again', { icon: 'refresh', onClick: () => navigate('music') })]));
      return undefined;
    }
    const caps = model.capabilities || {};
    const ops = caps.operations || {};
    const C = caps.controls || {};
    const RC = caps.remix_controls || {};
    const EC = caps.edit_controls || {};
    const XC = caps.extend_controls || {};
    const identity = model.identity || {};
    let tagGroups = {};
    try { tagGroups = (await api.get('/api/music/tags?limit=50')).groups || {}; } catch { tagGroups = {}; }

    const modes = MODE_OPS.filter(([, op]) => ops[op]);
    if (!modes.some(([m]) => m === session.mode)) session.mode = modes[0] ? modes[0][0] : 'create';
    if (ctx.query.mode && modes.some(([m]) => m === ctx.query.mode)) session.mode = ctx.query.mode;
    let alive = true;

    // ---------------------------------------------------- results column
    const jobsBox = h('div', { class: 'ws-jobs', id: 'music-jobs', 'aria-live': 'polite' });
    const trackList = h('div', { class: 'track-list', id: 'track-list' });
    const results = h('div', { class: 'stage' }, jobsBox,
      h('section', { class: 'ws-section', 'aria-labelledby': 'tracks-h' },
        h('div', { class: 'row-between' }, h('h2', { class: 'section-title', id: 'tracks-h' }, 'Tracks'),
          h('a', { class: 'link small', href: '#/library?type=audio' }, 'All tracks in Library')),
        trackList));

    let libraryTracks = [];
    const allTracks = () => {
      const seen = new Set();
      return [...session.tracks, ...libraryTracks].filter((a) => (seen.has(a.id) ? false : seen.add(a.id)));
    };

    function trackCard(asset) {
      const a = asset;
      const s = a.settings || {};
      const timings = s.timings || {};
      const meta = [a.duration ? mmss(a.duration) : null, a.bpm ? `${Math.round(a.bpm)} BPM` : null, a.music_key, a.time_signature].filter(Boolean);
      const lineage = h('div', { class: 'track-lineage' });
      const loadLineage = async () => {
        clear(lineage);
        try {
          const full = a.children || a.ancestors ? a : await getAsset(a.id);
          const parent = (full.ancestors || [])[0];
          const kids = full.children || [];
          if (!parent && !kids.length) { lineage.append(h('p', { class: 'muted small' }, 'No parent or derived tracks.')); return; }
          if (parent) lineage.append(h('p', { class: 'small' }, 'From: ', parent.deleted ? h('span', { class: 'muted' }, 'a deleted track')
            : h('button', { type: 'button', class: 'link-btn', onclick: () => detailsDrawer(parent.id) }, parent.title || truncate(parent.prompt, 40) || parent.id)));
          if (kids.length) lineage.append(h('p', { class: 'small' }, 'Derived: ', kids.map((k, i) => [i ? ', ' : '', h('button', { type: 'button', class: 'link-btn', onclick: () => detailsDrawer(k.id) }, `${OP_LABEL[k.operation] || k.operation}: ${k.title || k.id}`)])));
        } catch (err) {
          lineage.append(h('p', { class: 'muted small' }, err.message));
        }
      };
      const more = disclosure('Details, lyrics and lineage', h('div', { class: 'stack-sm' },
        kv([
          ['Seed', a.seed ?? undefined],
          ['Steps', a.steps ?? undefined],
          ['Model', a.model_repo ? `${a.model_repo}${a.model_revision ? ` @ ${a.model_revision.slice(0, 10)}` : ''}` : a.model_alias],
          ['Language model', s.lm_model || undefined],
          ['Render time', timings.generate_s ? `${Number(timings.generate_s).toFixed(1)} s` : undefined],
          ['Sample rate', a.sample_rate ? `${a.sample_rate} Hz · ${a.channels === 1 ? 'mono' : 'stereo'}` : undefined],
          ['Operation', OP_LABEL[a.operation] || a.operation],
        ]),
        a.lyrics ? h('pre', { class: 'lyrics', tabindex: '0', 'aria-label': 'Lyrics' }, a.lyrics) : null,
        lineage), { ic: 'info' });
      more.addEventListener('toggle', () => { if (more.open && !lineage.firstChild) loadLineage(); }, { once: false });
      const canRecipe = Boolean(recipeOf(a));
      const act = (label, ic, fn, extra = {}) => button(label, { icon: ic, size: 'sm', variant: extra.variant || 'ghost', attrs: { 'data-action': extra.action }, onClick: fn });
      const card = h('article', { class: `track-card${session.focusId === a.id ? ' is-focus' : ''}`, dataset: { asset: a.id }, 'aria-label': `Track ${titleOf(a)}` },
        h('header', { class: 'track-head' },
          h('div', { class: 'track-art', 'aria-hidden': 'true' }, icon('music', { size: 20 })),
          h('div', { class: 'track-titles' },
            h('h3', { class: 'track-title' }, titleOf(a)),
            h('p', { class: 'muted small' }, [OP_LABEL[a.operation] || a.operation, ...meta].join(' · '))),
          h('div', { class: 'row-tight' },
            iconButton('heart', a.favourite ? 'Remove from favourites' : 'Add to favourites', async () => refresh(await toggleFavourite(a)),
              { pressed: a.favourite, attrs: { class: `icon-btn icon-btn-ghost fav-btn${a.favourite ? ' is-on' : ''}`, 'data-action': 'favourite' } }),
            iconButton('edit', 'Rename', async () => refresh(await renameAsset(a)), { attrs: { 'data-action': 'rename' } }),
            iconButton('info', 'Details', () => detailsDrawer(a), { attrs: { 'data-action': 'details' } }),
            iconButton('trash', 'Delete', () => deleteWithConfirm(a), { attrs: { class: 'icon-btn icon-btn-ghost danger', 'data-action': 'delete' } }))),
        audioPlayer(a, { label: titleOf(a) }),
        a.tags && a.tags.length ? h('div', { class: 'tag-row' }, a.tags.map((t) => h('span', { class: 'tag tag-static' }, t))) : null,
        a.prompt ? h('p', { class: 'track-prompt' }, a.prompt) : null,
        h('div', { class: 'track-actions' },
          canRecipe && s.operation === 'generate' ? act('Reuse settings', 'sliders', () => reuse(a), { action: 'reuse' }) : null,
          canRecipe ? act('Variation', 'shuffle', () => variation(a), { action: 'variation' }) : null,
          ops.remix ? act('Remix', 'remix', () => useSource('remix', a), { action: 'remix', variant: 'secondary' }) : null,
          ops.edit ? act('Repaint', 'scissors', () => useSource('repaint', a), { action: 'repaint' }) : null,
          ops.extend ? act('Extend', 'extend', () => useSource('extend', a), { action: 'extend' }) : null,
          h('span', { class: 'track-dl' }, downloadButtons(a))),
        more);
      return card;
    }

    function renderTracks() {
      clear(trackList);
      const list = allTracks();
      if (!list.length) {
        trackList.append(emptyState({ icon: 'music', title: 'No tracks yet', text: 'Describe a song on the left and press Create. Finished tracks appear here with a player and downloads.' }));
        return;
      }
      for (const a of list.slice(0, 16)) trackList.append(trackCard(a));
      const focus = session.focusId && trackList.querySelector(`[data-asset="${session.focusId}"]`);
      if (focus) focus.scrollIntoView({ block: 'nearest' });
    }

    function refresh(a) {
      if (!a) return;
      session.tracks = session.tracks.map((x) => (x.id === a.id ? a : x));
      libraryTracks = libraryTracks.map((x) => (x.id === a.id ? a : x));
      renderTracks();
    }

    async function loadLibrary() {
      try {
        const res = await searchAssets({ type: 'audio', limit: 12 });
        if (alive) { libraryTracks = res.items || []; renderTracks(); }
      } catch { /* the Library is optional here */ }
    }

    async function importJob(job) {
      const fresh = [];
      for (const id of job.library_assets || []) {
        if (session.tracks.some((t) => t.id === id)) continue;
        try { fresh.push(await getAsset(id)); } catch { /* deleted */ }
      }
      if (!alive || !fresh.length) return;
      session.tracks = [...fresh, ...session.tracks].slice(0, 24);
      session.focusId = fresh[0].id;
      renderTracks();
    }

    const cards = new Map();
    function showJob(job) {
      if (cards.has(job.id)) return;
      const c = jobCard(job, { onRetry: (n) => trackJob(n) });
      cards.set(job.id, c);
      jobsBox.prepend(c);
      while (jobsBox.children.length > 4) { cards.delete(jobsBox.lastElementChild.dataset.job); jobsBox.lastElementChild.remove(); }
    }
    function trackJob(job) {
      if (!session.jobs.includes(job.id)) session.jobs.unshift(job.id);
      showJob(job);
    }
    const onJob = (job) => {
      if (!alive || !job || !isMusic(job)) return;
      if (!session.jobs.includes(job.id)) {
        if (phaseOf(job).terminal) return;
        trackJob(job);
      }
      if (phaseOf(job).key === 'COMPLETE' && job.imported && !session.done.has(job.id)) {
        session.done.add(job.id);
        importJob(job);
      }
    };
    const unsubJobs = center.subscribe(onJob);
    const unsubAssets = onAsset((a, deleted) => {
      if (a.type !== 'audio') return;
      if (deleted) {
        session.tracks = session.tracks.filter((x) => x.id !== a.id);
        libraryTracks = libraryTracks.filter((x) => x.id !== a.id);
        renderTracks();
      } else refresh(a);
    });

    // ---------------------------------------------------- source pickers
    function sourcePicker(key, { label, required = true }) {
      const box = h('div', { class: 'source-box' });
      const drop = dropzone({
        accept: 'audio/*,.wav,.flac,.mp3,.ogg,.m4a', label: 'Upload audio', hint: 'WAV, FLAC, MP3, OGG or M4A · up to 64 MB',
        onFile: (f) => doUpload(f, (a) => set(a), drop),
      });
      const set = (a) => {
        if (key === 'reference') session.reference = a; else session.sources[key] = a;
        replace(box, a ? sourceChip(a, { label, onClear: () => set(null) }) : h('p', { class: 'muted small' }, required ? 'Choose a track to work on.' : 'Optional.'));
        if (el.onChange) el.onChange(a);
      };
      const el = h('div', { class: 'panel-section' },
        h('p', { class: 'field-label' }, label), box,
        h('div', { class: 'row-wrap' }, button('Choose from Library', { icon: 'library', size: 'sm', onClick: async () => {
          const a = await pickAsset({ type: 'audio', title: `Choose ${label.toLowerCase()}` });
          if (a) set(a);
        } })), drop);
      el.set = set;
      el.get = () => (key === 'reference' ? session.reference : session.sources[key]) || null;
      queueMicrotask(() => set(el.get()));
      return el;
    }

    async function doUpload(file, done, drop) {
      const type = guessAudioType(file);
      if (!type) { toast('Choose a WAV, FLAC, MP3, OGG or M4A file.', 'danger'); return; }
      if (file.size > 64 * 1024 * 1024) { toast('That file is larger than 64 MB.', 'danger'); return; }
      const blob = file.type === type ? file : new Blob([file], { type });
      drop.progress(0);
      try {
        const asset = await upload('/api/music/upload', blob, { title: file.name.replace(/\.[^.]+$/, ''), filename: file.name, onProgress: (f) => drop.progress(f) });
        drop.progress(null);
        emitAsset(asset);
        done(asset);
        toast('Uploaded to the Library.', 'ok');
      } catch (err) {
        drop.progress(null);
        toast(friendlyError(err.message).text, 'danger');
      }
    }

    // ---------------------------------------------------- forms
    const forms = {};
    const errors = {};
    const formError = (key) => { errors[key] = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true }); return errors[key]; };
    const showError = (key, msg) => { errors[key].hidden = false; errors[key].textContent = msg; };

    // Create — in conditioning order
    const cf = {};
    const locks = session.locks;
    const changed = () => { if (cf.preview) cf.preview.refresh(); if (cf.vocals) syncVocalStatus(); };
    const withLock = (comp, name) => {
      const lbl = comp.querySelector('label');
      const row = h('div', { class: 'field-row' });
      comp.insertBefore(row, lbl);
      row.append(lbl, lockButton(name, locks, changed));
      return comp;
    };
    if (ops.generate) {
      cf.ai = aiPanel({ readForm: () => readForm(), applySettings: (st, o) => applySettings(st, o), locks });
      cf.referencePanel = referencePanel({
        pickAsset: () => pickAsset({ type: 'audio', title: 'Choose a reference track' }),
        uploadFile: (f, drop, done) => doUpload(f, done, drop),
        applySettings: (st, o) => applySettings(st, o),
      });
      cf.reference = disclosure('Analyze Reference (upload, Library, YouTube, Spotify)', cf.referencePanel, { ic: 'wave' });
      cf.description = C.description ? withLock(composer({ label: 'Song description — what song should be created?', placeholder: 'An emotional song about leaving Cape Town after the end of a relationship.', maxLength: C.description.max_length || 512, rows: 2, onSubmit: () => submit('create'), id: 'music-description' }), 'description') : null;
      cf.tags = C.style_tags ? styleTagEditor({ groups: tagGroups, max: C.style_tags.max_items || 24, lock: lockButton('style_tags', locks, changed), onChange: changed }) : null;
      cf.prompt = C.prompt ? withLock(composer({ label: 'Style prompt — how should it sound?', placeholder: 'Intimate close-mic female vocal, soft piano opening, gradually expanding strings, restrained percussion, powerful final chorus.', maxLength: C.prompt.max_length || 512, rows: 3, onSubmit: () => submit('create'), id: 'music-prompt' }), 'style_prompt') : null;
      cf.vocals = vocalControls({ languages: (C.vocal_language && C.vocal_language.values) || [], langLabel: LANG_LABEL, locks, onChange: changed });
      cf.lyrics = C.lyrics ? lyricsEditor({ maxLength: C.lyrics.max_length || 4096 }) : null;
      if (cf.lyrics) {
        cf.lyrics.querySelector('.field-row').append(lockButton('lyrics', locks, changed));
        cf.lyrics.textarea.addEventListener('input', debounceChanged());
      }
      cf.duration = C.duration ? numberField('Duration', C.duration, { unit: 's', hint: 'Seconds' }) : null;
      cf.bpm = C.bpm ? numberField('BPM', C.bpm) : null;
      const keyList = uid('keys');
      cf.key = C.key ? textInput({ placeholder: C.key.example || 'e.g. F# minor', maxLength: 16, attrs: { list: keyList } }) : null;
      cf.ts = C.time_signature ? select([['', 'Auto'], ...(C.time_signature.values || []).map((v) => [v, (C.time_signature.labels || {})[v] || TS_LABEL[v] || v])], '') : null;
      cf.seed = C.seed ? seedField('music') : null;
      cf.batch = C.batch_size ? chips(Array.from({ length: Math.min(8, C.batch_size.max || 1) - (C.batch_size.min || 1) + 1 }, (_, i) => String((C.batch_size.min || 1) + i)).map((v) => [v, v]), { value: '1', label: 'Tracks per run' }) : null;
      cf.steps = C.inference_steps ? numberField('Inference steps', C.inference_steps, { placeholder: C.inference_steps.default ? `Default ${C.inference_steps.default}` : 'Default' }) : null;
      cf.sampler = C.infer_method ? select([['', 'Default'], ...(C.infer_method.values || []).map((v) => [v, v.toUpperCase()])], '') : null;
      cf.thinking = C.thinking ? toggle('Planner “thinking”', C.thinking.default !== false, { hint: 'The language model plans the song (audio codes) before rendering. Needed when the planner writes lyrics or the caption.' }) : null;
      cf.enhance = C.enhance_prompt ? toggle('Enhance prompt', Boolean(C.enhance_prompt.default), { hint: 'Lets the planner expand a short style prompt and format the lyrics before rendering.' }) : null;
      cf.rewrite = C.lm_caption_rewrite ? toggle('Let the planner rewrite the caption', Boolean(C.lm_caption_rewrite.default), { hint: 'Off (default): the caption shown under “What ACE-Step receives” is sent exactly.' }) : null;
      cf.lmTemp = C.lm_temperature ? numberField('Planner temperature', { ...C.lm_temperature, type: 'number' }, { placeholder: 'Default', hint: 'Language-model creativity' }) : null;
      cf.lmCfg = C.lm_cfg_scale ? numberField('Planner CFG (language model)', { ...C.lm_cfg_scale, type: 'number' }, { placeholder: 'Default', hint: 'Classifier-free guidance of the language-model planner, not of the music model' }) : null;
      cf.lmTopP = C.lm_top_p ? numberField('Planner top-p', { ...C.lm_top_p, type: 'number' }, { placeholder: 'Default' }) : null;
      cf.guidance = C.guidance_scale ? numberField('Music model guidance (DiT CFG)', { ...C.guidance_scale, type: 'number' }, { placeholder: 'Default' }) : null;
      cf.format = C.output_format ? select([['', 'Default'], ...(C.output_format.values || []).map((v) => [v, v.toUpperCase()])], '') : null;
      cf.title = textInput({ maxLength: 200, placeholder: 'Optional' });
      cf.referenceTrack = C.reference ? sourcePicker('reference', { label: 'Reference track for the sound (optional)', required: false }) : null;
      const keyHelper = cf.key ? h('div', { class: 'stack-sm' },
        h('datalist', { id: keyList }, COMMON_KEYS.map((k) => h('option', { value: k }))),
        h('div', { class: 'chips chips-sm', role: 'group', 'aria-label': 'Common keys' },
          COMMON_KEYS.slice(0, 8).map((k) => h('button', { type: 'button', class: 'chip chip-sm', onclick: () => { cf.key.value = k; changed(); } }, k)))) : null;
      for (const input of [cf.duration && cf.duration.input, cf.bpm && cf.bpm.input, cf.key]) if (input) input.addEventListener('input', debounceChanged());
      for (const el of [cf.ts, cf.thinking && cf.thinking.input, cf.enhance && cf.enhance.input, cf.rewrite && cf.rewrite.input]) if (el) el.addEventListener('change', changed);
      for (const c of [cf.description, cf.prompt]) if (c) c.textarea.addEventListener('input', debounceChanged());
      cf.instrumentalLyrics = () => {
        const on = cf.vocals.instrumental.input.checked;
        if (cf.lyrics) {
          cf.lyrics.textarea.disabled = on;
          for (const b of cf.lyrics.querySelectorAll('.section-btn')) b.disabled = on;
        }
      };
      cf.vocals.instrumental.input.addEventListener('change', cf.instrumentalLyrics);
      cf.preview = conditioningPreview(() => buildBody({ preview: true }));
      const stepHead = (n, text) => h('p', { class: 'step-head' }, h('span', { class: 'step-num', 'aria-hidden': 'true' }, String(n)), text);
      forms.create = h('div', { class: 'stack', id: 'form-create' },
        cf.ai, cf.reference,
        stepHead(1, 'What the song is'), cf.description,
        stepHead(2, 'How it sounds'), cf.tags, cf.prompt,
        stepHead(3, 'Vocals and lyrics'), cf.vocals, cf.lyrics,
        stepHead(4, 'Technical'),
        h('div', { class: 'grid-2' },
          cf.duration ? withFieldLock(cf.duration, 'duration') : null, cf.bpm ? withFieldLock(cf.bpm, 'bpm') : null,
          cf.key ? field('Key', cf.key, { hint: 'e.g. C major, A minor, F# minor', extra: lockButton('key', locks, changed) }) : null,
          cf.ts ? field('Time signature', cf.ts, { extra: lockButton('time_signature', locks, changed) }) : null),
        keyHelper,
        h('div', { class: 'grid-2' }, cf.batch ? h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Tracks per run'), cf.batch) : null, field('Title', cf.title, { extra: lockButton('title', locks, changed) })),
        cf.seed, cf.referenceTrack,
        disclosure('Advanced (planner and sampler)', h('div', { class: 'stack' },
          h('div', { class: 'grid-2' }, cf.steps, cf.sampler ? field('Sampler', cf.sampler, { hint: 'ODE is deterministic; SDE adds variety.' }) : null),
          cf.thinking, cf.enhance, cf.rewrite,
          h('div', { class: 'grid-2' }, cf.lmTemp, cf.lmCfg, cf.lmTopP, cf.guidance,
            cf.format ? field('Preferred format', cf.format) : null)), { ic: 'sliders' }),
        cf.preview,
        formError('create'));
      if (C.description === undefined) cf.ai.hidden = true;
    }

    function withFieldLock(numField, name) {
      numField.querySelector('.field-row').append(lockButton(name, locks, changed));
      return numField;
    }

    function debounceChanged() {
      let t = null;
      return () => { clearTimeout(t); t = setTimeout(changed, 350); };
    }

    function syncVocalStatus() {
      const st = cf.vocals.status;
      const f = safeForm();
      if (!f) return;
      const source = cf.vocals.source.value;
      const words = (f.lyrics || '').split('\n').some((l) => l.trim() && !/^\s*\[[^\]]*\]\s*$/.test(l));
      const asked = f.vocal_intent !== 'auto' || VOCAL_WORDS.test(`${f.style_prompt} ${f.style_tags.join(' ')} ${f.description}`.replace(NO_VOCALS, ' '));
      let text = '';
      if (f.instrumental) text = 'Instrumental: no vocals will be rendered.';
      else if (words) text = 'Vocals: your lyrics will be sung.';
      else if (source === 'assistant') text = 'Vocals: gx-auto writes the lyrics when you press Create.';
      else if (source === 'planner') text = 'Vocals: ACE-Step’s planner writes the lyrics while rendering.';
      else if (asked) text = 'Vocals need lyrics. Write them, pick “Write with AI”, or turn Instrumental on.';
      else text = 'No lyrics: the track will be instrumental.';
      st.textContent = text;
      st.classList.toggle('form-danger', !f.instrumental && !words && source === 'user' && asked);
    }

    function safeForm() {
      try { return readForm(); } catch { return null; }
    }

    // The form in the field names Build with AI / Improve / flows use.
    function readForm() {
      const num = (f) => (f ? f.read() : null);
      const tsValue = cf.ts ? cf.ts.value : '';
      return {
        title: cf.title.value.trim(),
        description: cf.description ? cf.description.get() : '',
        style_tags: cf.tags ? cf.tags.getValue() : [],
        style_prompt: cf.prompt ? cf.prompt.get() : '',
        instrumental: cf.vocals.instrumental.input.checked,
        vocal_intent: cf.vocals.intent.getValue(),
        vocal_language: cf.vocals.language.value,
        lyrics: cf.lyrics ? cf.lyrics.get().trim() : '',
        bpm: num(cf.bpm),
        key: cf.key && cf.key.value.trim() ? cf.key.value.trim() : null,
        time_signature: tsValue ? (TS_LABEL[tsValue] || tsValue) : null,
        duration: num(cf.duration),
        seed: cf.seed && cf.seed.isLocked() ? cf.seed.current() : null,
        thinking: cf.thinking ? cf.thinking.input.checked : null,
        inference_steps: num(cf.steps),
        infer_method: cf.sampler && cf.sampler.value ? cf.sampler.value : null,
        lm_temperature: num(cf.lmTemp),
      };
    }

    // Settings (from AI, a reference or Undo) -> the form. Locked fields stay unless forced.
    function applySettings(st, { force = false } = {}) {
      const take = (name) => Object.prototype.hasOwnProperty.call(st, name) && (force || !locks.has(name));
      const setNum = (f, v) => { if (f) f.input.value = v === null || v === undefined ? '' : String(v); };
      if (take('title')) cf.title.value = st.title || '';
      if (take('description') && cf.description) cf.description.set(st.description || '');
      if (take('style_tags') && cf.tags) cf.tags.setValue(st.style_tags || []);
      if (take('style_prompt') && cf.prompt) cf.prompt.set(st.style_prompt || '');
      if (take('instrumental')) cf.vocals.instrumental.input.checked = Boolean(st.instrumental);
      if (take('vocal_intent')) cf.vocals.intent.setValue(st.vocal_intent || 'auto');
      if (take('vocal_language')) {
        const has = [...cf.vocals.language.options].some((o) => o.value === (st.vocal_language || ''));
        cf.vocals.language.value = has ? (st.vocal_language || '') : '';
      }
      if (take('lyrics') && cf.lyrics && (st.lyrics || force || st.instrumental)) cf.lyrics.set(st.instrumental && !force ? '' : (st.lyrics || ''));
      if (take('bpm')) setNum(cf.bpm, st.bpm);
      if (take('duration')) setNum(cf.duration, st.duration);
      if (take('key') && cf.key) cf.key.value = st.key || '';
      if (take('time_signature') && cf.ts) cf.ts.value = st.time_signature ? (TS_BACK[st.time_signature] || String(st.time_signature)) : '';
      if (take('seed') && cf.seed && st.seed !== null && st.seed !== undefined && st.seed !== cf.seed.current()) cf.seed.set(st.seed, true);
      if (take('thinking') && cf.thinking && typeof st.thinking === 'boolean') cf.thinking.input.checked = st.thinking;
      if (take('inference_steps')) setNum(cf.steps, st.inference_steps);
      if (take('infer_method') && cf.sampler) cf.sampler.value = st.infer_method || '';
      if (take('lm_temperature')) setNum(cf.lmTemp, st.lm_temperature);
      cf.vocals.sync();
      cf.instrumentalLyrics();
      changed();
    }

    // Remix
    const rf = {};
    if (ops.remix) {
      rf.source = sourcePicker('remix', { label: 'Source track' });
      rf.prompt = composer({ label: 'New style', placeholder: 'the same song as a jazz trio, brushed drums', maxLength: (C.prompt && C.prompt.max_length) || 512, rows: 3, onSubmit: () => submit('remix'), id: 'remix-prompt' });
      rf.tags = C.style_tags ? styleTagEditor({ groups: {}, max: C.style_tags.max_items || 24 }) : null;
      if (rf.tags) rf.tags.id = 'remix-tags';
      rf.lyrics = C.lyrics ? lyricsEditor({ label: 'Lyrics override (optional)', placeholder: 'Leave empty to keep the original lyrics' }) : null;
      const st = RC.strength || { min: 0, max: 1, default: 0.5 };
      rf.strength = slider({ label: 'Remix strength', min: st.min ?? 0, max: st.max ?? 1, step: 0.05, value: st.default ?? 0.5, format: (v) => v.toFixed(2), hint: 'Low stays close to the source (a faithful cover); high reinvents it more freely.' });
      const ns = RC.noise_strength;
      rf.noise = ns ? slider({ label: 'Noise strength', min: ns.min ?? 0, max: ns.max ?? 1, step: 0.05, value: ns.default ?? 0, format: (v) => v.toFixed(2), hint: 'Extra noise added to the source before re-rendering.' }) : null;
      rf.seed = seedField('music-remix');
      rf.title = textInput({ maxLength: 200, placeholder: 'Optional' });
      forms.remix = h('div', { class: 'stack', id: 'form-remix' }, rf.source, rf.prompt, rf.tags, rf.lyrics, rf.strength, rf.noise, rf.seed, field('Title', rf.title), formError('remix'));
    }

    // Repaint
    const pf = {};
    if (ops.edit) {
      pf.source = sourcePicker('repaint', { label: 'Source track' });
      pf.waveBox = h('div', { class: 'repaint-wave' });
      pf.start = numberInput({ min: 0, step: 0.1, placeholder: '0.0' });
      pf.end = numberInput({ min: 0, step: 0.1, placeholder: '0.0' });
      pf.prompt = composer({ label: 'What should this section become?', placeholder: 'a soaring guitar solo', maxLength: (C.prompt && C.prompt.max_length) || 512, rows: 2, onSubmit: () => submit('repaint'), id: 'repaint-prompt' });
      pf.lyrics = C.lyrics ? lyricsEditor({ label: 'Lyrics for the section (optional)', placeholder: '[Bridge]\n…' }) : null;
      const modeSpec = EC.mode || { values: ['conservative', 'balanced', 'aggressive'] };
      pf.mode = chips((modeSpec.values || []).map((v) => [v, v.charAt(0).toUpperCase() + v.slice(1)]), { value: (modeSpec.values || [])[1] || (modeSpec.values || [])[0], label: 'Repaint mode' });
      const es = EC.strength || { min: 0, max: 1 };
      pf.strength = slider({ label: 'Strength', min: es.min ?? 0, max: es.max ?? 1, step: 0.05, value: es.default ?? 0.7, format: (v) => v.toFixed(2), hint: 'How strongly the selected section is re-generated.' });
      pf.seed = seedField('music-repaint');
      pf.title = textInput({ maxLength: 200, placeholder: 'Optional' });
      const syncSel = () => {
        const a = readNumber(pf.start);
        const b = readNumber(pf.end);
        if (pf.player && a !== null && b !== null) pf.player.wave.setSelection([a, b]);
      };
      pf.start.addEventListener('input', syncSel);
      pf.end.addEventListener('input', syncSel);
      pf.source.onChange = (a) => {
        clear(pf.waveBox);
        pf.player = null;
        if (!a) return;
        pf.player = audioPlayer(a, {
          label: titleOf(a), selectable: true,
          onSelect: ([s, e]) => { pf.start.value = s.toFixed(1); pf.end.value = e.toFixed(1); },
        });
        const dur = a.duration || 0;
        if (dur && !pf.end.value) { pf.start.value = (dur * 0.25).toFixed(1); pf.end.value = (dur * 0.5).toFixed(1); }
        pf.waveBox.append(pf.player, h('p', { class: 'field-hint' }, 'Drag across the waveform to choose the section, or type the times below. Click to seek.'));
        syncSel();
      };
      forms.repaint = h('div', { class: 'stack', id: 'form-repaint' }, pf.source, pf.waveBox,
        h('div', { class: 'grid-2' }, field('Start (s)', pf.start), field('End (s)', pf.end)),
        pf.prompt, pf.lyrics,
        h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Mode'), pf.mode),
        pf.strength, pf.seed, field('Title', pf.title), formError('repaint'));
    }

    // Extend
    const xf = {};
    if (ops.extend) {
      xf.source = sourcePicker('extend', { label: 'Source track' });
      const secs = XC.seconds || { min: 5, max: 240 };
      xf.seconds = numberField('Seconds to add', { ...secs, type: 'number', value: Math.max(secs.min || 5, Math.min(secs.max || 240, 30)) }, { placeholder: '30', unit: 's' });
      const dirs = (XC.direction && XC.direction.values) || ['end', 'start'];
      xf.direction = chips(dirs.map((d) => [d, d === 'end' ? 'After the end' : d === 'start' ? 'Before the start' : d]), { value: dirs[0], label: 'Direction' });
      xf.prompt = composer({ label: 'Style for the new part (optional)', placeholder: 'a big final chorus with strings', maxLength: (C.prompt && C.prompt.max_length) || 512, rows: 2, onSubmit: () => submit('extend'), id: 'extend-prompt' });
      xf.lyrics = C.lyrics ? lyricsEditor({ label: 'Lyrics for the new part (optional)', placeholder: '[Outro]\n…' }) : null;
      xf.seed = seedField('music-extend');
      xf.title = textInput({ maxLength: 200, placeholder: 'Optional' });
      forms.extend = h('div', { class: 'stack', id: 'form-extend' }, xf.source, xf.seconds,
        h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Direction'), xf.direction),
        xf.prompt, xf.lyrics, xf.seed, field('Title', xf.title), formError('extend'));
    }

    const submitBtn = button('Create', { icon: 'sparkles', variant: 'primary', attrs: { id: 'music-submit', class: 'btn btn-primary btn-lg btn-block' }, onClick: () => submit(session.mode) });
    const modeTabs = tabs(modes.map(([m, , label, ic]) => [m, label, ic]), session.mode, (m) => setMode(m), { label: 'Music mode' });
    const panelBody = h('div', { class: 'panel-body' }, Object.values(forms));
    const panel = h('aside', { class: 'panel panel-music', 'aria-label': 'Music settings' }, modeTabs, panelBody, h('div', { class: 'panel-foot' }, submitBtn));

    const sub = `${identity.dit_name || identity.dit_repo || 'ACE-Step'} · songs, remixes, repaint and extend`;
    replace(root,
      pageHeader('Music', sub, [engineBadge(model)]),
      modes.length ? h('div', { class: 'workspace' }, panel, results)
        : callout('warn', 'Music is not available', 'The music model reports no supported operations right now.'));

    function setMode(m) {
      session.mode = m;
      modeTabs.select(m);
      for (const [k, f] of Object.entries(forms)) f.hidden = k !== m;
      const label = { create: 'Create', remix: 'Remix', repaint: 'Repaint section', extend: 'Extend' }[m];
      submitBtn.querySelector('span').textContent = label;
    }

    function useSource(mode, asset) {
      const picker = { remix: rf.source, repaint: pf.source, extend: xf.source }[mode];
      if (!picker) return;
      picker.set(asset);
      setMode(mode);
      panel.scrollIntoView({ block: 'start', behavior: 'smooth' });
      const target = { remix: rf.prompt, repaint: pf.prompt, extend: xf.prompt }[mode];
      if (target) target.textarea.focus({ preventScroll: true });
      toast(`${titleOf(asset)} is the source. Adjust the settings, then submit.`, 'ok', 3000);
    }

    function reuse(asset) {
      const r = recipeOf(asset);
      if (!r || !ops.generate) return;
      const b = r.body;
      applySettings({
        title: b.title || '', description: b.description || '', style_tags: b.style_tags || asset.tags || [],
        style_prompt: b.prompt || '', instrumental: Boolean(b.instrumental), vocal_intent: b.vocal_intent || 'auto',
        vocal_language: b.vocal_language || '', lyrics: b.instrumental ? '' : (b.lyrics || ''), bpm: b.bpm ?? null,
        key: b.key || null, time_signature: b.time_signature ? (TS_LABEL[b.time_signature] || b.time_signature) : null,
        duration: b.duration ?? null, seed: b.seed ?? asset.seed ?? null, thinking: typeof b.thinking === 'boolean' ? b.thinking : null,
        inference_steps: b.inference_steps ?? null, infer_method: b.infer_method || null, lm_temperature: b.lm_temperature ?? null,
      }, { force: true });
      cf.vocals.source.value = 'user';
      setMode('create');
      toast('Settings loaded into Create.', 'ok', 2500);
    }

    async function variation(asset) {
      const r = recipeOf(asset);
      if (!r) return;
      const body = { ...r.body };
      delete body.seed;
      try {
        trackJob(await submitMusic(body));
        toast('Variation started with a new seed.', 'ok');
      } catch (err) {
        toast(friendlyError(err.message).text, 'danger');
      }
    }

    function buildBody({ preview = false } = {}) {
      const f = readForm();
      const body = { operation: 'generate' };
      if (f.description) body.description = f.description;
      if (f.style_prompt) body.prompt = f.style_prompt;
      if (f.style_tags.length) body.style_tags = f.style_tags;
      body.instrumental = f.instrumental;
      const source = cf.vocals.source.value;
      if (!f.instrumental) {
        if (f.lyrics) body.lyrics = f.lyrics;
        if (f.vocal_intent !== 'auto') body.vocal_intent = f.vocal_intent;
        if (f.vocal_language) body.vocal_language = f.vocal_language;
        if (source !== 'user') body.lyrics_source = source;
      }
      if (!f.description && !f.style_prompt && !f.style_tags.length) {
        if (preview) return null;
        throw new Error('Add a song description, style tags or a style prompt.');
      }
      if (f.duration !== null) body.duration = f.duration;
      if (f.bpm !== null) body.bpm = f.bpm;
      if (f.key) body.key = f.key;
      if (cf.ts && cf.ts.value) body.time_signature = cf.ts.value;
      if (cf.batch) body.batch_size = Number(cf.batch.getValue());
      if (f.inference_steps !== null) body.inference_steps = f.inference_steps;
      if (f.infer_method) body.infer_method = f.infer_method;
      if (cf.thinking) body.thinking = cf.thinking.input.checked;
      if (cf.enhance) body.enhance_prompt = cf.enhance.input.checked;
      if (cf.rewrite && cf.rewrite.input.checked) body.lm_caption_rewrite = true;
      for (const [k, fl] of [['lm_temperature', cf.lmTemp], ['lm_cfg_scale', cf.lmCfg], ['lm_top_p', cf.lmTopP], ['guidance_scale', cf.guidance]]) {
        const v = fl ? fl.read() : null;
        if (v !== null) body[k] = v;
      }
      if (preview) return body;
      if (cf.format && cf.format.value) body.output_format = cf.format.value;
      if (cf.seed) body.seed = cf.seed.next();
      if (f.title) body.title = f.title;
      if (session.reference) body.reference_asset_id = session.reference.id;
      return body;
    }

    function buildRemix() {
      const src = session.sources.remix;
      if (!src) throw new Error('Choose the source track to remix.');
      const body = { operation: 'remix', source_asset_id: src.id, strength: rf.strength.getValue(), seed: rf.seed.next() };
      if (rf.prompt.get()) body.prompt = rf.prompt.get();
      const tags = rf.tags ? rf.tags.getValue() : [];
      if (tags.length) body.style_tags = tags;
      if (rf.lyrics && rf.lyrics.get().trim()) body.lyrics = rf.lyrics.get().trim();
      if (rf.noise) body.noise_strength = rf.noise.getValue();
      if (rf.title.value.trim()) body.title = rf.title.value.trim();
      return body;
    }

    function buildRepaint() {
      const src = session.sources.repaint;
      if (!src) throw new Error('Choose the track to repaint.');
      const start = readNumber(pf.start);
      const end = readNumber(pf.end);
      if (start === null || end === null) throw new Error('Choose the section: set a start and an end time.');
      if (end <= start) throw new Error('The end of the section must be after its start.');
      if (src.duration && start >= src.duration) throw new Error('The section starts after the end of the track.');
      const body = { operation: 'edit', source_asset_id: src.id, start, end, mode: pf.mode.getValue(), strength: pf.strength.getValue(), seed: pf.seed.next() };
      if (pf.prompt.get()) body.prompt = pf.prompt.get();
      if (pf.lyrics && pf.lyrics.get().trim()) body.lyrics = pf.lyrics.get().trim();
      if (pf.title.value.trim()) body.title = pf.title.value.trim();
      return body;
    }

    function buildExtend() {
      const src = session.sources.extend;
      if (!src) throw new Error('Choose the track to extend.');
      const seconds = xf.seconds.read();
      if (seconds === null) throw new Error('Enter how many seconds to add.');
      const body = { operation: 'extend', source_asset_id: src.id, seconds, direction: xf.direction.getValue(), seed: xf.seed.next() };
      if (xf.prompt.get()) body.prompt = xf.prompt.get();
      if (xf.lyrics && xf.lyrics.get().trim()) body.lyrics = xf.lyrics.get().trim();
      if (xf.title.value.trim()) body.title = xf.title.value.trim();
      return body;
    }

    async function submit(mode) {
      const err = errors[mode];
      if (err) err.hidden = true;
      let body;
      try {
        body = { create: () => buildBody(), remix: buildRemix, repaint: buildRepaint, extend: buildExtend }[mode]();
      } catch (e) {
        showError(mode, e.message);
        return;
      }
      submitBtn.disabled = true;
      try {
        const job = await submitMusic(body);
        trackJob(job);
        toast('Queued on gx-music.', 'ok', 2000);
      } catch (e) {
        showError(mode, friendlyError(e.message).text);
      } finally {
        submitBtn.disabled = false;
      }
    }

    setMode(session.mode);
    for (const id of session.jobs.slice(0, 4).reverse()) {
      const j = center.jobs.get(id);
      if (j) { showJob(j); onJob(j); }
    }
    for (const j of center.active()) onJob(j);
    renderTracks();
    loadLibrary();

    if (ctx.query.source) {
      try {
        const a = await getAsset(ctx.query.source);
        if (a.type === 'audio') useSource(modes.some(([m]) => m === ctx.query.mode) && ctx.query.mode !== 'create' ? ctx.query.mode : 'remix', a);
      } catch (e) { toast(e.message, 'danger'); }
    }
    if (ctx.query.asset) {
      try {
        const a = await getAsset(ctx.query.asset);
        if (a.type === 'audio') {
          session.tracks = [a, ...session.tracks.filter((t) => t.id !== a.id)];
          session.focusId = a.id;
          renderTracks();
        }
      } catch (e) { toast(e.message, 'danger'); }
    }
    if (ctx.query.focus) {
      const t = (cf.description || cf.prompt);
      if (t) t.textarea.focus();
    }

    if (cf.preview) { cf.vocals.sync(); changed(); }
    return () => { alive = false; unsubJobs(); unsubAssets(); if (cf.referencePanel) cf.referencePanel.stop(); };
  },
};
