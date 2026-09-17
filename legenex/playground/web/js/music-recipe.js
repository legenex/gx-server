// A stored gx-music request (the node-2 job view's `request`) -> a body the
// Music API accepts again. The node-2 view keeps ACE-Step's own parameter
// names (key_scale, audio_duration, use_format, ...); they are mapped back to
// the public names and engine-only switches are dropped, so Reuse, Variation
// and Retry never send fields the API refuses. No imports: jobs.js and
// assets.js both use it.

const ENGINE_TO_API = {
  vocal_language: 'vocal_language', inference_steps: 'inference_steps', infer_method: 'infer_method',
  thinking: 'thinking', use_format: 'enhance_prompt', use_cot_caption: 'lm_caption_rewrite',
  lm_temperature: 'lm_temperature', lm_cfg_scale: 'lm_cfg_scale', lm_top_p: 'lm_top_p', batch_size: 'batch_size',
  bpm: 'bpm', key_scale: 'key', time_signature: 'time_signature', audio_duration: 'duration',
  guidance_scale: 'guidance_scale',
  // already public (older views and the offline fixture)
  duration: 'duration', key: 'key', enhance_prompt: 'enhance_prompt', lm_caption_rewrite: 'lm_caption_rewrite',
};
const OPERATION_PARAMS = {
  remix: { audio_cover_strength: 'strength', cover_noise_strength: 'noise_strength', strength: 'strength', noise_strength: 'noise_strength' },
  edit: { repainting_start: 'start', repainting_end: 'end', repaint_mode: 'mode', repaint_strength: 'strength', repaint_wav_crossfade_sec: 'crossfade', start: 'start', end: 'end', mode: 'mode', strength: 'strength' },
  extend: { repaint_mode: 'mode', repaint_strength: 'strength', repaint_wav_crossfade_sec: 'crossfade' },
};
const TOP_LEVEL = ['prompt', 'lyrics', 'description', 'vocal_intent', 'lyrics_source', 'output_format'];

export function musicBodyFromRequest(req, operation) {
  const op = operation || req.operation || 'generate';
  const body = { operation: op };
  const params = req.parameters || {};
  const map = { ...ENGINE_TO_API, ...(OPERATION_PARAMS[op] || {}) };
  for (const [k, v] of Object.entries(params)) {
    const name = map[k];
    if (!name || v === undefined || v === null || v === '') continue;
    body[name] = v;
  }
  if (typeof body.seed === 'undefined' && params.seed !== undefined && params.seed !== null) {
    const first = Number(String(params.seed).split(',')[0]);
    if (Number.isInteger(first) && first >= 0) body.seed = first;
  }
  for (const k of TOP_LEVEL) if (req[k] !== undefined && req[k] !== null && req[k] !== '') body[k] = req[k];
  if (Array.isArray(req.style_tags) && req.style_tags.length) body.style_tags = [...req.style_tags];
  if (typeof req.instrumental === 'boolean') body.instrumental = req.instrumental;
  if (body.instrumental) {
    delete body.lyrics;
    delete body.vocal_intent;
    delete body.vocal_language;
  }
  if (body.vocal_intent === 'auto') delete body.vocal_intent;
  if (op === 'extend' && req.extend) {
    body.seconds = req.extend.seconds;
    body.direction = req.extend.direction;
  }
  if (op === 'generate') {
    for (const k of ['start', 'end', 'mode', 'strength', 'noise_strength', 'crossfade']) delete body[k];
  }
  return body;
}
