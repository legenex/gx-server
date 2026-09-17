// Music Create form building blocks (MUS, build V3):
//   styleTagEditor   real tokens: add, edit, remove, reorder (keyboard + buttons), suggestions
//   lockButton       "keep this field" for Build with AI / Improve / Use Suggestions
//   vocalControls    instrumental, vocal type, language, who writes the lyrics
//   conditioningPreview   the exact caption / lyrics / vocal mode ACE-Step receives
//   aiPanel          Build with AI + Improve My Prompt with a change list and Undo
//   referencePanel   Analyze Reference (upload, Library, YouTube, Spotify) + Use Suggestions
// Everything is built with createElement/textContent (no HTML strings).
import { api } from './api.js';
import { clear, debounce, h, mmss, replace, toast, uid } from './dom.js';
import { icon } from './icons.js';
import { friendlyError } from './jobs.js';
import { badge, button, callout, chips, disclosure, dropzone, field, kv, select, textInput, toggle } from './ui.js';

export const SUGGESTED_TAGS = ['female vocals', 'male vocals', 'soulful', 'cinematic', 'deep house', 'Afro house', 'piano',
  'melancholic', 'energetic', '1980s', 'orchestral', 'electronic', 'dark', 'uplifting', 'acoustic', 'raspy vocal', 'breathy vocal'];
export const VOCAL_TYPES = [['auto', 'Auto'], ['female', 'Female'], ['male', 'Male'], ['duet', 'Duet'], ['choir', 'Choir'], ['rap', 'Rap'], ['spoken', 'Spoken']];
export const LYRIC_SOURCES = [
  ['user', 'I write them (required for vocals)'],
  ['assistant', 'Write with AI (gx-auto) if empty'],
  ['planner', 'Let the music planner (ACE-Step LM) write them if empty'],
];
const FIELD_LABEL = {
  title: 'Title', description: 'Song description', style_tags: 'Style tags', style_prompt: 'Style prompt',
  instrumental: 'Instrumental', vocal_intent: 'Vocal type', vocal_language: 'Vocal language', lyrics: 'Lyrics',
  bpm: 'BPM', key: 'Key', time_signature: 'Time signature', duration: 'Duration', seed: 'Seed',
  thinking: 'Planner thinking', inference_steps: 'Inference steps', infer_method: 'Sampler', lm_temperature: 'Planner temperature',
};
const ACTION_LABEL = { set: 'Set', filled: 'Filled', refined: 'Refined', added: 'Added tags', kept_locked: 'Kept (locked)', suggested: 'Suggestion only' };
const SOURCE_LABEL = { measured: 'Measured', model: 'Heard by ACE-Step', inferred: 'Inferred by gx-auto' };
const SOURCE_TONE = { measured: 'ok', model: 'info', inferred: 'neutral' };
export const TAG_MAX = 48;

const show = (v) => {
  if (v === null || v === undefined || v === '') return '—';
  if (Array.isArray(v)) return v.length ? v.join(', ') : '—';
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  const text = String(v);
  return text.length > 160 ? `${text.slice(0, 157)}…` : text;
};

// ---------------------------------------------------------------- locks
export function lockButton(fieldName, locks, onChange) {
  const label = FIELD_LABEL[fieldName] || fieldName;
  const b = h('button', {
    type: 'button', class: 'icon-btn icon-btn-ghost lock-btn', dataset: { lock: fieldName },
    'aria-pressed': 'false', 'aria-label': `Keep ${label} when using AI`, title: `Keep ${label} when using AI`,
  });
  const render = () => {
    const on = locks.has(fieldName);
    b.setAttribute('aria-pressed', String(on));
    b.classList.toggle('is-on', on);
    replace(b, icon(on ? 'lock' : 'unlock', { size: 16 }));
  };
  b.addEventListener('click', () => {
    if (locks.has(fieldName)) locks.delete(fieldName); else locks.add(fieldName);
    render();
    if (onChange) onChange();
  });
  render();
  b.sync = render;
  return b;
}

