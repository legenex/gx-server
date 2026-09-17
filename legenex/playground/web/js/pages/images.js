// Images workspace (gx-image): generate, edit, variation, compare, lineage.
// Build V3: model selector (Qwen Image 2512 / Qwen Image Edit 2511 /
// VisionmasterPro_V3), edit modes, edit quality and an optional edit mask.
import { getAsset, getMediaOptions, upload } from '../api.js';
import { duplicateAsset, emitAsset, pickAsset, sourceChip } from '../assets.js';
import { h, toast, replace } from '../dom.js';
import { friendlyError, submitMedia } from '../jobs.js';
import { maskEditor } from '../maskpaint.js';
import { navigate } from '../nav.js';
import { pref } from '../prefs.js';
import {
  button, chips, composer, disclosure, dropzone, field, numberInput, pageHeader, readNumber, seedField, select, slider,
  tabs, textInput, toggle,
} from '../ui.js';
import { createStage } from '../workspace.js';

const session = { results: [], jobs: [], done: new Set(), selectedId: null, compareId: null };
// size and generation model start from Settings (ignored when the model does not offer them)
const draft = {
  mode: 'generate', source: null, prompt: '', negative: '', size: pref('default_image_size'), n: 1, quality: 'standard',
  steps: null, guidance: null, strength: 0.6, title: '', uncensored: true, qualityTags: true,
  model: { generate: pref('default_image_model'), edit: null }, editMode: {}, editQuality: 'fast',
};

const MODES = [['generate', 'Generate', 'sparkles'], ['edit', 'Edit', 'wand'], ['variation', 'Variation', 'shuffle']];
const QUALITY = [['fast', 'Fast'], ['standard', 'Standard'], ['hd', 'HD']];
const EDIT_QUALITY = [['fast', 'Fast', '4 steps'], ['quality', 'High quality', 'slower']];
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
const MAX_UPLOAD = 25 * 1024 * 1024;
const OPERATION = { generate: 'generate', edit: 'edit', variation: 'variation' };

function aspect(size) {
  const [w, hh] = size.split('x').map(Number);
  if (!w || !hh) return '';
  if (w === hh) return 'Square';
  return w > hh ? 'Landscape' : 'Portrait';
}

