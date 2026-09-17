// Video workspace (gx-video): text to video (with Wan 2.2 LoRAs, presets and
// sampler settings), image to video, video edit; plus video history and errors.
import { api, getAsset, getMediaOptions, upload } from '../api.js';
import { duplicateAsset, emitAsset, pickAsset, sourceChip } from '../assets.js';
import { h, toast, replace } from '../dom.js';
import { center, friendlyError, submitMedia } from '../jobs.js';
import { pref } from '../prefs.js';
import { button, chips, composer, disclosure, dropzone, field, kv, numberInput, pageHeader, readNumber, seedField, select, slider, tabs, textInput, callout } from '../ui.js';
import { createErrorsPanel, createHistoryPanel, createLoraStack, generateVideo, generationDetails, getVideoConfig, openAdvancedDialog, presetPicker, previewWorkflow } from '../wan.js';
import { createStage } from '../workspace.js';

const session = { results: [], jobs: [], done: new Set(), selectedId: null, compareId: null };
const ADV_DEFAULTS = { shift: 5, cfg: 1, steps: 4, boundary: 2, sampler_name: 'euler', scheduler: 'simple' };
// size starts from Settings (the page falls back to the first offered size)
const draft = {
  mode: 't2v', sources: { i2v: null, v2v: null }, prompt: '', size: pref('default_video_size', '640x640'), seconds: 3, fps: 16,
  strength: 0.85, title: '', negative: null, suffix: '', stack: [], advanced: { ...ADV_DEFAULTS }, presetId: null,
};

const MODES = [['t2v', 'Text to Video', 'sparkles'], ['i2v', 'Image to Video', 'image'], ['v2v', 'Video Edit', 'wand']];
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
const VIDEO_TYPES = ['video/mp4', 'video/webm', 'video/quicktime'];

function strengthText(v) {
  return v >= 0.5
    ? `Strong instruction edit (${v.toFixed(2)}): a keyframe is edited to follow your instruction, then the motion is rebuilt.`
    : `Restyle (${v.toFixed(2)}): keeps the original motion and composition and changes the look.`;
}