// ------------------------------------------------------------ style tags
export function styleTagEditor({ groups = {}, max = 24, lock, onChange } = {}) {
  let tags = [];
  let active = -1;
  const listId = uid('tag-dl');
  const live = h('p', { class: 'sr-only', 'aria-live': 'polite' });
  const list = h('ul', { class: 'token-list', 'aria-label': 'Selected style tags' });
  const empty = h('p', { class: 'muted small token-empty' }, 'No tags yet. Pick suggestions below or type your own.');
  const datalist = h('datalist', { id: listId });
  const input = textInput({ placeholder: 'Type a tag and press Enter', maxLength: TAG_MAX, attrs: { list: listId, id: uid('tag-in'), 'aria-describedby': '' } });
  const help = h('p', { class: 'field-hint', id: uid('tag-help') },
    'Enter or comma adds a tag. On a tag: Delete removes it, Alt + arrow keys move it, Enter edits it.');
  input.setAttribute('aria-describedby', help.id);
  const toolbar = h('div', { class: 'token-toolbar', role: 'toolbar', 'aria-label': 'Selected tag actions', hidden: true });
  const suggestionBox = h('div', { class: 'chips chips-sm', role: 'group', 'aria-label': 'Suggested style tags' });

  const announce = (text) => { live.textContent = text; };
  const emit = () => { if (onChange) onChange([...tags]); };
  const has = (t) => tags.some((x) => x.toLowerCase() === t.toLowerCase());
  const clean = (t) => String(t || '').replace(/,/g, ' ').replace(/\s+/g, ' ').trim().slice(0, TAG_MAX);

  function add(raw, { quiet = false } = {}) {
    const t = clean(raw);
    if (!t) return false;
    if (has(t)) { if (!quiet) announce(`${t} is already a tag`); return false; }
    if (tags.length >= max) { toast(`At most ${max} style tags.`, 'warn'); return false; }
    tags.push(t);
    render();
    announce(`Added ${t}. ${tags.length} tags.`);
    emit();
    return true;
  }

  function remove(i) {
    const [t] = tags.splice(i, 1);
    active = tags.length ? Math.min(i, tags.length - 1) : -1;
    render();
    announce(`Removed ${t}. ${tags.length} tags.`);
    emit();
    focusToken(active);
    if (active < 0) input.focus();
  }

  function move(i, delta) {
    const j = i + delta;
    if (j < 0 || j >= tags.length) return;
    [tags[i], tags[j]] = [tags[j], tags[i]];
    active = j;
    render();
    announce(`Moved ${tags[j]} to position ${j + 1} of ${tags.length}.`);
    emit();
    focusToken(j);
  }

  function edit(i) {
    const li = list.children[i];
    if (!li) return;
    const old = tags[i];
    const box = textInput({ value: old, maxLength: TAG_MAX, attrs: { 'aria-label': `Edit tag ${old}`, class: 'input token-edit' } });
    // Enter commits, then render() removes the <li> that holds this input, which
    // fires its own blur while the removal is still running. Without the latch
    // the blur handler re-enters render() and the outer clear() then fails with
    // "removeChild: the node to be removed is no longer a child of this node".
    let finished = false;
    const done = (commit) => {
      if (finished || !box.isConnected) return;
      finished = true;
      const t = clean(box.value);
      if (commit && t && t !== old && !tags.some((x, k) => k !== i && x.toLowerCase() === t.toLowerCase())) {
        tags[i] = t;
        announce(`Changed ${old} to ${t}.`);
        emit();
      }
      render();
      focusToken(i);
    };
    box.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); done(true); }
      if (ev.key === 'Escape') { ev.preventDefault(); done(false); }
    });
    box.addEventListener('blur', () => done(true));
    replace(li, box);
    box.focus();
    box.select();
  }

  function focusToken(i) {
    const btn = list.children[i] && list.children[i].querySelector('.token-text');
    if (btn) btn.focus();
  }

  function render() {
    clear(list);
    empty.hidden = tags.length > 0;
    tags.forEach((t, i) => {
      const text = h('button', {
        type: 'button', class: 'token-text', 'aria-pressed': String(i === active),
        'aria-label': `${t}, tag ${i + 1} of ${tags.length}`, dataset: { tag: t },
        onclick: () => { active = i === active ? -1 : i; render(); focusToken(i); },
      }, t);
      text.addEventListener('keydown', (ev) => {
        if (ev.key === 'Delete' || ev.key === 'Backspace') { ev.preventDefault(); remove(i); } else if (ev.altKey && ev.key === 'ArrowLeft') { ev.preventDefault(); move(i, -1); } else if (ev.altKey && ev.key === 'ArrowRight') { ev.preventDefault(); move(i, 1); } else if (ev.key === 'ArrowLeft') { ev.preventDefault(); focusToken(Math.max(0, i - 1)); } else if (ev.key === 'ArrowRight') {
          ev.preventDefault();
          if (i + 1 < tags.length) focusToken(i + 1); else input.focus();
        } else if (ev.key === 'Enter' || ev.key === 'F2') { ev.preventDefault(); edit(i); }
      });
      const x = h('button', { type: 'button', class: 'token-x', 'aria-label': `Remove tag ${t}`, onclick: () => remove(i) }, icon('x', { size: 12 }));
      list.append(h('li', { class: `token${i === active ? ' is-active' : ''}` }, text, x));
    });
    // toolbar for the selected tag: buttons for pointer and touch users
    toolbar.hidden = active < 0 || active >= tags.length;
    if (!toolbar.hidden) {
      const i = active;
      replace(toolbar,
        h('span', { class: 'small muted' }, `Selected: ${tags[i]}`),
        button('Move left', { icon: 'chevronLeft', size: 'sm', variant: 'ghost', attrs: { disabled: i === 0, 'data-action': 'tag-left' }, onClick: () => move(i, -1) }),
        button('Move right', { icon: 'chevronRight', size: 'sm', variant: 'ghost', attrs: { disabled: i === tags.length - 1, 'data-action': 'tag-right' }, onClick: () => move(i, 1) }),
        button('Edit', { icon: 'edit', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'tag-edit' }, onClick: () => edit(i) }),
        button('Remove', { icon: 'trash', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'tag-remove' }, onClick: () => remove(i) }));
    }
    for (const b of suggestionBox.children) b.setAttribute('aria-pressed', String(has(b.dataset.value)));
    for (const g of groupChips) g.setValue(tags.filter((t) => g.values.has(t)));
  }

  for (const t of SUGGESTED_TAGS) {
    const b = h('button', { type: 'button', class: 'chip', dataset: { value: t }, 'aria-pressed': 'false' }, t);
    b.addEventListener('click', () => {
      if (has(t)) remove(tags.findIndex((x) => x.toLowerCase() === t.toLowerCase())); else add(t);
    });
    suggestionBox.append(b);
  }

  const groupChips = [];
  const groupBox = h('div', { class: 'tag-groups' });
  for (const [name, values] of Object.entries(groups || {})) {
    if (!Array.isArray(values) || !values.length) continue;
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    const c = chips(values.map((t) => [t, t]), {
      multiple: true, value: [], label, cls: 'chips-sm',
      onChange: (vals) => {
        const set = new Set(vals);
        for (const t of values) {
          if (set.has(t) && !has(t)) add(t, { quiet: true });
          if (!set.has(t) && has(t)) tags = tags.filter((x) => x.toLowerCase() !== t.toLowerCase());
        }
        render();
        emit();
      },
    });
    c.values = new Set(values);
    groupChips.push(c);
    groupBox.append(h('div', { class: 'tag-group' }, h('p', { class: 'tag-group-name' }, label), c));
  }

  const suggest = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) return;
    try {
      const res = await api.get(`/api/music/tags?q=${encodeURIComponent(q)}&limit=10`);
      replace(datalist, ...(res.suggestions || []).map((s) => h('option', { value: s })));
    } catch { /* suggestions are a convenience */ }
  }, 250);
  input.addEventListener('input', () => {
    if (input.value.includes(',')) {
      const parts = input.value.split(',');
      input.value = parts.pop();
      parts.forEach((p) => add(p));
    }
    suggest();
  });
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); if (add(input.value)) input.value = ''; }
    if (ev.key === 'Backspace' && !input.value && tags.length) { ev.preventDefault(); active = tags.length - 1; render(); focusToken(active); }
  });
  const addBtn = button('Add', { size: 'sm', attrs: { 'data-action': 'tag-add' }, onClick: () => { if (add(input.value)) input.value = ''; input.focus(); } });
  const labelId = uid('tags-l');
  const el = h('section', { class: 'field token-field', id: 'music-tags', 'aria-labelledby': labelId },
    h('div', { class: 'field-row' }, h('h3', { class: 'field-label', id: labelId }, 'Style tags'), lock || null),
    h('p', { class: 'field-hint' }, 'Tags are added to the style caption ACE-Step renders from (see “What ACE-Step receives”).'),
    list, empty, toolbar, live,
    h('div', { class: 'input-group' }, input, datalist, addBtn), help,
    h('p', { class: 'tag-group-name' }, 'Suggestions'), suggestionBox,
    groupBox.children.length ? disclosure('More tags from the model vocabulary', groupBox, { ic: 'tag' }) : null);
  input.setAttribute('aria-label', 'Add a style tag');
  render();
  el.getValue = () => [...tags];
  el.setValue = (v) => {
    tags = [];
    for (const t of v || []) { const c = clean(t); if (c && !has(c) && tags.length < max) tags.push(c); }
    active = -1;
    render();
  };
  el.input = input;
  return el;
}

