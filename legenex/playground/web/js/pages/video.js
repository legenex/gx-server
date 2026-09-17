// Video workspace (gx-video): text to video, image to video, video edit.
import { getAsset, getMediaOptions, upload } from '../api.js';
import { duplicateAsset, emitAsset, pickAsset, sourceChip } from '../assets.js';
import { h, toast, replace } from '../dom.js';
import { friendlyError, submitMedia } from '../jobs.js';
import { pref } from '../prefs.js';
import { button, chips, composer, dropzone, field, pageHeader, seedField, slider, tabs, textInput, callout } from '../ui.js';
import { createStage } from '../workspace.js';

const session = { results: [], jobs: [], done: new Set(), selectedId: null, compareId: null };
// size starts from Settings (the page falls back to the first offered size)
const draft = { mode: 't2v', sources: { i2v: null, v2v: null }, prompt: '', size: pref('default_video_size', '640x640'), seconds: 3, fps: 16, strength: 0.85, title: '' };

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
    const options = await getMediaOptions().catch(() => ({ video_sizes: ['640x640'] }));
    const sizes = options.video_sizes || [];
    if (!sizes.includes(draft.size)) draft.size = sizes[0];
    if (ctx.query.mode && MODES.some(([m]) => m === ctx.query.mode)) draft.mode = ctx.query.mode;

    const stage = createStage({
      type: 'video', session,
      emptyTitle: 'Your videos appear here',
      emptyText: 'Describe a shot on the left and press Generate. Clips render on gx10-02 and can take a few minutes.',
      actions: (a) => [
        button('Edit again', { icon: 'wand', size: 'sm', attrs: { 'data-action': 'edit-again' }, onClick: () => { setSource('v2v', a); setMode('v2v'); prompt.textarea.focus(); } }),
        a.settings && a.settings.requested ? button('Variation', { icon: 'shuffle', size: 'sm', attrs: { 'data-action': 'variation' }, onClick: async () => { const j = await duplicateAsset(a, { newSeed: true }); if (j) stage.trackJob(j); } }) : null,
        button('Re-prompt', { icon: 'refresh', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'reprompt' }, onClick: () => reprompt(a) }),
        a.settings && a.settings.requested ? button('Run again', { icon: 'copy', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'duplicate' }, onClick: async () => { const j = await duplicateAsset(a); if (j) stage.trackJob(j); } }) : null,
      ],
    });

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
    const sizeChips = chips(sizes.map((s) => [s, s.replace('x', '×')]), { value: draft.size, label: 'Video size', cls: 'chips-size' });
    const seconds = slider({ label: 'Length', min: 0.5, max: 10, step: 0.5, value: draft.seconds, format: (v) => `${v.toFixed(1)} s` });
    const fps = slider({ label: 'Frame rate', min: 8, max: 24, step: 1, value: draft.fps, format: (v) => `${v} fps` });
    const strengthHint = h('p', { class: 'field-hint strength-explain', 'aria-live': 'polite' });
    const strength = slider({ label: 'Edit strength', min: 0.05, max: 1, step: 0.05, value: draft.strength, format: (v) => v.toFixed(2), onInput: (v) => { strengthHint.textContent = strengthText(v); } });
    strengthHint.textContent = strengthText(draft.strength);
    const strengthBox = h('div', { class: 'stack-sm' }, strength, strengthHint);
    const seed = seedField('video');
    const title = textInput({ value: draft.title, maxLength: 200, placeholder: 'Optional' });
    const formError = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
    const genBtn = button('Generate video', { icon: 'sparkles', variant: 'primary', attrs: { id: 'generate-btn', class: 'btn btn-primary btn-lg btn-block' }, onClick: () => generate() });

    const panel = h('aside', { class: 'panel', 'aria-label': 'Video settings' },
      modeTabs,
      h('div', { class: 'panel-body' },
        sourceSection, prompt,
        h('div', { class: 'field' }, h('p', { class: 'field-label' }, 'Size'), sizeChips),
        seconds, fps, strengthBox, seed, field('Title', title),
        callout('info', null, 'Video renders take a few minutes. You can leave this page; progress continues in Activity.')),
      h('div', { class: 'panel-foot' }, formError, genBtn));

    replace(root,
      pageHeader('Video', 'gx-video · text to video, image to video and video edits'),
      h('div', { class: 'workspace' }, panel, stage.el));

    function setMode(m) {
      draft.mode = m;
      modeTabs.select(m);
      sourceSection.hidden = m === 't2v';
      fps.hidden = m === 'v2v';
      strengthBox.hidden = m !== 'v2v';
      sourceLabel.textContent = m === 'i2v' ? 'Start image' : 'Video to edit';
      const types = m === 'i2v' ? IMAGE_TYPES : VIDEO_TYPES;
      drop.input.accept = types.join(',');
      drop.querySelector('.dropzone-title').textContent = m === 'i2v' ? 'Upload an image' : 'Upload a video';
      drop.querySelector('.dropzone-hint').textContent = m === 'i2v' ? 'PNG, JPEG or WebP · up to 25 MB' : 'MP4, WebM or MOV · up to 150 MB';
      prompt.querySelector('label').textContent = m === 'v2v' ? 'Edit instruction' : 'Prompt';
      renderSource();
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

    async function reprompt(a) {
      const req = (a.settings && a.settings.requested) || {};
      const kind = MODES.some(([m]) => m === req.kind) ? req.kind : 't2v';
      prompt.set(a.prompt || req.prompt || '');
      if (req.size && sizes.includes(req.size)) sizeChips.setValue(req.size);
      if (req.seconds) seconds.setValue(req.seconds);
      if (req.fps) fps.setValue(req.fps);
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

    async function generate() {
      formError.hidden = true;
      const kind = draft.mode;
      const text = prompt.get();
      if (!text) return fail(kind === 'v2v' ? 'Describe the edit you want.' : 'Write a prompt first.', prompt.textarea);
      const src = draft.sources[kind];
      if (kind !== 't2v' && !src) return fail(kind === 'i2v' ? 'Choose or upload a start image first.' : 'Choose or upload a video first.', pickBtn);
      const body = { kind, prompt: text, size: sizeChips.getValue(), seconds: seconds.getValue(), seed: seed.next() };
      if (kind !== 'v2v') body.fps = fps.getValue();
      if (kind === 'v2v') body.strength = strength.getValue();
      if (src) body.source_id = src.id;
      if (title.value.trim()) body.title = title.value.trim();
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
    if (ctx.query.focus || ctx.query.source) prompt.textarea.focus();

    return () => {
      Object.assign(draft, {
        prompt: prompt.textarea.value, size: sizeChips.getValue(), seconds: seconds.getValue(), fps: fps.getValue(),
        strength: strength.getValue(), title: title.value,
      });
      stage.destroy();
    };
  },
};
