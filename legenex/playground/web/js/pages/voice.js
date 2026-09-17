// Voice Studio (gx-voice, Qwen3-TTS 12Hz 1.7B): voiceovers, designed voices,
// cloned voices (with recorded permission), dialogue, takes and a Voice Library.
// The backend picks the model per line: preset voice -> CustomVoice, a
// description -> VoiceDesign, a saved designed/cloned voice -> Base.
import { api, getAsset, upload } from '../api.js';
import { audioPlayer } from '../audio.js';
import { detailsDrawer, emitAsset, onAsset, pickAsset, sourceChip } from '../assets.js';
import { ago, clear, confirmDialog, h, mmss, openDialog, openDrawer, replace, storeGet, storeSet, toast, truncate, uid } from '../dom.js';
import { icon } from '../icons.js';
import { center, friendlyError, isVoice, jobCard, phaseOf, submitVoice } from '../jobs.js';
import { navigate } from '../nav.js';
import {
  badge, button, callout, chips, composer, disclosure, dropzone, emptyState, field, iconButton, kv, linkButton,
  numberInput, pageHeader, readNumber, seedField, select, skeletonLines, slider, tabs, textInput, toggle,
} from '../ui.js';

const CONSENT_TEXT = 'I confirm that I own this recording or have the speaker\'s permission to clone their voice, '
  + 'and that I will not use the clone to deceive or impersonate anyone.';
const LANG_LABEL = {
  auto: 'Auto-detect', english: 'English', chinese: 'Chinese', german: 'German', french: 'French', spanish: 'Spanish',
  italian: 'Italian', portuguese: 'Portuguese', russian: 'Russian', japanese: 'Japanese', korean: 'Korean',
};
const KIND_LABEL = { preset: 'Preset', designed: 'Designed', cloned: 'Cloned' };
const OP_WORD = { tts: 'Speech', voice_design: 'Voice design', voice_clone: 'Voice clone', dialogue: 'Dialogue' };
const DELIVERY = [
  ['', 'None'],
  ['narration', 'Narration', 'as a clear, engaging narrator'],
  ['ad', 'Advertising', 'as a persuasive, upbeat advertising voiceover'],
  ['conversational', 'Conversational', 'in a relaxed, conversational way'],
  ['character', 'Character', 'as an expressive character performance'],
  ['announcer', 'Announcer', 'as a bold, energetic announcer'],
];
const EMOTIONS = ['warm', 'cheerful', 'excited', 'calm', 'serious', 'confident', 'sad', 'angry', 'whispering', 'dramatic'];
const PACING = [['slow', 'Slow', 'at a slow, relaxed pace'], ['natural', 'Natural', ''], ['fast', 'Fast', 'at a quick, lively pace']];
const DESIGN_EXAMPLES = [
  'Warm, mature female narrator with a calm, trustworthy tone',
  'Energetic young male radio host, bright and punchy',
  'Gravelly old sea captain, slow and deliberate, a little hoarse',
  'Soft-spoken storyteller with a gentle, breathy voice',
];
const WPM = 150;

const session = { mode: storeGet('voice.mode', 'speak'), jobs: [], selectedVoice: storeGet('voice.voice', 'preset:aiden'), focus: null };

function modeOk(m) { return ['speak', 'design', 'clone', 'dialogue'].includes(m); }

function estimate(text) {
  const words = (text.match(/\S+/g) || []).length;
  return { words, seconds: Math.round((words / WPM) * 60) };
}

function safeVoiceId(id) {
  return /^(vc_[0-9a-f]{24}|preset:[a-z_]{2,16})$/.test(id || '') ? id : null;
}

function voiceLabel(v) {
  return `${v.name}${v.builtin ? '' : ` · ${KIND_LABEL[v.kind] || v.kind}`}`;
}

function numberField(label, { min, max, step = 'any', placeholder = 'Default', hint } = {}) {
  const input = numberInput({ min, max, step, placeholder });
  const el = field(label, input, { hint: [hint, `${min}–${max}`].filter(Boolean).join(' · ') });
  el.read = () => {
    const v = readNumber(input);
    if (v === null) return null;
    if (v < min || v > max) throw new Error(`${label} must be between ${min} and ${max}.`);
    return v;
  };
  el.input = input;
  return el;
}

function languageSelect(languages, value = 'auto') {
  return select(languages.map((l) => [l, LANG_LABEL[l] || l]), value);
}

function takesChips(value = '1') {
  return chips(['1', '2', '3', '4'].map((v) => [v, v]), { value, label: 'Takes' });
}