// ---------------------------------------------------------------- vocals
export function vocalControls({ languages = [], langLabel = {}, locks, onChange }) {
  const instrumental = toggle('Instrumental (no vocals)', false, { hint: 'On: no vocals and no lyrics. Off: the lyrics below are sung.' });
  const intent = chips(VOCAL_TYPES, { value: 'auto', label: 'Vocal type' });
  const intentHint = h('p', { class: 'field-hint' }, 'Adds the vocal type to the style caption. Vocals are only sung when there are lyrics.');
  const language = select([['', 'Auto (detected from the lyrics)'], ...languages.map((v) => [v, langLabel[v] || v])], '');
  const source = select(LYRIC_SOURCES, 'user', { attrs: { id: 'music-lyrics-source' } });
  const status = h('p', { class: 'vocal-status small', role: 'status' });
  const el = h('section', { class: 'field vocal-controls', 'aria-label': 'Vocals and lyrics' },
    instrumental,
    h('div', { class: 'field' }, h('div', { class: 'field-row' }, h('p', { class: 'field-label', id: 'vocal-type-label' }, 'Vocal type'),
      lockButton('vocal_intent', locks, onChange)), intent, intentHint),
    h('div', { class: 'grid-2' },
      field('Vocal language', language, { extra: lockButton('vocal_language', locks, onChange) }),
      field('When the lyrics are empty', source)),
    status);
  const sync = () => {
    const off = instrumental.input.checked;
    for (const b of intent.querySelectorAll('button')) b.disabled = off;
    language.disabled = off;
    source.disabled = off;
    if (onChange) onChange();
  };
  instrumental.input.addEventListener('change', sync);
  intent.addEventListener('click', () => { if (onChange) onChange(); });
  language.addEventListener('change', () => { if (onChange) onChange(); });
  source.addEventListener('change', () => { if (onChange) onChange(); });
  el.instrumental = instrumental;
  el.intent = intent;
  el.language = language;
  el.source = source;
  el.status = status;
  el.sync = sync;
  return el;
}

