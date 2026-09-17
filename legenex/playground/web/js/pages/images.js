// Images workspace (gx-image): generate, edit, variation, compare, lineage.
import { getAsset, getMediaOptions, upload } from '../api.js';
import { duplicateAsset, emitAsset, pickAsset, sourceChip } from '../assets.js';
import { h, toast, replace } from '../dom.js';
import { friendlyError, submitMedia } from '../jobs.js';
import { navigate } from '../nav.js';
import {
  button, chips, composer, disclosure, dropzone, field, numberInput, pageHeader, readNumber, seedField, slider,
  tabs, textInput, toggle,
} from '../ui.js';
import { createStage } from '../workspace.js';

const session = { results: [], jobs: [], done: new Set(), selectedId: null, compareId: null };
const draft = {
  mode: 'generate', source: null, prompt: '', negative: '', size: '1328x1328', n: 1, quality: 'standard',
  steps: null, guidance: null, strength: 0.6, title: '', uncensored: true,
};

const MODES = [['generate', 'Generate', 'sparkles'], ['edit', 'Edit', 'wand'], ['variation', 'Variation', 'shuffle']];
const QUALITY = [['fast', 'Fast'], ['standard', 'Standard'], ['hd', 'HD']];
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
const MAX_UPLOAD = 25 * 1024 * 1024;

function aspect(size) {
  const [w, hh] = size.split('x').map(Number);
  if (!w || !hh) return '';
  if (w === hh) return 'Square';
  return w > hh ? 'Landscape' : 'Portrait';
}