export default {
  title: 'Video',
  async mount(root, ctx) {
    const [options, config] = await Promise.all([
      getMediaOptions().catch(() => ({ video_sizes: ['640x640'] })),
      getVideoConfig().catch(() => null),
    ]);
    const sizes = options.video_sizes || [];
    if (!sizes.includes(draft.size)) draft.size = sizes[0];
    if (ctx.query.mode && MODES.some(([m]) => m === ctx.query.mode)) draft.mode = ctx.query.mode;
    const defaults = (config && config.defaults) || {};
    if (draft.negative === null) draft.negative = defaults.negative_prompt || '';

    const stage = createStage({
      type: 'video', session,
      emptyTitle: 'Your videos appear here',
      emptyText: 'Describe a shot on the left and press Generate. Clips render on gx10-02 and can take a few minutes.',
      actions: (a) => [
        button('Edit again', { icon: 'wand', size: 'sm', attrs: { 'data-action': 'edit-again' }, onClick: () => { setSource('v2v', a); setMode('v2v'); prompt.textarea.focus(); } }),
        a.settings && a.settings.requested ? button('Variation', { icon: 'shuffle', size: 'sm', attrs: { 'data-action': 'variation' }, onClick: async () => { const j = await duplicateAsset(a, { newSeed: true }); if (j) stage.trackJob(j); } }) : null,
        button('Re-prompt', { icon: 'refresh', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'reprompt' }, onClick: () => reprompt(a) }),
        a.settings && a.settings.requested ? button('Run again', { icon: 'copy', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'duplicate' }, onClick: async () => { const j = await duplicateAsset(a); if (j) stage.trackJob(j); } }) : null,
        a.settings && a.settings.wan && a.settings.wan.generation_id ? button('Workflow', { icon: 'layers', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'workflow' }, onClick: () => openGeneration(a.settings.wan.generation_id) }) : null,
      ],
    });

    // ---------------------------------------------------------- controls
    const modeTabs = tabs(MODES, draft.mode, (m) => setMode(m), { label: 'Video mode' });
    const sourceBox = h('div', { class: 'source-box' });
    const sourceLabel = h('p', { class: 'field-label' }, 'Source');
    const drop = dropzone({ accept: '', label: 'Upload', hint: 'PNG, JPEG or WebP · up to 25 MB', onFile: (f) => doUpload(f) });
    const pickBtn = button('Choose from Library', { icon: 'library', size: 'sm', onClick: async () => {
      const type = draft.mode === 'i2v' ? 'image' : 'video';
      const a = await pickAsset({ type, title: type === 'image' ? 'Choose a start image' : 'Choose a video to edit' });
      if (a) setSource(draft.mode, a);
    } });
    const sourceSection = h('div', { class: 'panel-section' }, sourceLabel, sourceBox, h('div', { class: 'row-wrap' }, pickBtn), drop);

    const prompt = composer({ label: 'Prompt', placeholder: 'Slow dolly shot through a neon-lit alley in the rain…', maxLength: 4000, rows: 5, value: draft.prompt, onSubmit: () => generate(), id: 'video-prompt' });
    const suffix = textInput({ value: draft.suffix, maxLength: 1000, placeholder: 'e.g. cinematic lighting, film grain', attrs: { id: 'video-suffix' } });
    const suffixField = field('Style wording', suffix, { hint: 'Added to the end of the prompt. Presets fill this in.' });
    const negative = h('textarea', { class: 'input', id: 'video-negative', rows: 2, maxlength: 4000 });
    negative.value = draft.negative;
    const negativeField = field('Negative prompt', negative, { hint: 'What the video should avoid.' });

    const model = config && config.model;
    const modelSection = h('section', { class: 'panel-section', 'aria-labelledby': 'video-model-h' },
      h('h2', { class: 'field-label', id: 'video-model-h' }, 'Model'),
      model ? kv([['Model', model.label], ['High-noise expert', model.high_model || 'unknown'], ['Low-noise expert', model.low_model || 'unknown'],
        ['Built-in LoRAs', (model.base_loras || []).join(', ') || 'none'], ['LoRA support', model.lora_support ? 'yes' : 'no']])
        : callout('warn', null, 'Model details are not available right now; generation still works.'));

    const stack = createLoraStack({ onChange: (items) => { draft.stack = items; } });
    if (defaults.strength_high !== undefined) stack.setDefaults(defaults);
    stack.set(draft.stack);
    stack.hydrate();

    const presets = presetPicker({ getCurrent: () => currentPreset(), onApply: (p) => applyPreset(p) });

    const sizeChips = chips(sizes.map((s) => [s, s.replace('x', '×')]), { value: draft.size, label: 'Video size', cls: 'chips-size' });
    const seconds = slider({ label: 'Length', min: 0.5, max: 10, step: 0.5, value: draft.seconds, format: (v) => `${v.toFixed(1)} s`, onInput: () => syncFrames() });
    const fps = slider({ label: 'Frame rate', min: 8, max: 24, step: 1, value: draft.fps, format: (v) => `${v} fps`, onInput: () => syncFrames() });
    const framesNote = h('p', { class: 'field-hint', 'aria-live': 'polite' });
    const strengthHint = h('p', { class: 'field-hint strength-explain', 'aria-live': 'polite' });
    const strength = slider({ label: 'Edit strength', min: 0.05, max: 1, step: 0.05, value: draft.strength, format: (v) => v.toFixed(2), onInput: (v) => { strengthHint.textContent = strengthText(v); } });
    strengthHint.textContent = strengthText(draft.strength);
    const strengthBox = h('div', { class: 'stack-sm' }, strength, strengthHint);
    const seed = seedField('video');
    const title = textInput({ value: draft.title, maxLength: 200, placeholder: 'Optional' });

    const adv = {
      shift: numberInput({ value: draft.advanced.shift, min: 0.5, max: 20, step: 0.5, attrs: { id: 'video-shift' } }),
      cfg: numberInput({ value: draft.advanced.cfg, min: 1, max: 10, step: 0.5, attrs: { id: 'video-cfg' } }),
      steps: numberInput({ value: draft.advanced.steps, min: 2, max: 40, step: 1, attrs: { id: 'video-steps' } }),
      boundary: numberInput({ value: draft.advanced.boundary, min: 1, max: 39, step: 1, attrs: { id: 'video-boundary' } }),
      sampler_name: select(((config && config.samplers) || ['euler']).map((s) => [s, s]), draft.advanced.sampler_name, { attrs: { id: 'video-sampler' } }),
      scheduler: select(((config && config.schedulers) || ['simple']).map((s) => [s, s]), draft.advanced.scheduler, { attrs: { id: 'video-scheduler' } }),
    };
    const advError = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
    const previewBtn = button('Preview workflow', { icon: 'layers', size: 'sm', attrs: { 'data-action': 'preview-workflow' }, onClick: () => preview() });
    const advancedBox = disclosure('Advanced settings', h('div', { class: 'stack-sm' },
      h('p', { class: 'field-hint' }, 'The standard graph samples 4 steps (high-noise expert first, then low-noise) with CFG 1.0, as its built-in distillation LoRAs expect.'),
      h('div', { class: 'grid-2' },
        field('Shift', adv.shift), field('CFG', adv.cfg),
        field('Steps', adv.steps), field('Switch to low noise at step', adv.boundary),
        field('Sampler', adv.sampler_name), field('Scheduler', adv.scheduler)),
      advError,
      h('div', { class: 'row-wrap' },
        button('Reset', { size: 'sm', variant: 'ghost', attrs: { 'data-action': 'reset-advanced' }, onClick: () => setAdvanced(ADV_DEFAULTS) }),
        previewBtn)), { ic: 'sliders' });
    advancedBox.id = 'video-advanced';

    const formError = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
    const genBtn = button('Generate video', { icon: 'sparkles', variant: 'primary', attrs: { id: 'generate-btn', class: 'btn btn-primary btn-lg btn-block' }, onClick: () => generate() });
    const loraOnly = [suffixField, presets, modelSection, stack, advancedBox];
    const settingsSection = h('section', { class: 'panel-section', 'aria-labelledby': 'video-settings-h' },
      h('h2', { class: 'field-label', id: 'video-settings-h' }, 'Generation settings'),
      h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Size'), sizeChips),
      seconds, fps, framesNote, strengthBox, seed, field('Title', title));

    const panel = h('aside', { class: 'panel', 'aria-label': 'Video settings' },
      modeTabs,
      h('div', { class: 'panel-body' },
        sourceSection, prompt, suffixField, negativeField, presets, modelSection, stack, settingsSection, advancedBox,
        callout('info', null, 'Video renders take a few minutes. You can leave this page; progress continues in Activity.')),
      h('div', { class: 'panel-foot' }, formError, genBtn));

    const history = createHistoryPanel({ onLoad: (g) => loadGeneration(g), onPlay: async (g) => {
      try { stage.select(await getAsset(g.asset_id)); } catch (err) { toast(err.message, 'danger'); }
    } });
    const errors = createErrorsPanel({ onLoad: (g) => loadGeneration(g) });
    stage.el.append(history, errors);

    replace(root,
      pageHeader('Video', 'gx-video · Wan 2.2 text to video with LoRAs, image to video and video edits'),
      h('div', { class: 'workspace' }, panel, stage.el));

    // ---------------------------------------------------------- behaviour
    function syncFrames() {
      const s = seconds.getValue();
      const f = fps.getValue();
      let n = Math.max(5, Math.min(Math.round(s * f), 161));
      const r = (n - 1) % 4;
      n = r <= 2 ? n - r : n - r + 4;
      if (n > 161) n -= 4;
      framesNote.textContent = draft.mode === 'v2v' ? '' : `${n} frames (Wan needs 4k+1 frames).`;
    }

    function setMode(m) {
      draft.mode = m;
      modeTabs.select(m);
      sourceSection.hidden = m === 't2v';
      fps.hidden = m === 'v2v';
      strengthBox.hidden = m !== 'v2v';
      for (const el of loraOnly) el.hidden = m !== 't2v';
      sourceLabel.textContent = m === 'i2v' ? 'Start image' : 'Video to edit';
      const types = m === 'i2v' ? IMAGE_TYPES : VIDEO_TYPES;
      drop.input.accept = types.join(',');
      drop.querySelector('.dropzone-title').textContent = m === 'i2v' ? 'Upload an image' : 'Upload a video';
      drop.querySelector('.dropzone-hint').textContent = m === 'i2v' ? 'PNG, JPEG or WebP · up to 25 MB' : 'MP4, WebM or MOV · up to 150 MB';
      prompt.querySelector('label').textContent = m === 'v2v' ? 'Edit instruction' : 'Prompt';
      renderSource();
      syncFrames();
      formError.hidden = true;
    }

    function renderSource() {
      const a = draft.sources[draft.mode];
      replace(sourceBox, a ? sourceChip(a, { label: draft.mode === 'i2v' ? 'Start image' : 'Source video', onClear: () => setSource(draft.mode, null) })
        : h('p', { class: 'muted small' }, 'Nothing selected yet.'));
    }

    function setSource(m, a) {
      draft.sources[m] = a;
      renderSource();
    }

    function lockButton() { return seed.querySelector('button[aria-pressed]'); }
    function seedLocked() { const b = lockButton(); return Boolean(b) && b.getAttribute('aria-pressed') === 'true'; }
    function setSeed(value, fixed) {
      if (fixed && value !== null && value !== undefined) { seed.set(value, true); return; }
      seed.set(null);
      if (seedLocked()) lockButton().click();
    }

    function readAdvanced() {
      const out = {};
      for (const k of ['shift', 'cfg', 'steps', 'boundary']) {
        const v = readNumber(adv[k], { integer: k === 'steps' || k === 'boundary' });
        out[k] = v === null ? ADV_DEFAULTS[k] : v;
      }
      out.sampler_name = adv.sampler_name.value;
      out.scheduler = adv.scheduler.value;
      return out;
    }

    function setAdvanced(values) {
      const v = { ...ADV_DEFAULTS, ...(values || {}) };
      for (const k of ['shift', 'cfg', 'steps', 'boundary']) adv[k].value = String(v[k]);
      adv.sampler_name.value = v.sampler_name;
      adv.scheduler.value = v.scheduler;
      advError.hidden = true;
    }

    function checkAdvanced(a) {
      if (!(a.shift >= 0.5 && a.shift <= 20)) return 'Shift must be between 0.5 and 20.';
      if (!(a.cfg >= 1 && a.cfg <= 10)) return 'CFG must be between 1 and 10.';
      if (!(a.steps >= 2 && a.steps <= 40)) return 'Steps must be between 2 and 40.';
      if (!(a.boundary >= 1 && a.boundary < a.steps)) return 'The switch step must be at least 1 and lower than the step count.';
      return null;
    }

    function currentPreset() {
      return {
        loras: stack.payload(), size: sizeChips.getValue(), seconds: seconds.getValue(), fps: fps.getValue(),
        seed_mode: seedLocked() ? 'fixed' : 'random', seed: seedLocked() ? readNumber(seed.input, { integer: true }) : null,
        prompt: '', prompt_suffix: suffix.value.trim(), negative_prompt: negative.value.trim(), negative_mode: 'replace',
        advanced: readAdvanced(),
      };
    }

    function stackFromRecord(list) {
      return (list || []).map((l) => ({
        entry_id: l.entry_id, display_name: l.display_name || l.entry_id, kind: l.kind, apply: l.apply || null,
        enabled: l.enabled !== false, strength_high: l.strength_high ?? defaults.strength_high ?? 0.8,
        strength_low: l.strength_low ?? defaults.strength_low ?? 0.8, high_file: l.high_file, low_file: l.low_file, file: l.file,
        apply_options: l.apply ? [l.apply] : [], problems: [], usable: true, missing: Boolean(l.missing),
      }));
    }

    function applyPreset(p) {
      const d = p.data;
      if (sizes.includes(d.size)) sizeChips.setValue(d.size);
      seconds.setValue(d.seconds);
      fps.setValue(d.fps);
      setSeed(d.seed, d.seed_mode === 'fixed');
      suffix.value = d.prompt_suffix || '';
      if (d.negative_mode === 'append' && d.negative_prompt) {
        negative.value = [negative.value.trim(), d.negative_prompt].filter(Boolean).join(', ');
      } else {
        negative.value = d.negative_prompt || '';
      }
      if (d.prompt) prompt.set(d.prompt);
      setAdvanced(d.advanced);
      stack.set(stackFromRecord(d.loras));
      stack.hydrate();
      draft.presetId = p.id;
      setMode('t2v');
      syncFrames();
    }

    async function loadGeneration(g) {
      const req = g.request || {};
      if (req.kind && req.kind !== 't2v') {
        await reprompt({ prompt: g.prompt, settings: { requested: req } });
        return;
      }
      prompt.set(req.prompt || g.prompt || '');
      suffix.value = '';
      negative.value = req.negative_prompt ?? g.negative_prompt ?? '';
      if (sizes.includes(req.size || g.size)) sizeChips.setValue(req.size || g.size);
      if (req.seconds || g.seconds) seconds.setValue(req.seconds || g.seconds);
      if (req.fps || g.fps) fps.setValue(req.fps || g.fps);
      setSeed(req.seed ?? g.seed, true);
      title.value = req.title || '';
      setAdvanced(req.advanced || g.settings);
      stack.set(stackFromRecord(g.loras));
      stack.hydrate();
      draft.presetId = req.preset_id || null;
      setMode('t2v');
      syncFrames();
      prompt.textarea.focus();
      toast('Settings loaded into the form.', 'ok', 2500);
    }

    async function openGeneration(id) {
      try {
        generationDetails(await api.get(`/api/video/generations/${id}`), { onLoad: (g) => loadGeneration(g) });
      } catch (err) { toast(err.message, 'danger'); }
    }

    async function reprompt(a) {
      const req = (a.settings && a.settings.requested) || {};
      const wanReq = req.wan && req.wan.request;
      if (wanReq) {
        await loadGeneration({ request: wanReq, loras: req.wan.loras, prompt: wanReq.prompt });
        return;
      }
      const kind = MODES.some(([m]) => m === req.kind) ? req.kind : 't2v';
      prompt.set(a.prompt || req.prompt || '');
      if (req.size && sizes.includes(req.size)) sizeChips.setValue(req.size);
      if (req.seconds) seconds.setValue(req.seconds);
      if (req.fps) fps.setValue(req.fps);
      if (req.negative_prompt) negative.value = req.negative_prompt;
      if (req.strength) { strength.setValue(req.strength); strengthHint.textContent = strengthText(req.strength); }
      if (req.source_id && kind !== 't2v') {
        try { setSource(kind, await getAsset(req.source_id)); } catch { setSource(kind, null); }
      }
      setMode(kind);
      prompt.textarea.focus();
    }

    async function doUpload(file) {
      const m = draft.mode;
      const types = m === 'i2v' ? IMAGE_TYPES : VIDEO_TYPES;
      const limit = (m === 'i2v' ? 25 : 150) * 1024 * 1024;
      if (!types.includes(file.type)) { toast(m === 'i2v' ? 'Choose a PNG, JPEG or WebP image.' : 'Choose an MP4, WebM or MOV video.', 'danger'); return; }
      if (file.size > limit) { toast(`That file is larger than ${limit / 1048576} MB.`, 'danger'); return; }
      drop.progress(0);
      try {
        const asset = await upload('/api/media/upload', file, { title: file.name.replace(/\.[^.]+$/, ''), onProgress: (f) => drop.progress(f) });
        drop.progress(null);
        emitAsset(asset);
        setSource(m, asset);
        toast('Uploaded.', 'ok');
      } catch (err) {
        drop.progress(null);
        toast(friendlyError(err.message).text, 'danger');
      }
    }

    function fail(msg, focus) {
      formError.hidden = false;
      formError.textContent = msg;
      if (focus) focus.focus();
    }

    function t2vBody(text, seedValue) {
      const style = suffix.value.trim();
      const body = {
        prompt: style ? `${text}, ${style}` : text, negative_prompt: negative.value.trim(), size: sizeChips.getValue(),
        seconds: seconds.getValue(), fps: fps.getValue(), loras: stack.payload(), advanced: readAdvanced(),
      };
      if (seedValue !== undefined) body.seed = seedValue;
      if (title.value.trim()) body.title = title.value.trim();
      if (draft.presetId) body.preset_id = draft.presetId;
      return body;
    }

    async function preview() {
      advError.hidden = true;
      const text = prompt.get();
      if (!text) { advError.textContent = 'Write a prompt first.'; advError.hidden = false; return; }
      const a = readAdvanced();
      const problem = checkAdvanced(a) || stack.validate();
      if (problem) { advError.textContent = problem; advError.hidden = false; return; }
      previewBtn.disabled = true;
      try {
        const seedValue = readNumber(seed.input, { integer: true });
        openAdvancedDialog(await previewWorkflow(t2vBody(text, seedValue === null ? undefined : seedValue)));
      } catch (err) {
        advError.textContent = err.message;
        advError.hidden = false;
      } finally { previewBtn.disabled = false; }
    }

    async function generate() {
      formError.hidden = true;
      const kind = draft.mode;
      const text = prompt.get();
      if (!text) return fail(kind === 'v2v' ? 'Describe the edit you want.' : 'Write a prompt first.', prompt.textarea);
      const src = draft.sources[kind];
      if (kind !== 't2v' && !src) return fail(kind === 'i2v' ? 'Choose or upload a start image first.' : 'Choose or upload a video first.', pickBtn);
      if (kind === 't2v') {
        const problem = stack.validate() || checkAdvanced(readAdvanced());
        if (problem) {
          if (!advancedBox.open && checkAdvanced(readAdvanced())) advancedBox.open = true;
          return fail(problem);
        }
      }
      genBtn.disabled = true;
      try {
        let job;
        if (kind === 't2v') {
          job = await generateVideo(t2vBody(text, seed.next()));
          center.track(job);
        } else {
          const body = { kind, prompt: text, size: sizeChips.getValue(), seconds: seconds.getValue(), seed: seed.next() };
          if (negative.value.trim()) body.negative_prompt = negative.value.trim();
          if (kind !== 'v2v') body.fps = fps.getValue();
          if (kind === 'v2v') body.strength = strength.getValue();
          if (src) body.source_id = src.id;
          if (title.value.trim()) body.title = title.value.trim();
          job = await submitMedia(body);
        }
        stage.trackJob(job);
        toast('Queued.', 'ok', 2000);
      } catch (err) {
        fail(friendlyError(err.message).text);
      } finally {
        genBtn.disabled = false;
      }
      return undefined;
    }

    setMode(draft.mode);
    if (ctx.query.source) {
      try {
        const a = await getAsset(ctx.query.source);
        const m = a.type === 'image' ? 'i2v' : a.type === 'video' ? 'v2v' : null;
        if (m) { setSource(m, a); setMode(m); }
      } catch (err) { toast(err.message, 'danger'); }
    }
    if (ctx.query.asset) {
      try { const a = await getAsset(ctx.query.asset); if (a.type === 'video') stage.select(a); } catch (err) { toast(err.message, 'danger'); }
    }
    if (ctx.query.generation && /^[0-9a-f]{16}$/.test(ctx.query.generation)) openGeneration(ctx.query.generation);
    if (ctx.query.focus || ctx.query.source) prompt.textarea.focus();

    return () => {
      Object.assign(draft, {
        prompt: prompt.textarea.value, size: sizeChips.getValue(), seconds: seconds.getValue(), fps: fps.getValue(),
        strength: strength.getValue(), title: title.value, negative: negative.value, suffix: suffix.value,
        stack: stack.get(), advanced: readAdvanced(),
      });
      history.destroy();
      errors.destroy();
      stage.destroy();
    };
  },
};