// ------------------------------------------------ what ACE-Step receives
export function conditioningPreview(readBody) {
  const out = h('div', { class: 'cond-body', 'aria-live': 'polite' });
  const el = h('section', { class: 'cond-preview', id: 'music-conditioning', 'aria-labelledby': 'cond-h' },
    h('div', { class: 'row-between' }, h('h3', { class: 'field-label', id: 'cond-h' }, 'What ACE-Step receives'),
      h('span', { class: 'small muted' }, 'Updates as you edit')), out);
  let seq = 0;
  const refresh = debounce(async () => {
    let body;
    try { body = readBody(); } catch (err) {
      replace(out, h('p', { class: 'small form-danger' }, err.message));
      return;
    }
    if (!body) { replace(out, h('p', { class: 'small muted' }, 'Add a description, tags or a style prompt.')); return; }
    const mine = ++seq;
    try {
      const res = await api.post('/api/music/preview', body);
      if (mine !== seq) return;
      const c = res.conditioning || {};
      const mode = {
        vocals: 'Vocals (your lyrics are sung)', planner_lyrics: 'Vocals (the music planner writes the lyrics)',
        instrumental: 'Instrumental', instrumental_no_lyrics: 'Instrumental (no lyrics and no vocal request)',
      }[c.vocal_mode] || c.vocal_mode;
      const meta = c.metadata || {};
      replace(out,
        h('p', { class: 'small field-label' }, 'Caption (style prompt + tags + vocal type + description)'),
        h('pre', { class: 'cond-caption', tabindex: '0', 'data-caption': '' }, c.caption || (c.planner ? '(written by the music planner from your description)' : '—')),
        h('p', { class: 'small muted' }, `${res.caption_length || 0} / ${res.caption_max || 512} characters`),
        kv([
          ['Vocals', mode + (res.lyrics_pending ? ' · lyrics written by gx-auto on Create' : '')],
          ['Vocal type', c.vocal_intent && c.vocal_intent !== 'auto' ? c.vocal_intent : 'Auto'],
          ['Language', c.instrumental ? '—' : `${c.vocal_language || 'en'}${c.language_detection ? ' (the planner may detect another)' : ''}`],
          ['Lyrics', c.instrumental ? '[Instrumental]' : (res.lyrics_pending ? 'written by gx-auto' : (c.lyrics ? `${c.lyrics.split('\n').filter((l) => l.trim() && !/^\s*\[.*\]\s*$/.test(l)).length} sung lines` : 'written by the planner'))],
          ['Planner writes', c.planner ? c.planner.writes.join(' and ') : 'nothing (your caption is sent as shown)'],
          ['Metadata', Object.entries(meta).map(([k, v]) => `${k.replace('audio_duration', 'duration').replace('key_scale', 'key')} ${v}`).join(' · ') || 'chosen by the planner'],
        ]),
        (c.notes || []).length ? h('ul', { class: 'small muted' }, c.notes.map((n) => h('li', {}, n))) : null);
      el.dataset.state = 'ok';
    } catch (err) {
      if (mine !== seq) return;
      el.dataset.state = 'error';
      replace(out, callout('warn', 'This would be refused', friendlyError(err.message).text));
    }
  }, 500);
  el.refresh = refresh;
  return el;
}