// ------------------------------------------------------------ page
export default {
  title: 'Voice',
  async mount(root, ctx) {
    replace(root, pageHeader('Voice', 'gx-voice · voiceovers, designed and cloned voices'), skeletonLines(6));
    let alive = true;
    let model = null;
    let modelError = null;
    let voices = [];
    try {
      model = await api.get('/api/voice/model');
    } catch (err) {
      modelError = err;
    }
    try {
      voices = (await api.get('/api/voice/voices')).voices || [];
    } catch (err) {
      replace(root, pageHeader('Voice', 'gx-voice'),
        callout('danger', 'The Voice Studio is not reachable', friendlyError(err.message).text, [
          button('Try again', { icon: 'refresh', onClick: () => navigate('voice') })]));
      return undefined;
    }
    const languages = (model && model.languages) || Object.keys(LANG_LABEL);
    const limits = (model && model.limits) || { takes: 4, text_chars: 10000, segments: 60 };
    if (!modeOk(session.mode)) session.mode = 'speak';
    if (modeOk(ctx.query.mode)) session.mode = ctx.query.mode;

    const voiceById = (id) => voices.find((v) => v.id === id) || null;

    async function reloadVoices() {
      try {
        voices = (await api.get('/api/voice/voices')).voices || [];
      } catch (err) {
        toast(friendlyError(err.message).text, 'danger');
        return;
      }
      for (const p of pickers) p.refresh();
      renderLibrary();
    }

    // ------------------------------------------------ voice picker
    const pickers = new Set();
    function voicePicker({ label = 'Voice', value, onChange, id } = {}) {
      const sel = h('select', { class: 'input select', id: id || uid('voice') });
      const info = h('p', { class: 'field-hint voice-hint', 'aria-live': 'polite', id: uid('voice-info') });
      sel.setAttribute('aria-describedby', info.id);
      const refresh = () => {
        const current = sel.value || value;
        clear(sel);
        const saved = voices.filter((v) => !v.builtin);
        const presets = voices.filter((v) => v.builtin);
        if (saved.length) sel.append(h('optgroup', { label: 'Your voices' }, saved.map((v) => h('option', { value: v.id }, voiceLabel(v)))));
        sel.append(h('optgroup', { label: 'Preset voices' }, presets.map((v) => h('option', { value: v.id }, `${v.name} (${v.native_language})`))));
        sel.value = voiceById(current) ? current : (presets[0] ? presets[0].id : '');
        describe();
      };
      const describe = () => {
        const v = voiceById(sel.value);
        clear(info);
        if (!v) return;
        info.append(badge(v.builtin ? 'Preset' : KIND_LABEL[v.kind], v.builtin ? 'neutral' : 'info'), ' ', v.description || '');
        if (v.kind !== 'preset') {
          info.append(h('span', { class: 'voice-note' }, ' Delivery follows this voice\'s reference clip; style instructions are not applied to it.'));
        }
      };
      sel.addEventListener('change', () => { describe(); if (onChange) onChange(voiceById(sel.value)); });
      const el = field(label, sel, { extra: null });
      el.append(info);
      el.select = sel;
      el.get = () => sel.value;
      el.set = (vid) => { if (voiceById(vid)) { sel.value = vid; describe(); if (onChange) onChange(voiceById(vid)); } };
      el.refresh = refresh;
      pickers.add(el);
      refresh();
      return el;
    }

    // ------------------------------------------------ stage: jobs + takes
    const jobsBox = h('div', { class: 'ws-jobs', id: 'voice-jobs', 'aria-live': 'polite' });
    const takesList = h('div', { class: 'take-list', id: 'voice-takes' });
    const moreTakes = button('Show older', { size: 'sm', variant: 'ghost', onClick: () => loadHistory(historyLimit + 12) });
    moreTakes.hidden = true;
    let history = [];
    let historyLimit = 12;
    const cards = new Map();

    function showJob(job) {
      if (cards.has(job.id)) return;
      const c = jobCard(job, { onRetry: (next) => trackJob(next) });
      cards.set(job.id, c);
      jobsBox.prepend(c);
      while (jobsBox.children.length > 4) { cards.delete(jobsBox.lastElementChild.dataset.job); jobsBox.lastElementChild.remove(); }
    }
    function trackJob(job) {
      if (!session.jobs.includes(job.id)) session.jobs.unshift(job.id);
      session.jobs = session.jobs.slice(0, 20);
      session.focus = job.id;
      showJob(job);
      upsert(job);
    }
    function upsert(job) {
      const i = history.findIndex((j) => j.id === job.id);
      if (i >= 0) history[i] = job; else history.unshift(job);
      renderTakes();
    }

    async function saveTake(job, take, btn) {
      if (btn) btn.disabled = true;
      try {
        const asset = await api.post(`/api/voice/jobs/${job.id}/takes/${take.index}/save`, {});
        emitAsset(asset);
        take.asset_id = asset.id;
        toast('Saved to the Library.', 'ok');
        renderTakes();
      } catch (err) {
        toast(friendlyError(err.message).text, 'danger');
        if (btn) btn.disabled = false;
      }
    }

    function saveDesignedVoice(job, take) {
      const name = textInput({ maxLength: 80, placeholder: 'e.g. Captain Hale', attrs: { required: true } });
      const desc = h('textarea', { class: 'input', rows: 3, maxlength: 1000 });
      desc.value = (job.request && job.request.description) || '';
      const err = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
      const ok = button('Save voice', { icon: 'check', variant: 'primary' });
      const cancel = button('Cancel', { variant: 'ghost' });
      const dlg = openDialog({
        title: `Save take ${take.index + 1} as a voice`,
        body: h('div', { class: 'stack' },
          h('p', { class: 'dialog-text' }, 'This take becomes the voice\'s reference clip. New lines in this voice are rendered from it, so the voice stays consistent.'),
          field('Voice name', name), field('Description', desc), err),
        actions: [cancel, ok],
      });
      cancel.addEventListener('click', () => dlg.close('cancel'));
      ok.addEventListener('click', async () => {
        err.hidden = true;
        if (!name.value.trim()) { err.hidden = false; err.textContent = 'Give the voice a name.'; name.focus(); return; }
        ok.disabled = true;
        try {
          const v = await api.post('/api/voice/voices', { kind: 'designed', name: name.value.trim(), description: desc.value.trim(), job_id: job.id, take: take.index });
          dlg.close('ok');
          toast(`Saved “${v.name}” to your voices.`, 'ok');
          session.selectedVoice = v.id;
          await reloadVoices();
          speak.voice.set(v.id);
        } catch (e) {
          err.hidden = false;
          err.textContent = friendlyError(e.message).text;
          ok.disabled = false;
        }
      });
      name.focus();
    }

    function takeCard(job, take) {
      const title = `${job.title || OP_WORD[job.operation]} · take ${take.index + 1}`;
      // Lossless playback: a 24 kHz mono take is about 2.9 MB per minute.
      const player = audioPlayer({ url: take.audio_url, duration: take.duration_s, waveform: take.waveform, title }, { label: title });
      const actions = [
        ...take.formats.map((f) => linkButton(f.toUpperCase(), `${take.audio_url}?format=${f}&download=1`, {
          icon: 'download', size: 'sm', download: '', attrs: { 'aria-label': `Download take ${take.index + 1} as ${f.toUpperCase()}`, 'data-format': f },
        })),
      ];
      if (take.asset_id) {
        actions.push(button('In Library', { icon: 'library', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'library' }, onClick: () => detailsDrawer(take.asset_id) }));
      } else {
        const b = button('Save to Library', { icon: 'check', size: 'sm', variant: 'secondary', attrs: { 'data-action': 'save' } });
        b.addEventListener('click', () => saveTake(job, take, b));
        actions.push(b);
      }
      if (job.operation === 'voice_design') {
        actions.push(button('Save as voice', { icon: 'mic', size: 'sm', variant: 'primary', attrs: { 'data-action': 'save-voice' }, onClick: () => saveDesignedVoice(job, take) }));
      }
      return h('div', { class: 'take', dataset: { take: String(take.index) } },
        h('div', { class: 'take-head' },
          h('span', { class: 'take-num' }, `Take ${take.index + 1}`),
          h('span', { class: 'muted small' }, [mmss(take.duration_s), take.seed !== null && take.seed !== undefined ? `seed ${take.seed}` : null].filter(Boolean).join(' · '))),
        player,
        h('div', { class: 'track-actions' }, actions));
    }

    function takeSet(job) {
      const p = phaseOf(job);
      const req = job.request || {};
      const voice = job.voice_id ? voiceById(job.voice_id) : null;
      const script = req.text || (req.segments || []).map((s) => `${(voiceById(s.voice_id) || {}).name || 'Voice'}: ${s.text}`).join('\n');
      const meta = [OP_WORD[job.operation], voice ? voice.name : null, req.description ? truncate(req.description, 60) : null, ago(job.created_at)].filter(Boolean);
      const t = job.timings || {};
      const body = [];
      if (p.key === 'COMPLETE') {
        for (const take of job.takes || []) body.push(takeCard(job, take));
      } else if (p.terminal) {
        body.push(h('p', { class: 'muted small' }, p.key === 'FAILED' ? friendlyError((job.error || {}).message).text : 'Cancelled.'));
      } else {
        body.push(h('p', { class: 'muted small' }, job.detail || 'Working…'));
      }
      const notes = (job.notes || []).map((n) => h('p', { class: 'hint' }, icon('info', { size: 14 }), n));
      const reuse = job.operation !== 'voice_clone' ? button('Use settings', { icon: 'sliders', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'reuse' }, onClick: () => reuseJob(job) }) : null;
      const del = p.terminal ? iconButton('trash', 'Delete this generation (Library copies stay)', async () => {
        if (!(await confirmDialog({ title: 'Delete this generation?', message: 'Its takes are removed from the history. Anything you saved to the Library stays.', okLabel: 'Delete', danger: true }))) return;
        try {
          await api.post(`/api/voice/jobs/${job.id}/delete`, { confirm: true });
          history = history.filter((j) => j.id !== job.id);
          renderTakes();
        } catch (e) { toast(friendlyError(e.message).text, 'danger'); }
      }, { attrs: { class: 'icon-btn icon-btn-ghost danger', 'data-action': 'delete' } }) : null;
      return h('article', { class: `take-set${session.focus === job.id ? ' is-focus' : ''}`, dataset: { job: job.id }, 'aria-label': `${OP_WORD[job.operation]}: ${job.title || truncate(script, 40)}` },
        h('header', { class: 'track-head' },
          h('div', { class: 'track-art', 'aria-hidden': 'true' }, icon(job.operation === 'dialogue' ? 'layers' : 'mic', { size: 20 })),
          h('div', { class: 'track-titles' },
            h('h3', { class: 'track-title' }, job.title || truncate(script, 60) || OP_WORD[job.operation]),
            h('p', { class: 'muted small' }, meta.join(' · '))),
          h('div', { class: 'row-tight' }, reuse, del)),
        script ? h('p', { class: 'take-script' }, truncate(script, 280)) : null,
        ...notes,
        ...body,
        p.key === 'COMPLETE' && (t.generate_s || t.rtf) ? h('p', { class: 'muted small' },
          [t.generate_s ? `rendered in ${Number(t.generate_s).toFixed(1)} s` : null,
            t.first_audio_s ? `first audio after ${Number(t.first_audio_s).toFixed(1)} s` : null,
            t.rtf ? `${Number(t.rtf).toFixed(2)}× real time` : null].filter(Boolean).join(' · ')) : null);
    }

    function renderTakes() {
      clear(takesList);
      const list = history.filter((j) => j.status !== 'deleted');
      if (!list.length) {
        takesList.append(emptyState({ icon: 'mic', title: 'No voice takes yet', text: 'Write a script, pick a voice and press Generate. Your takes appear here with a player, downloads and Save to Library.' }));
        return;
      }
      for (const j of list) takesList.append(takeSet(j));
      const focus = session.focus && takesList.querySelector(`[data-job="${session.focus}"]`);
      if (focus && ctx.query.job) focus.scrollIntoView({ block: 'nearest' });
    }

    async function loadHistory(limit = historyLimit) {
      historyLimit = limit;
      try {
        const res = await api.get(`/api/voice/jobs?limit=${limit}`);
        if (!alive) return;
        const fresh = res.jobs || [];
        const known = new Map(fresh.map((j) => [j.id, j]));
        for (const j of history) if (!known.has(j.id) && !phaseOf(j).terminal) known.set(j.id, j);
        history = [...known.values()].sort((a, b) => b.created_at - a.created_at);
        moreTakes.hidden = fresh.length < limit;
        renderTakes();
      } catch (err) {
        replace(takesList, callout('warn', 'Voice history could not be loaded', friendlyError(err.message).text));
      }
    }

    const unsubJobs = center.subscribe((job) => {
      if (!alive || !job || !isVoice(job)) return;
      if (!session.jobs.includes(job.id) && !phaseOf(job).terminal) trackJob(job);
      else upsert(job);
    });

    // ------------------------------------------------ Voice Library
    const libraryGrid = h('div', { class: 'voice-grid', id: 'voice-library', role: 'list' });
    const presetGrid = h('div', { class: 'voice-grid', role: 'list' });

    function editVoice(v) {
      const name = textInput({ value: v.name, maxLength: 80 });
      const desc = h('textarea', { class: 'input', rows: 3, maxlength: 1000 });
      desc.value = v.description || '';
      const instr = textInput({ value: v.instructions || '', maxLength: 500, placeholder: 'e.g. warm, confident, medium pace' });
      const lang = languageSelect(languages, v.language || 'auto');
      const transcript = h('textarea', { class: 'input', rows: 3, maxlength: 2000 });
      transcript.value = v.reference_text || '';
      const err = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
      const ok = button('Save changes', { variant: 'primary' });
      const cancel = button('Cancel', { variant: 'ghost' });
      const dlg = openDialog({
        title: `Edit ${v.name}`,
        body: h('div', { class: 'stack' }, field('Name', name), field('Description', desc),
          v.kind === 'preset' ? field('Default style instructions', instr, { hint: 'Used whenever this voice speaks, before any per-script instructions.' }) : null,
          field('Default language', lang),
          v.kind === 'preset' ? null : field('Reference transcript', transcript, { hint: 'The exact words in the reference clip. With a transcript the clone is closer to the original.' }),
          err),
        actions: [cancel, ok],
      });
      cancel.addEventListener('click', () => dlg.close('cancel'));
      ok.addEventListener('click', async () => {
        ok.disabled = true;
        const body = { name: name.value.trim(), description: desc.value.trim(), language: lang.value };
        if (v.kind === 'preset') body.instructions = instr.value.trim();
        else body.transcript = transcript.value.trim();
        try {
          await api.post(`/api/voice/voices/${v.id}`, body);
          dlg.close('ok');
          toast('Voice updated (a new version was recorded).', 'ok');
          reloadVoices();
        } catch (e) {
          err.hidden = false;
          err.textContent = friendlyError(e.message).text;
          ok.disabled = false;
        }
      });
    }

    async function showVersions(v) {
      const body = h('div', { class: 'stack' }, skeletonLines(3));
      openDrawer({ title: `Versions · ${v.name}`, body });
      try {
        const res = await api.get(`/api/voice/voices/${v.id}/versions`);
        replace(body, ...res.versions.map((x) => h('section', { class: 'card version' },
          h('p', { class: 'card-title' }, `Version ${x.version}`),
          h('p', { class: 'muted small' }, `${ago(x.created_at)} by ${x.changed_by}`),
          kv([['Name', x.snapshot.name], ['Description', x.snapshot.description], ['Instructions', x.snapshot.instructions],
            ['Language', LANG_LABEL[x.snapshot.language] || x.snapshot.language], ['Transcript', x.snapshot.reference_text]]))));
      } catch (e) {
        replace(body, callout('danger', 'Could not load the versions', friendlyError(e.message).text));
      }
    }

    function copyPreset(v) {
      const name = textInput({ maxLength: 80, value: `${v.name} (mine)` });
      const instr = textInput({ maxLength: 500, placeholder: 'e.g. excited, fast, radio-style' });
      const lang = languageSelect(languages, 'auto');
      const err = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
      const ok = button('Save voice', { variant: 'primary' });
      const cancel = button('Cancel', { variant: 'ghost' });
      const dlg = openDialog({
        title: `Save ${v.name} with your defaults`,
        body: h('div', { class: 'stack' }, field('Name', name), field('Default style instructions', instr), field('Default language', lang), err),
        actions: [cancel, ok],
      });
      cancel.addEventListener('click', () => dlg.close('cancel'));
      ok.addEventListener('click', async () => {
        ok.disabled = true;
        try {
          const saved = await api.post('/api/voice/voices', { kind: 'preset', speaker: v.speaker, name: name.value.trim(), instructions: instr.value.trim(), language: lang.value });
          dlg.close('ok');
          toast(`Saved “${saved.name}”.`, 'ok');
          await reloadVoices();
        } catch (e) {
          err.hidden = false;
          err.textContent = friendlyError(e.message).text;
          ok.disabled = false;
        }
      });
    }

    function useVoice(v) {
      setMode('speak');
      speak.voice.set(v.id);
      speak.script.textarea.focus({ preventScroll: true });
      panel.scrollIntoView({ block: 'start', behavior: 'smooth' });
      toast(`${v.name} selected.`, 'ok', 2000);
    }

    function voiceCard(v) {
      const preview = v.reference_url ? audioPlayer({ url: v.reference_url, duration: 0, title: v.name }, { label: `Reference clip of ${v.name}` }) : null;
      const actions = [button('Use', { icon: 'mic', size: 'sm', variant: 'primary', attrs: { 'data-action': 'use' }, onClick: () => useVoice(v) })];
      if (v.builtin) {
        actions.push(button('Save with my defaults', { icon: 'plus', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'copy' }, onClick: () => copyPreset(v) }));
      } else {
        actions.push(
          iconButton('edit', `Edit ${v.name}`, () => editVoice(v), { attrs: { 'data-action': 'edit' } }),
          iconButton('history', `Versions of ${v.name}`, () => showVersions(v), { attrs: { 'data-action': 'versions' } }),
          iconButton('trash', `Delete ${v.name}`, async () => {
            if (!(await confirmDialog({ title: `Delete ${v.name}?`, message: 'The voice disappears from your library and from the API. Its reference clip stays in the Library, and past takes stay.', okLabel: 'Delete voice', danger: true }))) return;
            try {
              await api.post(`/api/voice/voices/${v.id}/delete`, { confirm: true });
              toast('Voice deleted.', 'ok');
              reloadVoices();
            } catch (e) { toast(friendlyError(e.message).text, 'danger'); }
          }, { attrs: { class: 'icon-btn icon-btn-ghost danger', 'data-action': 'delete' } }),
        );
      }
      return h('article', { class: 'voice-card', role: 'listitem', dataset: { voice: v.id }, 'aria-label': `Voice ${v.name}` },
        h('div', { class: 'voice-card-head' },
          h('span', { class: `voice-avatar voice-avatar-${v.kind}`, 'aria-hidden': 'true' }, (v.name || '?').slice(0, 1).toUpperCase()),
          h('div', { class: 'voice-card-titles' },
            h('h3', { class: 'voice-name' }, v.name),
            h('p', { class: 'muted small' }, [v.builtin ? 'Preset' : KIND_LABEL[v.kind], v.builtin ? v.native_language : (LANG_LABEL[v.language] || v.language),
              v.version > 1 ? `v${v.version}` : null].filter(Boolean).join(' · ')))),
        v.description ? h('p', { class: 'voice-desc' }, truncate(v.description, 160)) : null,
        v.instructions ? h('p', { class: 'hint' }, icon('sliders', { size: 14 }), truncate(v.instructions, 80)) : null,
        preview,
        h('div', { class: 'voice-actions' }, actions));
    }

    function renderLibrary() {
      clear(libraryGrid);
      clear(presetGrid);
      const saved = voices.filter((v) => !v.builtin);
      if (!saved.length) {
        libraryGrid.append(h('div', { role: 'listitem', class: 'voice-empty' }, emptyState({ icon: 'mic', title: 'No saved voices yet', text: 'Design a voice from a description, clone one from a recording you are allowed to use, or save a preset with your own defaults.' })));
      }
      for (const v of saved) libraryGrid.append(voiceCard(v));
      for (const v of voices.filter((x) => x.builtin)) presetGrid.append(voiceCard(v));
    }

    // ------------------------------------------------ forms
    const errors = {};
    const formError = (key) => { errors[key] = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true }); return errors[key]; };
    const showError = (key, msg) => { errors[key].hidden = false; errors[key].textContent = msg; errors[key].scrollIntoView({ block: 'nearest' }); };

    function advancedSampling() {
      const f = {
        temperature: numberField('Temperature', { min: 0.1, max: 2, step: 0.05, hint: 'Higher is more varied' }),
        top_p: numberField('Top-p', { min: 0.05, max: 1, step: 0.05 }),
        top_k: numberField('Top-k', { min: 1, max: 200, step: 1 }),
        repetition_penalty: numberField('Repetition penalty', { min: 1, max: 2, step: 0.01 }),
      };
      const el = disclosure('Advanced sampling', h('div', { class: 'grid-2' }, Object.values(f)), { ic: 'sliders' });
      el.read = () => {
        const out = {};
        for (const [k, fl] of Object.entries(f)) { const v = fl.read(); if (v !== null) out[k] = v; }
        return out;
      };
      return el;
    }

    // Speak
    const speak = {};
    speak.voice = voicePicker({ value: session.selectedVoice, id: 'voice-select', onChange: (v) => {
      if (!v) return;
      session.selectedVoice = v.id;
      storeSet('voice.voice', v.id);
      const st = v.style || {};
      if (st.speed) speak.speed.setValue(st.speed);
      if (st.pause_ms !== undefined) speak.pause.setValue(st.pause_ms);
      if (v.language && v.language !== 'auto') speak.language.value = v.language;
      syncInstructionState();
    } });
    speak.script = composer({ label: 'Script', placeholder: 'Write what the voice should say. Blank lines start a new paragraph (with a pause).', maxLength: limits.text_chars || 10000, rows: 9, onSubmit: () => submit('speak'), id: 'voice-script' });
    speak.stats = h('p', { class: 'field-hint', 'aria-live': 'polite' });
    const syncStats = () => { const e = estimate(speak.script.get()); speak.stats.textContent = `${e.words} words · about ${mmss(e.seconds)} at a natural pace`; };
    speak.script.textarea.addEventListener('input', syncStats);
    speak.delivery = chips(DELIVERY.map(([k, label]) => [k, label]), { value: '', label: 'Delivery' });
    speak.emotions = chips(EMOTIONS.map((e) => [e, e]), { multiple: true, value: [], label: 'Emotion', cls: 'chips-sm' });
    speak.instructions = textInput({ maxLength: 300, placeholder: 'e.g. smile while speaking, stress the product name', attrs: { id: 'voice-instructions' } });
    speak.pacing = chips(PACING.map(([k, label]) => [k, label]), { value: 'natural', label: 'Pacing' });
    speak.speed = slider({ label: 'Tempo (time stretch)', min: 0.5, max: 2, step: 0.05, value: 1, format: (v) => `${v.toFixed(2)}×`, hint: 'Speeds up or slows down the finished audio without changing the pitch.' });
    speak.pause = slider({ label: 'Pause between paragraphs', min: 0, max: 2000, step: 50, value: 350, format: (v) => `${v} ms` });
    speak.language = languageSelect(languages, 'auto');
    speak.takes = takesChips('1');
    speak.seed = seedField('voice');
    speak.title = textInput({ maxLength: 200, placeholder: 'Optional' });
    speak.autoSave = toggle('Save every take to the Library', false);
    speak.advanced = advancedSampling();
    speak.styleBox = h('div', { class: 'stack-sm' },
      h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Delivery'), speak.delivery),
      h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Emotion'), speak.emotions),
      field('Style instructions', speak.instructions, { hint: 'Free text for tone, emotion and emphasis.' }),
      h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Pacing'), speak.pacing));
    speak.styleNote = callout('info', 'Style follows the voice', 'Designed and cloned voices speak the way their reference clip sounds. Tempo and pauses still apply.');
    function syncInstructionState() {
      const v = voiceById(speak.voice.get());
      const preset = !v || v.kind === 'preset';
      speak.styleBox.hidden = !preset;
      speak.styleNote.hidden = preset;
    }
    const buildInstructions = () => {
      const parts = [];
      const d = DELIVERY.find(([k]) => k === speak.delivery.getValue());
      if (d && d[2]) parts.push(d[2]);
      const em = speak.emotions.getValue();
      if (em.length) parts.push(`${em.join(', ')} tone`);
      if (speak.instructions.value.trim()) parts.push(speak.instructions.value.trim());
      const pace = PACING.find(([k]) => k === speak.pacing.getValue());
      if (pace && pace[2]) parts.push(pace[2]);
      return parts.join('; ');
    };
    const speakForm = h('div', { class: 'stack', id: 'form-speak' },
      speak.voice, speak.script, speak.stats, speak.styleBox, speak.styleNote,
      h('div', { class: 'grid-2' }, field('Language', speak.language), h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Takes'), speak.takes)),
      speak.speed, speak.pause, speak.seed, field('Title', speak.title), speak.autoSave, speak.advanced, formError('speak'));

    // Design
    const design = {};
    design.description = composer({ label: 'Describe the voice', placeholder: 'Age, gender, accent, timbre, energy… e.g. a warm, mature female narrator with a calm, trustworthy tone', maxLength: 1000, rows: 4, onSubmit: () => submit('design'), id: 'voice-description' });
    design.examples = h('div', { class: 'chips chips-sm', role: 'group', 'aria-label': 'Example descriptions' },
      DESIGN_EXAMPLES.map((t) => h('button', { type: 'button', class: 'chip chip-sm', onclick: () => { design.description.set(t); design.description.textarea.focus(); } }, truncate(t, 38))));
    design.text = composer({ label: 'Preview line', maxLength: 1000, rows: 3, value: 'Hello! This is how I sound. I can read your scripts, your stories and your ads.', onSubmit: () => submit('design'), id: 'voice-design-text' });
    design.language = languageSelect(languages, 'auto');
    design.takes = takesChips('2');
    design.seed = seedField('voice-design');
    const designForm = h('div', { class: 'stack', id: 'form-design' },
      design.description, design.examples, design.text,
      callout('info', 'Keep the one you like', 'Each take is a different voice for the same description. Press “Save as voice” on your favourite: it becomes a reusable voice that sounds the same every time.'),
      h('div', { class: 'grid-2' }, field('Language', design.language), h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Takes'), design.takes)),
      design.seed, formError('design'));

    // Clone
    const clone = { ref: null };
    clone.refBox = h('div', { class: 'stack-sm source-box', id: 'clone-ref' });
    const setRef = (asset) => {
      clone.ref = asset;
      clear(clone.refBox);
      if (!asset) return;
      clone.refBox.append(sourceChip(asset, { label: 'Reference clip', onClear: () => setRef(null) }),
        audioPlayer(asset, { label: `Reference clip ${asset.title || ''}` }));
    };
    clone.drop = dropzone({
      accept: 'audio/*,.wav,.flac,.mp3,.ogg,.m4a,.webm', label: 'Upload a reference clip', hint: '2–60 s of clear speech from one speaker · WAV, FLAC, MP3, OGG, M4A · up to 32 MB',
      onFile: async (file) => {
        const types = { wav: 'audio/wav', flac: 'audio/flac', mp3: 'audio/mpeg', ogg: 'audio/ogg', m4a: 'audio/mp4', webm: 'audio/webm' };
        const ext = (file.name.split('.').pop() || '').toLowerCase();
        const type = file.type && file.type.startsWith('audio/') ? file.type : types[ext];
        if (!type) { toast('Choose an audio file.', 'danger'); return; }
        if (file.size > 32 * 1024 * 1024) { toast('That file is larger than 32 MB.', 'danger'); return; }
        clone.drop.progress(0);
        try {
          const asset = await upload('/api/voice/upload', file.type === type ? file : new Blob([file], { type }), {
            title: file.name.replace(/\.[^.]+$/, ''), filename: file.name, onProgress: (f) => clone.drop.progress(f),
          });
          clone.drop.progress(null);
          emitAsset(asset);
          setRef(asset);
          toast('Reference clip checked and saved to the Library.', 'ok');
        } catch (err) {
          clone.drop.progress(null);
          showError('clone', friendlyError(err.message).text);
        }
      },
    });
    clone.pick = button('Choose from Library', { icon: 'library', size: 'sm', onClick: async () => {
      const a = await pickAsset({ type: 'audio', title: 'Choose a reference clip' });
      if (a) setRef(a);
    } });
    clone.transcript = h('textarea', { class: 'input', rows: 3, maxlength: 2000, id: 'clone-transcript', placeholder: 'Type exactly what is said in the clip (recommended)' });
    clone.consent = h('input', { type: 'checkbox', class: 'check', id: 'clone-consent' });
    clone.consentBox = h('div', { class: 'consent' },
      clone.consent,
      h('label', { for: 'clone-consent', class: 'consent-label' }, h('strong', {}, 'Permission to clone. '), CONSENT_TEXT),
      h('p', { class: 'field-hint consent-hint' }, 'Your confirmation, the time and the fingerprint of this exact recording are stored with the voice.'));
    clone.text = composer({ label: 'Test line', maxLength: 2000, rows: 3, value: 'This is a test of my cloned voice.', onSubmit: () => submit('clone'), id: 'clone-text' });
    clone.name = textInput({ maxLength: 80, placeholder: 'e.g. My narration voice', attrs: { id: 'clone-name' } });
    clone.language = languageSelect(languages, 'auto');
    clone.takes = takesChips('1');
    clone.seed = seedField('voice-clone');
    clone.saveBtn = button('Save as voice', { icon: 'mic', variant: 'secondary', attrs: { id: 'clone-save' }, onClick: () => saveClone() });
    const cloneForm = h('div', { class: 'stack', id: 'form-clone' },
      h('div', { class: 'panel-section' }, h('p', { class: 'field-label' }, 'Reference clip'), clone.refBox, h('div', { class: 'row-wrap' }, clone.pick), clone.drop),
      field('Transcript of the clip', clone.transcript, { hint: 'With the exact words the clone is closer to the speaker. Without them only the voice colour is used.' }),
      clone.consentBox, clone.text,
      h('div', { class: 'grid-2' }, field('Language', clone.language), h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Takes'), clone.takes)),
      clone.seed,
      h('div', { class: 'card clone-save' }, field('Voice name', clone.name), clone.saveBtn),
      formError('clone'));

    async function saveClone() {
      errors.clone.hidden = true;
      if (!clone.ref) { showError('clone', 'Choose or upload the reference clip first.'); return; }
      if (!clone.consent.checked) { showError('clone', 'Confirm that you have permission to clone this voice.'); clone.consent.focus(); return; }
      if (!clone.name.value.trim()) { showError('clone', 'Give the voice a name.'); clone.name.focus(); return; }
      clone.saveBtn.disabled = true;
      try {
        const v = await api.post('/api/voice/voices', {
          kind: 'cloned', name: clone.name.value.trim(), reference_asset_id: clone.ref.id,
          transcript: clone.transcript.value.trim(), language: clone.language.value,
          consent: { confirmed: true, statement: CONSENT_TEXT },
        });
        toast(`Saved “${v.name}” to your voices.`, 'ok');
        clone.name.value = '';
        await reloadVoices();
        useVoice(v);
      } catch (err) {
        showError('clone', friendlyError(err.message).text);
      } finally {
        clone.saveBtn.disabled = false;
      }
    }

    // Dialogue
    const dlgState = { lines: storeGet('voice.dialogue', null) };
    const linesBox = h('ol', { class: 'dialogue-lines', id: 'dialogue-lines' });
    const lineRows = [];
    function lineRow(line = {}) {
      const row = h('li', { class: 'dialogue-line' });
      const picker = voicePicker({ label: 'Speaker', value: safeVoiceId(line.voice_id) || session.selectedVoice });
      const text = h('textarea', { class: 'input', rows: 2, maxlength: 10000, placeholder: 'What this speaker says' });
      text.value = line.text || '';
      const tid = uid('line');
      text.id = tid;
      const instr = textInput({ maxLength: 300, placeholder: 'Style (preset voices), e.g. surprised', value: line.instructions || '' });
      const pause = numberInput({ min: 0, max: 5000, step: 50, placeholder: 'Default', value: line.pause_ms });
      const up = iconButton('chevronDown', 'Move line up', () => move(row, -1), { attrs: { class: 'icon-btn icon-btn-ghost icon-flip' } });
      const down = iconButton('chevronDown', 'Move line down', () => move(row, 1));
      const del = iconButton('x', 'Remove line', () => remove(row), { attrs: { class: 'icon-btn icon-btn-ghost danger' } });
      row.append(
        h('div', { class: 'row-between' }, h('span', { class: 'take-num line-num' }), h('div', { class: 'row-tight' }, up, down, del)),
        picker, h('div', { class: 'field' }, h('label', { class: 'field-label', for: tid }, 'Line'), text),
        h('div', { class: 'grid-2' }, field('Style', instr), field('Pause before (ms)', pause)));
      row.read = () => {
        const out = { voice_id: picker.get(), text: text.value.trim() };
        if (instr.value.trim()) out.instructions = instr.value.trim();
        const p = readNumber(pause, { integer: true });
        if (p !== null) out.pause_ms = p;
        return out;
      };
      row.picker = picker;
      return row;
    }
    function renumber() {
      lineRows.forEach((r, i) => {
        r.querySelector('.line-num').textContent = `Line ${i + 1}`;
        r.setAttribute('aria-label', `Line ${i + 1}`);
      });
      storeSet('voice.dialogue', lineRows.map((r) => r.read()));
    }
    function addLine(line) {
      if (lineRows.length >= (limits.segments || 60)) { toast('That is the maximum number of lines.', 'warn'); return; }
      const r = lineRow(line);
      lineRows.push(r);
      linesBox.append(r);
      r.addEventListener('input', () => renumber());
      renumber();
    }
    function move(row, delta) {
      const i = lineRows.indexOf(row);
      const j = i + delta;
      if (j < 0 || j >= lineRows.length) return;
      lineRows.splice(i, 1);
      lineRows.splice(j, 0, row);
      clear(linesBox);
      lineRows.forEach((r) => linesBox.append(r));
      renumber();
      row.querySelector('textarea').focus();
    }
    function remove(row) {
      const i = lineRows.indexOf(row);
      if (i < 0 || lineRows.length <= 1) return;
      lineRows.splice(i, 1);
      row.remove();
      pickers.delete(row.picker);
      renumber();
      (lineRows[Math.max(0, i - 1)].querySelector('textarea')).focus();
    }
    const initialLines = Array.isArray(dlgState.lines) && dlgState.lines.length ? dlgState.lines
      : [{ voice_id: 'preset:aiden', text: 'Welcome back to the show! Today we have a very special guest.' },
        { voice_id: 'preset:serena', text: 'Thanks for having me, it is great to be here.' }];
    initialLines.forEach((l) => addLine(l));
    const dialogue = {
      pause: slider({ label: 'Default pause between lines', min: 0, max: 2000, step: 50, value: 400, format: (v) => `${v} ms` }),
      language: languageSelect(languages, 'auto'),
      takes: takesChips('1'),
      seed: seedField('voice-dialogue'),
      title: textInput({ maxLength: 200, placeholder: 'Optional' }),
      autoSave: toggle('Save every take to the Library', false),
    };
    const dialogueForm = h('div', { class: 'stack', id: 'form-dialogue' },
      h('p', { class: 'muted small' }, 'Each line is rendered in its own voice and joined in order with the pause you set.'),
      linesBox,
      button('Add line', { icon: 'plus', size: 'sm', attrs: { id: 'dialogue-add' }, onClick: () => { addLine({ voice_id: session.selectedVoice }); lineRows[lineRows.length - 1].querySelector('textarea').focus(); } }),
      dialogue.pause,
      h('div', { class: 'grid-2' }, field('Language', dialogue.language), h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Takes'), dialogue.takes)),
      dialogue.seed, field('Title', dialogue.title), dialogue.autoSave, formError('dialogue'));

    // ------------------------------------------------ submit
    const forms = { speak: speakForm, design: designForm, clone: cloneForm, dialogue: dialogueForm };
    const LABEL = { speak: 'Generate', design: 'Design voice', clone: 'Test clone', dialogue: 'Generate dialogue' };
    const submitBtn = button('Generate', { icon: 'sparkles', variant: 'primary', attrs: { id: 'voice-submit', class: 'btn btn-primary btn-lg btn-block' }, onClick: () => submit(session.mode) });
    const modeTabs = tabs([['speak', 'Speak', 'mic'], ['design', 'Design', 'wand'], ['clone', 'Clone', 'copy'], ['dialogue', 'Dialogue', 'layers']], session.mode, (m) => setMode(m), { label: 'Voice mode' });
    const panel = h('aside', { class: 'panel panel-voice', 'aria-label': 'Voice settings' }, modeTabs, h('div', { class: 'panel-body' }, Object.values(forms)), h('div', { class: 'panel-foot' }, submitBtn));

    function setMode(m) {
      session.mode = m;
      storeSet('voice.mode', m);
      modeTabs.select(m);
      for (const [k, f] of Object.entries(forms)) f.hidden = k !== m;
      submitBtn.querySelector('span').textContent = LABEL[m];
    }

    function readTakesSeed(f) {
      return { takes: Number(f.takes.getValue()), seed: f.seed.next() };
    }

    function build(mode) {
      if (mode === 'speak') {
        const text = speak.script.get();
        if (!text) throw new Error('Write the script first.');
        const body = { operation: 'tts', voice_id: speak.voice.get(), text, language: speak.language.value,
          speed: speak.speed.getValue(), pause_ms: speak.pause.getValue(), auto_save: speak.autoSave.input.checked,
          ...readTakesSeed(speak), ...speak.advanced.read() };
        const v = voiceById(body.voice_id);
        if (!v) throw new Error('Choose a voice.');
        const instr = v.kind === 'preset' ? buildInstructions() : '';
        if (instr) body.instructions = instr.slice(0, 500);
        if (speak.title.value.trim()) body.title = speak.title.value.trim();
        return body;
      }
      if (mode === 'design') {
        const description = design.description.get();
        if (!description) throw new Error('Describe the voice you want.');
        const text = design.text.get();
        if (!text) throw new Error('Write a preview line.');
        return { operation: 'voice_design', description, text, language: design.language.value, ...readTakesSeed(design) };
      }
      if (mode === 'clone') {
        if (!clone.ref) throw new Error('Choose or upload the reference clip first.');
        if (!clone.consent.checked) throw new Error('Confirm that you have permission to clone this voice.');
        const text = clone.text.get();
        if (!text) throw new Error('Write a test line.');
        return { operation: 'voice_clone', text, language: clone.language.value, ...readTakesSeed(clone),
          reference: { asset_id: clone.ref.id, transcript: clone.transcript.value.trim(), consent: { confirmed: true, statement: CONSENT_TEXT } } };
      }
      const segments = lineRows.map((r) => r.read());
      const empty = segments.findIndex((s) => !s.text);
      if (empty >= 0) throw new Error(`Line ${empty + 1} is empty.`);
      const body = { operation: 'dialogue', segments, pause_ms: dialogue.pause.getValue(), language: dialogue.language.value,
        auto_save: dialogue.autoSave.input.checked, ...readTakesSeed(dialogue) };
      if (dialogue.title.value.trim()) body.title = dialogue.title.value.trim();
      return body;
    }

    async function submit(mode) {
      const err = errors[mode];
      if (err) err.hidden = true;
      let body;
      try {
        body = build(mode);
      } catch (e) {
        showError(mode, e.message);
        return;
      }
      submitBtn.disabled = true;
      try {
        const job = await submitVoice(body);
        trackJob(job);
        toast('Queued on gx-voice.', 'ok', 2000);
      } catch (e) {
        showError(mode, friendlyError(e.message).text);
      } finally {
        submitBtn.disabled = false;
      }
    }

    function reuseJob(job) {
      const r = job.request || {};
      if (job.operation === 'tts') {
        setMode('speak');
        if (r.voice_id) speak.voice.set(r.voice_id);
        speak.script.set(r.text || '');
        speak.script.textarea.dispatchEvent(new Event('input'));
        speak.instructions.value = r.instructions || '';
        speak.delivery.setValue('');
        speak.emotions.setValue([]);
        speak.pacing.setValue('natural');
        if (r.language) speak.language.value = r.language;
        if (r.speed) speak.speed.setValue(r.speed);
        if (r.pause_ms !== undefined) speak.pause.setValue(r.pause_ms);
        if (r.seed !== undefined) speak.seed.set(r.seed, true);
        speak.title.value = r.title || '';
      } else if (job.operation === 'voice_design') {
        setMode('design');
        design.description.set(r.description || '');
        design.text.set(r.text || '');
        if (r.seed !== undefined) design.seed.set(r.seed, true);
      } else if (job.operation === 'dialogue') {
        setMode('dialogue');
        for (const row of lineRows) { row.remove(); pickers.delete(row.picker); }
        lineRows.length = 0;
        (r.segments || []).forEach((s) => addLine(s));
        if (r.pause_ms !== undefined) dialogue.pause.setValue(r.pause_ms);
        if (r.seed !== undefined) dialogue.seed.set(r.seed, true);
      }
      panel.scrollIntoView({ block: 'start', behavior: 'smooth' });
      toast('Settings loaded.', 'ok', 2000);
    }

    // ------------------------------------------------ API panel
    const gateway = (model && model.gateway_url) || 'http://<gateway-host>:4000';
    const origin = location.origin;
    const apiPanel = disclosure('API access', h('div', { class: 'stack-sm' },
      h('p', { class: 'small' }, 'Use an API key from the Control Center (API Keys) that allows ', h('code', {}, 'gx-voice'), '. Replace ', h('code', {}, '$GX_API_KEY'), ' with it; never paste a key into a shared page.'),
      h('p', { class: 'field-label' }, 'OpenAI-compatible speech (LiteLLM gateway)'),
      h('pre', { class: 'voice-code', tabindex: '0', 'aria-label': 'curl example for the speech endpoint' },
        `curl -sS ${gateway}/v1/audio/speech \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "gx-voice", "voice": "aiden", "input": "Hello from gx-voice.", "response_format": "mp3"}' \\\n  -o speech.mp3`),
      h('p', { class: 'field-label' }, 'Voice API (takes, saved voices, design, clone, dialogue)'),
      h('pre', { class: 'voice-code', tabindex: '0', 'aria-label': 'curl example for the voice API' },
        `curl -sS ${origin}/v1/voice/speech \\\n  -H "Authorization: Bearer $GX_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"voice_id": "preset:ryan", "text": "Two takes, please.", "takes": 2}'\n# then poll GET ${origin}/v1/voice/jobs/<id> and download\n# GET ${origin}/v1/voice/jobs/<id>/takes/0/content?format=mp3`),
      h('p', { class: 'small muted' }, 'Voice names for the speech endpoint: a preset (aiden, ryan, vivian, serena, uncle_fu, dylan, eric, ono_anna, sohee), one of your saved voice names, or a voice id.')),
    { ic: 'command' });

    // ------------------------------------------------ layout
    const eng = model && model.state;
    const stateBadge = modelError ? badge('Voice engine unreachable', 'danger')
      : badge({ unloaded: 'Engine idle · loads with your first job', loading: 'Engine loading', ready: 'Engine ready', busy: 'Engine busy', unloading: 'Engine unloading', error: 'Engine error' }[eng] || `Engine ${eng}`,
        { ready: 'ok', busy: 'info', loading: 'info', error: 'danger' }[eng] || 'neutral');
    const results = h('div', { class: 'stage' },
      modelError ? callout('warn', 'gx10-02 is not answering right now', `${friendlyError(modelError.message).text} Your voices are listed; generation resumes when the voice service is back.`) : null,
      jobsBox,
      h('section', { class: 'ws-section', 'aria-labelledby': 'voice-takes-h' },
        h('div', { class: 'row-between' }, h('h2', { class: 'section-title', id: 'voice-takes-h' }, 'Takes and history'),
          h('a', { class: 'link small', href: '#/library?type=audio' }, 'Voice audio in Library')),
        takesList, h('div', { class: 'center' }, moreTakes)),
      h('section', { class: 'ws-section', 'aria-labelledby': 'voice-lib-h' },
        h('h2', { class: 'section-title', id: 'voice-lib-h' }, 'Voice Library'),
        libraryGrid,
        disclosure(`Preset voices (${voices.filter((v) => v.builtin).length})`, presetGrid, { ic: 'mic' })),
      apiPanel);
    replace(root,
      pageHeader('Voice', `Qwen3-TTS 1.7B · voiceovers, narration, designed and cloned voices`, [stateBadge]),
      h('div', { class: 'workspace workspace-voice' }, panel, results));

    setMode(session.mode);
    syncStats();
    syncInstructionState();
    renderLibrary();
    for (const id of session.jobs.slice(0, 4).reverse()) {
      const j = center.jobs.get(id);
      if (j && !phaseOf(j).terminal) showJob(j);
    }
    for (const j of center.active()) if (isVoice(j)) showJob(j);
    await loadHistory();

    if (ctx.query.job && /^vj_[0-9a-f]{32}$/.test(ctx.query.job)) {
      try {
        const j = await api.get(`/api/voice/jobs/${ctx.query.job}`);
        session.focus = j.id;
        upsert(j);
      } catch (e) { toast(friendlyError(e.message).text, 'danger'); }
    }
    if (ctx.query.asset) {
      try {
        const a = await getAsset(ctx.query.asset);
        const ref = String(a.source_ref || '');
        const m = /^(vj_[0-9a-f]{32})#\d$/.exec(ref);
        if (m) {
          const j = await api.get(`/api/voice/jobs/${m[1]}`);
          session.focus = j.id;
          upsert(j);
        } else if (a.type === 'audio') {
          setMode('clone');
          setRef(a);
        }
      } catch (e) { toast(friendlyError(e.message).text, 'danger'); }
    }
    const refresh = setInterval(() => { if (!document.hidden && alive) loadHistory(); }, 20000);
    const unsubAssets = onAsset((a, deleted) => {
      if (!alive || a.type !== 'audio') return;
      let changed = false;
      for (const j of history) {
        for (const t of j.takes || []) {
          if (t.asset_id === a.id && deleted) { t.asset_id = null; changed = true; }
        }
      }
      if (changed) renderTakes();
    });
    return () => { alive = false; clearInterval(refresh); unsubJobs(); unsubAssets(); };
  },
};