export default {
  title: 'Images',
  async mount(root, ctx) {
    const options = await getMediaOptions().catch(() => ({ image_sizes: ['1328x1328', '1024x1024'], image_models: [] }));
    const models = options.image_models || [];
    const defaults = options.default_image_model || {};
    const byId = new Map(models.map((m) => [m.id, m]));
    const modelsFor = (mode) => models.filter((m) => m.operations.includes(OPERATION[mode]));
    const pick = (mode) => {
      const wanted = draft.model[mode === 'generate' ? 'generate' : 'edit'];
      const list = modelsFor(mode);
      return list.find((m) => m.id === wanted)
        || list.find((m) => m.id === (mode === 'generate' ? defaults.generate : defaults.edit)) || list[0] || null;
    };

    // ------------------------------------------------------------ stage
    const stage = createStage({
      type: 'image', session,
      emptyTitle: 'Your images appear here',
      emptyText: 'Describe an image on the left and press Generate, or pick one from History below.',
      actions: (a) => [
        button('Edit', { icon: 'wand', size: 'sm', variant: 'secondary', attrs: { 'data-action': 'edit' }, onClick: () => useSource(a, 'edit') }),
        button('Variation', { icon: 'shuffle', size: 'sm', variant: 'secondary', attrs: { 'data-action': 'variation' }, onClick: () => useSource(a, 'variation') }),
        button('Re-prompt', { icon: 'refresh', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'reprompt' }, onClick: () => reprompt(a) }),
        button('Reuse settings', { icon: 'sliders', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'reuse-settings' }, onClick: () => { applySettings(a); toast('Settings loaded into the composer.', 'ok', 2500); } }),
        a.seed !== null && a.seed !== undefined ? button('Reuse seed', { icon: 'dice', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'reuse-seed' }, onClick: () => { seed.set(a.seed, true); toast(`Seed ${a.seed} locked.`, 'ok', 2500); } }) : null,
        (a.settings && a.settings.requested) ? button('Run again', { icon: 'copy', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'duplicate' }, onClick: async () => { const j = await duplicateAsset(a); if (j) stage.trackJob(j); } }) : null,
        button('Make video', { icon: 'film', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'make-video' }, onClick: () => navigate('video', { mode: 'i2v', source: a.id }) }),
      ],
    });

    // ------------------------------------------------------------ composer
    const modeTabs = tabs(MODES, draft.mode, (m) => setMode(m), { label: 'Image mode' });
    const modelSelect = select([], '', { attrs: { id: 'image-model', 'data-testid': 'image-model' }, onChange: (id) => setModel(id) });
    const modelHint = h('p', { class: 'field-hint', id: 'image-model-hint' });
    modelSelect.setAttribute('aria-describedby', 'image-model-hint');
    const modelField = h('div', { class: 'field' },
      h('label', { class: 'field-label', for: 'image-model' }, 'Model'), modelSelect, modelHint);

    const sourceBox = h('div', { class: 'source-box' });
    const drop = dropzone({
      accept: IMAGE_TYPES.join(','), label: 'Upload an image', hint: 'PNG, JPEG or WebP · up to 25 MB · drag & drop',
      onFile: (f) => doUpload(f),
    });
    const sourceSection = h('div', { class: 'panel-section', id: 'source-section' },
      h('p', { class: 'field-label' }, 'Source image'),
      sourceBox,
      h('div', { class: 'row-wrap' }, button('Choose from Library', { icon: 'library', size: 'sm', onClick: async () => {
        const a = await pickAsset({ type: 'image', title: 'Choose a source image' });
        if (a) setSource(a);
      } })),
      drop,
      h('p', { class: 'field-hint' }, 'Edits never overwrite the source: every result is a new image linked to its parent.'));

    const editModeHint = h('p', { class: 'field-hint', id: 'edit-mode-hint', 'aria-live': 'polite' });
    let editModeChips = chips([], { label: 'Edit mode' });
    const editModeWrap = h('div', { class: 'field', id: 'edit-mode-field' },
      h('p', { class: 'field-label' }, 'Edit mode'), editModeChips, editModeHint);
    const editQualityChips = chips(EDIT_QUALITY, { value: draft.editQuality, label: 'Edit quality', onChange: () => syncControls() });
    const editQualityField = h('div', { class: 'field', id: 'edit-quality-field' },
      h('p', { class: 'field-label' }, 'Edit quality'), editQualityChips,
      h('p', { class: 'field-hint' }, 'High quality runs the full model with real guidance (about 20 steps; slower). It also uses the negative prompt.'));

    const mask = maskEditor({ onChange: () => syncControls() });
    const maskNote = h('p', { class: 'field-hint', id: 'mask-note' });
    const maskBox = disclosure('Limit the edit to an area (mask)', h('div', { class: 'stack' }, maskNote, mask), { ic: 'brush' });
    maskBox.id = 'mask-section';

    const prompt = composer({ label: 'Prompt', placeholder: 'A cozy reading nook in a treehouse, warm light, film grain…', maxLength: 4000, rows: 5, value: draft.prompt, onSubmit: () => generate(), id: 'image-prompt' });
    const sizeWrap = h('div', { class: 'field' }, h('p', { class: 'field-label', id: 'size-label' }, 'Size'));
    let sizeChips = chips([], { label: 'Image size', cls: 'chips-size' });
    sizeWrap.append(sizeChips);
    const countChips = chips([['1', '1'], ['2', '2'], ['3', '3'], ['4', '4']], { value: String(draft.n), label: 'Number of images' });
    const qualityChips = chips(QUALITY, { value: draft.quality, label: 'Quality' });
    const qualityField = h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Quality'), qualityChips);
    const strength = slider({ label: 'Strength', min: 0, max: 1, step: 0.05, value: draft.strength, format: (v) => v.toFixed(2), hint: 'How far the result may move away from the source.' });
    const seed = seedField('image');
    const title = textInput({ value: draft.title, maxLength: 200, placeholder: 'Optional' });
    const uncensored = toggle('Uncensored adapter', draft.uncensored, { hint: 'Qwen only: applies the NSFW-capable adapter (the default for this studio).' });
    const qualityTags = toggle('Quality tags', draft.qualityTags, { hint: 'VisionmasterPro_V3 only: appends the quality tags this checkpoint was trained with.' });
    const negative = h('textarea', { class: 'input', rows: 2, maxlength: 4000, placeholder: 'blurry, text, watermark…' });
    negative.value = draft.negative;
    const steps = numberInput({ min: 1, max: 100, step: 1, placeholder: 'Auto', value: draft.steps });
    const guidance = numberInput({ min: 0, max: 20, step: 0.5, placeholder: 'Auto', value: draft.guidance });
    const negField = field('Negative prompt', negative, { hint: 'What to avoid.' });
    const guidanceField = field('Guidance', guidance, { hint: '0–20. Higher follows the prompt more literally.' });
    const stepsField = field('Steps', steps, { hint: 'Leave empty for the model default.' });
    const advanced = disclosure('Advanced', h('div', { class: 'stack' }, negField, h('div', { class: 'grid-2' }, stepsField, guidanceField)), { ic: 'sliders' });
    const genSection = h('div', { class: 'stack' },
      sizeWrap,
      h('div', { class: 'row-between align-start' },
        h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Images'), countChips),
        qualityField));
    const formError = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
    const genBtn = button('Generate', { icon: 'sparkles', variant: 'primary', size: 'lg', attrs: { id: 'generate-btn', class: 'btn btn-primary btn-lg btn-block' }, onClick: () => generate() });

    const panel = h('aside', { class: 'panel', 'aria-label': 'Image settings' },
      modeTabs,
      h('div', { class: 'panel-body' },
        modelField, sourceSection, editModeWrap, prompt, maskBox, genSection, editQualityField, strength,
        seed, field('Title', title), uncensored, qualityTags, advanced),
      h('div', { class: 'panel-foot' }, formError, genBtn));

    replace(root,
      pageHeader('Images', 'gx-image · text to image, instruction edits, masked edits and variations', [
        button('Upload', { icon: 'upload', variant: 'ghost', size: 'sm', onClick: () => { setMode('edit'); drop.input.click(); } }),
      ]),
      h('div', { class: 'workspace' }, panel, stage.el));

    // ------------------------------------------------------------ behaviour
    const current = () => pick(draft.mode);
    const currentEditMode = () => {
      const m = current();
      if (!m || draft.mode !== 'edit') return null;
      const id = draft.editMode[m.id] || (m.defaults && m.defaults.edit_mode) || (m.edit_modes[0] && m.edit_modes[0].id);
      return m.edit_modes.find((e) => e.id === id) || m.edit_modes[0] || null;
    };

    function renderModels() {
      const list = modelsFor(draft.mode);
      replace(modelSelect, ...list.map((m) => h('option', { value: m.id }, m.label)));
      const m = current();
      modelField.hidden = list.length === 0;
      modelSelect.disabled = list.length < 2;
      if (m) modelSelect.value = m.id;
      modelHint.textContent = m ? m.description : '';
    }

    function renderSizes() {
      const m = current();
      const sizes = (m && m.sizes && m.sizes.length) ? m.sizes : (options.image_sizes || []);
      const value = sizes.includes(draft.size) ? draft.size : ((m && m.default_size) || sizes[0]);
      draft.size = value;
      const next = chips(sizes.map((s) => [s, s.replace('x', '×'), aspect(s)]), { value, label: 'Image size', cls: 'chips-size', onChange: (v) => { draft.size = v; } });
      sizeChips.replaceWith(next);
      sizeChips = next;
    }

    function renderEditModes() {
      const m = current();
      const modes = (m && m.edit_modes) || [];
      const active = currentEditMode();
      const next = chips(modes.map((e) => [e.id, e.label]), {
        value: active ? active.id : '', label: 'Edit mode',
        onChange: (id) => { draft.editMode[m.id] = id; syncControls(); },
      });
      editModeChips.replaceWith(next);
      editModeChips = next;
    }

    function setModel(id) {
      const m = byId.get(id);
      if (!m) return;
      draft.model[draft.mode === 'generate' ? 'generate' : 'edit'] = id;
      renderModels();
      renderSizes();
      renderEditModes();
      syncControls();
    }

    // Shows exactly the controls that change what the backend does.
    function syncControls() {
      const m = current();
      const e = currentEditMode();
      const isEdit = draft.mode === 'edit';
      const isGen = draft.mode === 'generate';
      const qwen = m && m.family === 'qwen-image';
      const sdxl = m && m.family === 'sdxl';
      editModeWrap.hidden = !isEdit || !e;
      editModeHint.textContent = e ? e.description : '';
      editQualityField.hidden = !(isEdit && m && (m.qualities || []).length);
      const quality = editQualityChips.getValue();
      maskBox.hidden = !(isEdit && m && m.masks);
      const needsMask = Boolean(isEdit && e && e.requires_mask);
      const transform = Boolean(isEdit && qwen && e && e.id === 'transform');
      maskNote.textContent = needsMask
        ? `${m.label} needs a mask for “${e.label}”: paint or draw the area to change.`
        : (transform ? 'Full transformation changes the whole image, so it ignores the mask. Clear it or pick another mode.'
          : 'Optional. Without a mask the model decides what to change.');
      if (needsMask) maskBox.open = true;
      const strengthOn = draft.mode === 'variation' || Boolean(isEdit && e && e.strength_applies);
      strength.hidden = !strengthOn;
      const hintEl = strength.querySelector('.field-hint');
      if (hintEl) {
        hintEl.textContent = draft.mode === 'variation'
          ? 'Low keeps the scene and changes details; high re-imagines it more freely.'
          : (sdxl ? 'How much of the image (or the masked area) is redrawn: low stays close to the source.'
            : 'How far the transformation may move away from the source layout.');
      }
      genSection.hidden = !isGen;
      qualityField.hidden = !(isGen && m && (m.qualities || []).length);
      uncensored.hidden = !qwen;
      qualityTags.hidden = !sdxl;
      // The negative prompt only matters where real guidance runs (not on 4-step Qwen edits).
      negField.hidden = !(isGen || (isEdit && (sdxl || quality === 'quality')));
      guidanceField.hidden = !isGen && !sdxl;
      steps.max = isGen ? '100' : '50';
      prompt.querySelector('label').textContent = isGen ? 'Prompt' : isEdit ? (sdxl ? 'Describe the result' : 'Edit instruction') : 'Prompt (optional)';
      prompt.textarea.placeholder = isEdit
        ? (sdxl ? 'The same woman on a beach at sunset, photo…' : 'Replace the sky with a stormy sunset…')
        : draft.mode === 'variation' ? 'Optional: nudge the variation, e.g. “autumn colours”' : 'A cozy reading nook in a treehouse, warm light, film grain…';
    }

    function setMode(m) {
      draft.mode = m;
      modeTabs.select(m);
      sourceSection.hidden = m === 'generate';
      renderModels();
      renderSizes();
      renderEditModes();
      syncControls();
      genBtn.querySelector('span').textContent = m === 'generate' ? 'Generate' : m === 'edit' ? 'Apply edit' : 'Create variation';
      formError.hidden = true;
    }

    function setSource(a) {
      draft.source = a;
      replace(sourceBox, a ? sourceChip(a, { label: 'Source', onClear: () => setSource(null) }) : h('p', { class: 'muted small' }, 'No source selected yet.'));
      mask.setSource(a);
    }

    function useSource(a, m) {
      setSource(a);
      setMode(m);
      if (m === 'edit') prompt.set('');
      prompt.textarea.focus();
      toast(m === 'edit' ? 'Pick an edit mode, describe the edit, then press Apply edit.' : 'Adjust strength if you like, then press Create variation.', 'ok', 3000);
    }

    function applySettings(a) {
      const req = (a.settings && a.settings.requested) || {};
      const kind = req.kind || 't2i';
      const slot = kind === 't2i' ? 'generate' : 'edit';
      if (req.image_model && byId.has(req.image_model)) draft.model[slot] = req.image_model;
      if (req.edit_mode && req.image_model) draft.editMode[req.image_model] = req.edit_mode;
      if (req.edit_quality) editQualityChips.setValue(req.edit_quality);
      if (req.size) draft.size = req.size;
      renderModels();
      renderSizes();
      renderEditModes();
      if (req.n) countChips.setValue(String(req.n));
      if (req.quality) qualityChips.setValue(req.quality);
      steps.value = req.steps ?? a.steps ?? '';
      guidance.value = req.guidance ?? a.guidance ?? '';
      if (req.negative_prompt || a.negative_prompt) negative.value = req.negative_prompt || a.negative_prompt;
      if (typeof req.uncensored === 'boolean') uncensored.input.checked = req.uncensored;
      if (typeof req.quality_tags === 'boolean') qualityTags.input.checked = req.quality_tags;
      if (typeof req.strength === 'number') strength.setValue(req.strength);
      syncControls();
    }

    function reprompt(a) {
      const req = (a.settings && a.settings.requested) || {};
      const kind = req.kind || 't2i';
      if (kind === 'edit' || kind === 'variation') {
        if (req.source_id) getAsset(req.source_id).then(setSource).catch(() => setSource(null));
        setMode(kind);
      } else {
        setMode('generate');
      }
      applySettings(a);
      prompt.set(a.prompt || req.prompt || '');
      if (req.mask) toast('That edit used a mask: paint it again before applying.', 'warn', 4000);
      prompt.textarea.focus();
    }

    async function doUpload(file) {
      if (!IMAGE_TYPES.includes(file.type)) { toast('Choose a PNG, JPEG or WebP image.', 'danger'); return; }
      if (file.size > MAX_UPLOAD) { toast('That image is larger than 25 MB.', 'danger'); return; }
      drop.progress(0);
      try {
        const asset = await upload('/api/media/upload', file, { title: file.name.replace(/\.[^.]+$/, ''), onProgress: (f) => drop.progress(f) });
        drop.progress(null);
        emitAsset(asset);
        setSource(asset);
        if (draft.mode === 'generate') setMode('edit');
        stage.select(asset);
        toast('Uploaded. It is now the source image.', 'ok');
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

    async function generate() {
      formError.hidden = true;
      const m = draft.mode;
      const text = prompt.get();
      const kind = m === 'generate' ? 't2i' : m;
      const model = current();
      const e = currentEditMode();
      if (kind !== 'variation' && !text) return fail(m === 'edit' ? 'Describe the edit you want.' : 'Write a prompt first.', prompt.textarea);
      if (kind !== 't2i' && !draft.source) return fail('Choose or upload a source image first.', sourceSection.querySelector('button'));
      const body = { kind, seed: seed.next() };
      if (model) body.image_model = model.id;
      if (model && model.family === 'qwen-image') body.uncensored = uncensored.input.checked;
      if (model && model.family === 'sdxl' && kind === 't2i') body.quality_tags = qualityTags.input.checked;
      if (text) body.prompt = text;
      if (title.value.trim()) body.title = title.value.trim();
      const st = readNumber(steps, { integer: true });
      if (st !== null) body.steps = st;
      if (!negField.hidden && negative.value.trim()) body.negative_prompt = negative.value.trim();
      if (kind === 't2i') {
        body.size = sizeChips.getValue();
        body.n = Number(countChips.getValue());
        if (!qualityField.hidden) body.quality = qualityChips.getValue();
        const g = readNumber(guidance);
        if (g !== null) body.guidance = g;
      } else {
        body.source_id = draft.source.id;
        if (!strength.hidden) body.strength = strength.getValue();
      }
      if (kind === 'edit') {
        if (e) body.edit_mode = e.id;
        if (!editQualityField.hidden) body.edit_quality = editQualityChips.getValue();
        const exported = maskBox.hidden ? null : mask.exportMask();
        if (e && e.requires_mask && !exported) {
          maskBox.open = true;
          return fail(`${model.label} needs a mask for “${e.label}”. Paint or draw the area to change.`, maskBox.querySelector('summary'));
        }
        if (exported && model.family === 'qwen-image' && e && e.id === 'transform') {
          return fail('Full transformation changes the whole image. Clear the mask or pick another mode.', maskBox.querySelector('summary'));
        }
        if (exported) {
          body.mask = exported.dataUrl;
          body.mask_source = exported.source;
          if (exported.rects.length) body.mask_rects = exported.rects;
        }
      }
      genBtn.disabled = true;
      try {
        const job = await submitMedia(body);
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
    setSource(draft.source);

    if (ctx.query.asset) {
      try { const a = await getAsset(ctx.query.asset); if (a.type === 'image') { stage.select(a); setSource(a); } } catch (err) { toast(err.message, 'danger'); }
    }
    if (ctx.query.focus) prompt.textarea.focus();

    return () => {
      Object.assign(draft, {
        prompt: prompt.textarea.value, negative: negative.value, n: Number(countChips.getValue()),
        quality: qualityChips.getValue(), steps: readNumber(steps, { integer: true }), guidance: readNumber(guidance),
        strength: strength.getValue(), title: title.value, uncensored: uncensored.input.checked,
        qualityTags: qualityTags.input.checked, editQuality: editQualityChips.getValue(),
      });
      stage.destroy();
    };
  },
};