// --------------------------------------------------------- change lists
function changeList(changes) {
  const rows = changes.filter((c) => c.action !== 'set' || c.field !== 'seed');
  if (!rows.length) return h('p', { class: 'small muted' }, 'Nothing changed.');
  return h('ul', { class: 'change-list' }, rows.map((c) => h('li', { class: `change change-${c.action}` },
    h('span', { class: 'change-field' }, FIELD_LABEL[c.field] || c.field),
    badge(ACTION_LABEL[c.action] || c.action, c.action === 'kept_locked' ? 'warn' : c.action === 'suggested' ? 'neutral' : 'ok'),
    c.why ? h('span', { class: 'small muted' }, c.why) : null,
    c.action !== 'suggested' || c.after !== null ? h('span', { class: 'change-diff small' },
      h('del', {}, show(c.before)), ' → ', h('ins', {}, show(c.after))) : null)));
}

// --------------------------------------------------- Build with AI / Improve
export function aiPanel({ readForm, applySettings, locks }) {
  const prompt = h('textarea', {
    id: 'music-ai-prompt', class: 'input', rows: 3, maxlength: 1200,
    placeholder: 'e.g. Make me a dark but uplifting Afro house track for a luxury travel ad, female vocal, around 122 BPM, African percussion, emotional chorus, no cheesy EDM drop.',
  });
  const writeLyrics = toggle('Write lyrics too', true, { hint: 'Off: the lyrics field is left for you.' });
  const improveLyrics = toggle('Improve My Prompt may rewrite my lyrics', false);
  const note = textInput({ maxLength: 600, placeholder: 'Optional note for Improve, e.g. “more cinematic”', attrs: { id: 'music-ai-note' } });
  const status = h('div', { class: 'ai-status', role: 'status', 'aria-live': 'polite' });
  let undoSnapshot = null;
  const undo = button('Undo', { icon: 'undo', size: 'sm', attrs: { hidden: true, 'data-action': 'ai-undo' }, onClick: () => {
    if (!undoSnapshot) return;
    applySettings(undoSnapshot, { force: true });
    undoSnapshot = null;
    undo.hidden = true;
    replace(status, h('p', { class: 'small' }, 'Your previous settings are back.'));
  } });
  const run = async (kind) => {
    const current = readForm();
    const body = kind === 'build'
      ? { prompt: prompt.value.trim(), current, locked: [...locks], write_lyrics: writeLyrics.input.checked }
      : { current, locked: [...locks], improve_lyrics: improveLyrics.input.checked, instruction: note.value.trim() };
    if (kind === 'build' && body.prompt.length < 3) {
      replace(status, h('p', { class: 'small form-danger', role: 'alert' }, 'Describe the track you want first.'));
      prompt.focus();
      return;
    }
    buildBtn.disabled = true;
    improveBtn.disabled = true;
    replace(status, h('p', { class: 'loading small' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
      kind === 'build' ? 'gx-auto is designing the settings…' : 'gx-auto is refining your settings…'));
    try {
      const res = await api.post(`/api/music/ai/${kind}`, body, { timeout: 300_000 });
      undoSnapshot = current;
      applySettings(res.settings);
      undo.hidden = false;
      replace(status,
        h('p', { class: 'small' }, h('strong', {}, kind === 'build' ? 'Built with AI: ' : 'Improved: '), res.summary,
          ` · gx-auto, ${res.attempts} ${res.attempts === 1 ? 'attempt' : 'attempts'}, ${Number(res.elapsed_s || 0).toFixed(1)} s`),
        res.notes ? h('p', { class: 'small muted' }, res.notes) : null,
        (res.repair_notes || []).length ? h('p', { class: 'small muted' }, `Adjusted: ${res.repair_notes.join('; ')}`) : null,
        disclosure(`What changed (${res.changes.length})`, changeList(res.changes), { open: kind === 'improve', ic: 'list' }));
      toast(kind === 'build' ? 'Settings filled in. Everything stays editable.' : 'Settings improved. Undo is available.', 'ok', 3000);
    } catch (err) {
      replace(status, callout('danger', kind === 'build' ? 'Build with AI failed' : 'Improve failed', friendlyError(err.message).text));
    } finally {
      buildBtn.disabled = false;
      improveBtn.disabled = false;
    }
  };
  const buildBtn = button('Build with AI', { icon: 'wand', variant: 'primary', attrs: { id: 'music-ai-build' }, onClick: () => run('build') });
  const improveBtn = button('Improve My Prompt', { icon: 'sparkles', attrs: { id: 'music-ai-improve' }, onClick: () => run('improve') });
  prompt.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); run('build'); } });
  return h('section', { class: 'ai-panel', 'aria-labelledby': 'ai-h' },
    h('div', { class: 'row-between' }, h('h2', { class: 'ai-title', id: 'ai-h' }, icon('wand', { size: 18 }), ' Build with AI'),
      h('span', { class: 'small muted' }, 'gx-auto · runs on this cluster')),
    h('label', { class: 'field-label', for: 'music-ai-prompt' }, 'Describe the track in your own words'),
    prompt, writeLyrics,
    h('div', { class: 'row-wrap' }, buildBtn, improveBtn, undo),
    disclosure('Improve options', h('div', { class: 'stack-sm' }, field('Note for Improve', note), improveLyrics,
      h('p', { class: 'field-hint' }, 'Improve keeps locked fields, keeps your tags (and only suggests removals), fills empty fields, refines the description and style prompt, and never changes Instrumental, your vocal type or the seed.')), { ic: 'sliders' }),
    status);
}