export default {
  title: 'Images',
  async mount(root, ctx) {
    const options = await getMediaOptions().catch(() => ({ image_sizes: ['1328x1328', '1024x1024'] }));
    const sizes = options.image_sizes || [];
    if (!sizes.includes(draft.size)) draft.size = sizes[0];

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

    const prompt = composer({ label: 'Prompt', placeholder: 'A cozy reading nook in a treehouse, warm light, film grain…', maxLength: 4000, rows: 5, value: draft.prompt, onSubmit: () => generate(), id: 'image-prompt' });
    const sizeChips = chips(sizes.map((s) => [s, s.replace('x', '×'), aspect(s)]), { value: draft.size, label: 'Image size', cls: 'chips-size' });
    const countChips = chips([['1', '1'], ['2', '2'], ['3', '3'], ['4', '4']], { value: String(draft.n), label: 'Number of images' });
    const qualityChips = chips(QUALITY, { value: draft.quality, label: 'Quality' });
    const strength = slider({ label: 'Strength', min: 0.05, max: 1, step: 0.05, value: draft.strength, format: (v) => v.toFixed(2), hint: 'Low keeps the source close; high follows your prompt more freely.' });
    const seed = seedField('image');
    const title = textInput({ value: draft.title, maxLength: 200, placeholder: 'Optional' });
    const uncensored = toggle('Uncensored model', draft.uncensored, { hint: 'Uses the uncensored adapter (the default for this studio).' });
    const negative = h('textarea', { class: 'input', rows: 2, maxlength: 4000, placeholder: 'blurry, text, watermark…' });
    negative.value = draft.negative;
    const steps = numberInput({ min: 1, max: 100, step: 1, placeholder: 'Auto', value: draft.steps });
    const guidance = numberInput({ min: 0, max: 20, step: 0.5, placeholder: 'Auto', value: draft.guidance });
    const negField = field('Negative prompt', negative, { hint: 'What to avoid.' });
    const guidanceField = field('Guidance', guidance, { hint: '0–20. Higher follows the prompt more literally.' });
    const stepsField = field('Steps', steps, { hint: 'Leave empty for the model default.' });
    const advanced = disclosure('Advanced', h('div', { class: 'stack' }, negField, h('div', { class: 'grid-2' }, stepsField, guidanceField)), { ic: 'sliders' });
    const genSection = h('div', { class: 'stack' },
      h('div', { class: 'field' }, h('p', { class: 'field-label', id: 'size-label' }, 'Size'), sizeChips),
      h('div', { class: 'row-between align-start' },
        h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Images'), countChips),
        h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Quality'), qualityChips)));
    const formError = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
    const genBtn = button('Generate', { icon: 'sparkles', variant: 'primary', size: 'lg', attrs: { id: 'generate-btn', class: 'btn btn-primary btn-lg btn-block' }, onClick: () => generate() });

    const panel = h('aside', { class: 'panel', 'aria-label': 'Image settings' },
      modeTabs,
      h('div', { class: 'panel-body' },
        sourceSection, prompt, genSection, strength,
        seed, field('Title', title), uncensored, advanced),
      h('div', { class: 'panel-foot' }, formError, genBtn));

    replace(root,
      pageHeader('Images', 'gx-image · text to image, instruction edits and variations', [
        button('Upload', { icon: 'upload', variant: 'ghost', size: 'sm', onClick: () => { setMode('edit'); drop.input.click(); } }),
      ]),
      h('div', { class: 'workspace' }, panel, stage.el));

    // ------------------------------------------------------------ behaviour
    function setMode(m) {
      draft.mode = m;
      modeTabs.select(m);
      sourceSection.hidden = m === 'generate';
      genSection.hidden = m !== 'generate';
      strength.hidden = m === 'generate';
      negField.hidden = m !== 'generate';
      guidanceField.hidden = m !== 'generate';
      steps.max = m === 'generate' ? '100' : '50';
      prompt.querySelector('label').textContent = m === 'generate' ? 'Prompt' : m === 'edit' ? 'Edit instruction' : 'Prompt (optional)';
      prompt.textarea.placeholder = m === 'edit' ? 'Make the sky stormy and add a red kite…' : m === 'variation' ? 'Optional: nudge the variation, e.g. “autumn colours”' : 'A cozy reading nook in a treehouse, warm light, film grain…';
      genBtn.querySelector('span').textContent = m === 'generate' ? 'Generate' : m === 'edit' ? 'Apply edit' : 'Create variation';
      formError.hidden = true;
    }

    function setSource(a) {
      draft.source = a;
      replace(sourceBox, a ? sourceChip(a, { label: 'Source', onClear: () => setSource(null) }) : h('p', { class: 'muted small' }, 'No source selected yet.'));
    }

    function useSource(a, m) {
      setSource(a);
      setMode(m);
      if (m === 'edit') prompt.set('');
      prompt.textarea.focus();
      toast(m === 'edit' ? 'Describe the edit, then press Apply edit.' : 'Adjust strength if you like, then press Create variation.', 'ok', 3000);
    }

    function applySettings(a) {
      const req = (a.settings && a.settings.requested) || {};
      if (req.size && sizes.includes(req.size)) sizeChips.setValue(req.size);
      if (req.n) countChips.setValue(String(req.n));
      if (req.quality) qualityChips.setValue(req.quality);
      steps.value = req.steps ?? a.steps ?? '';
      guidance.value = req.guidance ?? a.guidance ?? '';
      if (req.negative_prompt || a.negative_prompt) negative.value = req.negative_prompt || a.negative_prompt;
      if (typeof req.uncensored === 'boolean') uncensored.input.checked = req.uncensored;
      if (req.strength) strength.setValue(req.strength);
    }

    function reprompt(a) {
      const req = (a.settings && a.settings.requested) || {};
      const kind = req.kind || 't2i';
      applySettings(a);
      prompt.set(a.prompt || req.prompt || '');
      if (kind === 'edit' || kind === 'variation') {
        if (req.source_id) getAsset(req.source_id).then(setSource).catch(() => setSource(null));
        setMode(kind);
      } else {
        setMode('generate');
      }
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
      if (kind !== 'variation' && !text) return fail(m === 'edit' ? 'Describe the edit you want.' : 'Write a prompt first.', prompt.textarea);
      if (kind !== 't2i' && !draft.source) return fail('Choose or upload a source image first.', sourceSection.querySelector('button'));
      const body = { kind, seed: seed.next(), uncensored: uncensored.input.checked };
      if (text) body.prompt = text;
      if (title.value.trim()) body.title = title.value.trim();
      const st = readNumber(steps, { integer: true });
      if (st !== null) body.steps = st;
      if (kind === 't2i') {
        body.size = sizeChips.getValue();
        body.n = Number(countChips.getValue());
        body.quality = qualityChips.getValue();
        const g = readNumber(guidance);
        if (g !== null) body.guidance = g;
        if (negative.value.trim()) body.negative_prompt = negative.value.trim();
      } else {
        body.source_id = draft.source.id;
        body.strength = strength.getValue();
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
        prompt: prompt.textarea.value, negative: negative.value, size: sizeChips.getValue(), n: Number(countChips.getValue()),
        quality: qualityChips.getValue(), steps: readNumber(steps, { integer: true }), guidance: readNumber(guidance),
        strength: strength.getValue(), title: title.value, uncensored: uncensored.input.checked,
      });
      stage.destroy();
    };
  },
};