// ------------------------------------------------------ reference analysis
export function referencePanel({ pickAsset, uploadFile, applySettings }) {
  let kind = 'upload';
  let asset = null;
  let session = null;
  let timer = null;
  const kindChips = chips([['upload', 'Upload'], ['library', 'Library'], ['youtube', 'YouTube'], ['spotify', 'Spotify']], { value: 'upload', label: 'Reference source' });
  const chosen = h('p', { class: 'small', 'aria-live': 'polite' }, 'No file chosen yet.');
  const drop = dropzone({ accept: 'audio/*,.wav,.flac,.mp3,.ogg,.m4a', label: 'Upload reference audio', hint: 'WAV, FLAC, MP3, OGG or M4A · up to 64 MB and 10 minutes',
    onFile: (f) => uploadFile(f, drop, (a) => setAsset(a)) });
  const libBtn = button('Choose from Library', { icon: 'library', size: 'sm', onClick: async () => { const a = await pickAsset(); if (a) setAsset(a); } });
  const url = textInput({ type: 'url', maxLength: 512, placeholder: 'https://www.youtube.com/watch?v=…', attrs: { id: 'music-ref-url', inputmode: 'url' } });
  const listen = toggle('Let ACE-Step listen as well (loads the music model on gx10-02)', true);
  const hint = textInput({ maxLength: 300, placeholder: 'Optional, e.g. “keep the groove, make it brighter”', attrs: { id: 'music-ref-hint' } });
  const result = h('div', { class: 'ref-result', 'aria-live': 'polite' });
  const uploadBox = h('div', { class: 'stack-sm' }, drop);
  const libBox = h('div', { class: 'stack-sm', hidden: true }, libBtn);
  const urlBox = h('div', { class: 'stack-sm', hidden: true }, field('Link', url),
    h('p', { class: 'field-hint' }, 'Only the public title and channel are read. Audio from YouTube or Spotify is never downloaded or analysed.'));
  function setAsset(a) {
    asset = a;
    chosen.textContent = a ? `Chosen: ${a.title || a.id}${a.duration ? ` (${mmss(a.duration)})` : ''}` : 'No file chosen yet.';
  }
  kindChips.addEventListener('click', () => {
    kind = kindChips.getValue();
    uploadBox.hidden = kind !== 'upload';
    libBox.hidden = kind !== 'library';
    urlBox.hidden = !['youtube', 'spotify'].includes(kind);
    listen.hidden = urlBox.hidden === false;
    chosen.hidden = !urlBox.hidden;
    url.placeholder = kind === 'spotify' ? 'https://open.spotify.com/track/…' : 'https://www.youtube.com/watch?v=…';
  });
  const analyzeBtn = button('Analyze Reference', { icon: 'wave', variant: 'secondary', attrs: { id: 'music-ref-analyze' }, onClick: () => start() });

  async function start() {
    let source;
    if (kind === 'youtube' || kind === 'spotify') {
      if (!url.value.trim()) { replace(result, h('p', { class: 'small form-danger', role: 'alert' }, 'Paste a link first.')); url.focus(); return; }
      source = { kind: 'url', url: url.value.trim() };
    } else {
      if (!asset) { replace(result, h('p', { class: 'small form-danger', role: 'alert' }, kind === 'upload' ? 'Upload an audio file first.' : 'Choose a track from the Library first.')); return; }
      source = { kind: 'asset', asset_id: asset.id };
    }
    analyzeBtn.disabled = true;
    replace(result, h('p', { class: 'loading small' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Starting the analysis…'));
    try {
      session = await api.post('/api/music/reference/analyze', { source, understand: source.kind === 'asset' && listen.input.checked, suggest: true, hint: hint.value.trim() });
      poll();
    } catch (err) {
      analyzeBtn.disabled = false;
      replace(result, callout('danger', 'Analysis could not start', friendlyError(err.message).text));
    }
  }

  async function poll() {
    clearTimeout(timer);
    if (!session || !el.isConnected) return;
    try {
      session = await api.get(`/api/music/reference/${session.id}`);
    } catch (err) {
      analyzeBtn.disabled = false;
      replace(result, callout('danger', 'Analysis status unavailable', friendlyError(err.message).text));
      return;
    }
    renderSession(session);
    if (session.state === 'done' || session.state === 'failed') { analyzeBtn.disabled = false; return; }
    timer = setTimeout(poll, 1500);
  }

  function renderSession(s) {
    const blocks = [];
    const title = s.source.title ? `${s.source.title}${s.source.author ? ` · ${s.source.author}` : ''}` : (s.source.url || 'Reference');
    blocks.push(h('p', { class: 'small' }, h('strong', {}, 'Source: '), title,
      ' ', badge(s.audio_analysed ? 'Audio analysed' : (s.source.kind === 'url' ? 'Metadata only — no audio analysed' : 'Waiting for audio analysis'), s.audio_analysed ? 'ok' : 'warn')));
    if (s.notice) blocks.push(callout('info', 'What was analysed', s.notice));
    if (s.state !== 'done' && s.state !== 'failed') {
      blocks.push(h('p', { class: 'loading small' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), s.detail || s.state));
    }
    if (s.measured) {
      const m = s.measured;
      const t = m.tempo || {};
      const k = m.key || {};
      const ts = m.time_signature || {};
      const conf = (v) => (typeof v === 'number' ? ` (confidence ${Math.round(v * 100)}%)` : '');
      blocks.push(h('div', { class: 'ref-block', 'data-block': 'measured' },
        h('p', { class: 'ref-block-title' }, badge(SOURCE_LABEL.measured, 'ok'), ' ', s.labels.measured),
        kv([
          ['Tempo', t.bpm ? `${t.bpm} BPM${conf(t.confidence)}${(t.candidates || []).length > 1 ? ` · also possible ${t.candidates.slice(1, 3).map((c) => c.bpm).join(' / ')}` : ''}` : '—'],
          ['Key', k.value ? `${k.value}${conf(k.confidence)}` : 'unclear'],
          ['Time signature', ts.value ? `${ts.value}${conf(ts.confidence)}` : 'unclear'],
          ['Duration', m.duration_s ? mmss(m.duration_s) : '—'],
          ['Loudness', m.loudness ? `${m.loudness.rms_dbfs} dBFS RMS · ${m.loudness.dynamic_range_db} dB range` : '—'],
          ['Energy', m.energy ? `${m.energy.level}, ${m.energy.trend}` : '—'],
          ['Tone', m.spectrum ? `${m.spectrum.brightness}, ${m.spectrum.bass_weight} bass` : '—'],
          ['Texture', m.texture ? m.texture.character : '—'],
          ['Stereo', m.stereo ? m.stereo.label : '—'],
          ['Structure', ((m.structure || {}).segments || []).map((g) => `${g.label} ${mmss(g.start)}–${mmss(g.end)} ${g.energy}`).join(' · ') || '—'],
        ]),
        h('p', { class: 'small muted' }, m.method || '')));
    }
    if (s.understanding) {
      const u = s.understanding;
      blocks.push(h('div', { class: 'ref-block', 'data-block': 'model' },
        h('p', { class: 'ref-block-title' }, badge(SOURCE_LABEL.model, 'info'), ' ', s.labels.model),
        kv([['Description', u.caption || '—'], ['Genres', u.genres || '—'],
          ['Vocals', u.vocals_detected ? `yes (${u.language || 'language unclear'})` : 'none heard']])));
    }
    if (s.suggestions) {
      const sug = s.suggestions.settings;
      const src = s.field_sources || {};
      const rows = ['description', 'style_tags', 'style_prompt', 'instrumental', 'vocal_intent', 'bpm', 'key', 'time_signature', 'duration']
        .filter((f) => sug[f] !== undefined)
        .map((f) => h('li', { class: 'ref-suggestion' }, h('span', { class: 'change-field' }, FIELD_LABEL[f]),
          badge(SOURCE_LABEL[src[f]] || 'Inferred', SOURCE_TONE[src[f]] || 'neutral'), h('span', { class: 'small' }, show(sug[f]))));
      const useBtn = button('Use Suggestions', { icon: 'check', variant: 'primary', size: 'sm', attrs: { id: 'music-ref-use' }, onClick: () => {
        applySettings(sug, { sources: src });
        toast('Suggestions copied into the form. Everything stays editable.', 'ok', 3000);
      } });
      blocks.push(h('div', { class: 'ref-block', 'data-block': 'suggestions' },
        h('p', { class: 'ref-block-title' }, 'Suggested settings for a new, original track'),
        h('ul', { class: 'change-list' }, rows),
        s.suggestions.notes ? h('p', { class: 'small muted' }, s.suggestions.notes) : null,
        h('div', { class: 'row-wrap' }, useBtn)));
    }
    if (s.state === 'failed') blocks.push(callout('danger', 'Analysis failed', friendlyError((s.error || {}).message || 'failed').text));
    replace(result, ...blocks);
  }

  const el = h('div', { class: 'stack-sm ref-panel', id: 'music-reference' },
    h('p', { class: 'field-hint' }, 'Measure a track you are allowed to use (tempo, key, energy, structure) and turn it into editable settings. This is inspiration for an original track, not cloning.'),
    kindChips, uploadBox, libBox, chosen, urlBox, listen, field('Hint for the suggestions', hint),
    h('div', { class: 'row-wrap' }, analyzeBtn), result);
  el.stop = () => clearTimeout(timer);
  el.setAsset = setAsset;
  return el;
}
