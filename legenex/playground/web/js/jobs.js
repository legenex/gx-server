// Job vocabulary, polling and the job progress card shared by every page.
import { api } from './api.js';
import { h, clear, elapsed, toast, replace } from './dom.js';
import { icon } from './icons.js';
import { musicBodyFromRequest } from './music-recipe.js';
import { button, progressBar } from './ui.js';

export const PHASE = {
  QUEUED: { label: 'QUEUED', tone: 'neutral' },
  WAITING: { label: 'WAITING FOR RESOURCE', tone: 'warn' },
  LOADING: { label: 'LOADING MODEL', tone: 'info' },
  PREPARING: { label: 'PREPARING', tone: 'info' },
  GENERATING: { label: 'GENERATING', tone: 'accent' },
  PROCESSING: { label: 'PROCESSING', tone: 'accent' },
  SAVING: { label: 'SAVING', tone: 'info' },
  COMPLETE: { label: 'COMPLETE', tone: 'ok' },
  FAILED: { label: 'FAILED', tone: 'danger' },
  CANCELLED: { label: 'CANCELLED', tone: 'neutral' },
};

const MEDIA_PHASE = { queued: 'QUEUED', waiting: 'WAITING', generating: 'GENERATING', saving: 'SAVING', ready: 'COMPLETE', failed: 'FAILED', cancelled: 'CANCELLED' };
const MUSIC_PHASE = {
  queued: 'QUEUED', waiting_for_resource: 'WAITING', loading_model: 'LOADING', preparing: 'PREPARING',
  generating: 'GENERATING', processing: 'PROCESSING', saving: 'SAVING', completed: 'COMPLETE',
  failed: 'FAILED', cancelled: 'CANCELLED',
};
const TERMINAL = new Set(['COMPLETE', 'FAILED', 'CANCELLED']);

export const isMusic = (job) => typeof job.id === 'string' && job.id.startsWith('mus-');
// Build V3 VOI: gx-voice jobs share the music status vocabulary.
export const isVoice = (job) => typeof job.id === 'string' && job.id.startsWith('vj_');
const VOICE_TITLE = { tts: 'Speech', voice_design: 'Voice design', voice_clone: 'Voice clone', dialogue: 'Dialogue' };

export function phaseOf(job) {
  let key;
  if (isVoice(job)) {
    key = MUSIC_PHASE[job.status] || 'QUEUED';
  } else if (isMusic(job)) {
    key = MUSIC_PHASE[job.phase] || MUSIC_PHASE[job.status] || 'QUEUED';
    // A render that finished on gx10-02 is only COMPLETE once it is in the Library.
    if (job.status === 'completed' && job.phase === 'saving') key = 'SAVING';
  } else {
    key = MEDIA_PHASE[job.phase] || 'QUEUED';
  }
  const p = PHASE[key];
  let label = p.label;
  if (!isMusic(job) && !isVoice(job) && key === 'GENERATING' && job.cold_start) label = 'LOADING MODEL · GENERATING';
  return { key, label, tone: p.tone, terminal: TERMINAL.has(key) };
}

export const isTerminal = (job) => phaseOf(job).terminal;

export function jobKind(job) {
  if (isVoice(job)) return 'voice';
  if (isMusic(job)) return 'music';
  return job.alias === 'gx-video' ? 'video' : 'image';
}

export function jobTitle(job) {
  if (isVoice(job)) return `${VOICE_TITLE[job.operation] || 'Voice'}${job.title ? ` · ${job.title}` : ''}`;
  if (isMusic(job)) {
    const op = { generate: 'Create music', remix: 'Remix', edit: 'Repaint', extend: 'Extend' }[job.operation] || 'Music';
    return `${op}${job.title ? ` · ${job.title}` : ''}`;
  }
  return job.label || job.kind;
}

export function jobPrompt(job) {
  if (isVoice(job)) {
    const r = job.request || {};
    return r.text || r.description || (r.segments || []).map((s) => s.text).join(' / ');
  }
  if (isMusic(job)) return (job.request && job.request.prompt) || '';
  return job.prompt || '';
}

export function jobStarted(job) {
  return isMusic(job) || isVoice(job) ? (job.started_at || job.created_at) : (job.started || job.created);
}

export function jobElapsed(job) {
  if (isVoice(job)) return (job.finished_at || Date.now() / 1000) - job.created_at;
  if (isMusic(job)) {
    if (job.finished_at && job.created_at) return job.finished_at - job.created_at;
    return job.elapsed_s;
  }
  return job.elapsed_seconds;
}

export function canCancel(job) {
  const p = phaseOf(job);
  if (p.terminal) return false;
  if (isVoice(job)) return job.status !== 'saving';
  if (isMusic(job)) return job.status !== 'completed';
  return job.phase === 'queued' || job.phase === 'waiting';
}

export function jobProgress(job) {
  return (isMusic(job) || isVoice(job)) && typeof job.progress === 'number' && !phaseOf(job).terminal ? job.progress : null;
}

export function jobError(job) {
  if (isVoice(job)) return job.error ? job.error.message || String(job.error.code) : null;
  if (isMusic(job)) return job.error ? job.error.message || String(job.error) : (job.import_error || null);
  return job.error || null;
}

// Friendly wording for backend errors. Short safe messages are shown as-is;
// internal exception text is replaced (the original stays under "Details").
export function friendlyError(message) {
  const raw = String(message || '').trim();
  if (!raw) return { text: 'Something went wrong. Please try again.', detail: null };
  const rules = [
    [/insufficient_memory|not enough memory|out of memory|OOM/i, 'The GPU node ran out of memory for this job. Try a smaller size or fewer items, or switch the resource profile.'],
    [/not reachable|node_unavailable|connection refused|timed out|timeout/i, 'The generation server is not reachable right now. Please try again in a moment.'],
    [/HTTP 5\d\d|bad_gateway|upstream/i, 'The generation server reported a problem. Please try again.'],
    [/^[A-Z][A-Za-z]+(Error|Exception)\b|Traceback|File "/, 'Something went wrong on the generation server. Please try again.'],
    [/not a valid PNG|no images|not a video|no real motion/i, 'The generator returned an unusable result. Please try again, perhaps with a different seed.'],
    [/cancel/i, 'This job was cancelled.'],
  ];
  for (const [rx, text] of rules) {
    if (rx.test(raw)) return { text, detail: raw.length > 400 ? `${raw.slice(0, 400)}…` : raw };
  }
  if (raw.length > 220 || /\n/.test(raw)) return { text: 'The job failed. Please try again.', detail: raw.slice(0, 400) };
  return { text: raw.charAt(0).toUpperCase() + raw.slice(1), detail: null };
}

// ------------------------------------------------------------ submissions
// The exact bodies we submitted, so Retry can resend the same recipe.
const submitted = new Map();

export async function submitMedia(body) {
  const job = await api.post('/api/media/jobs', body);
  submitted.set(job.id, { type: 'media', body });
  center.track(job);
  return job;
}

export async function submitVideo(body) {
  const job = await api.post('/api/video/generate', body);
  submitted.set(job.id, { type: 'video', body });
  center.track(job);
  return job;
}

export async function submitMusic(body) {
  const job = await api.post('/api/music/jobs', body);
  submitted.set(job.id, { type: 'music', body });
  center.track(job);
  return job;
}

export async function submitVoice(body) {
  const job = await api.post('/api/voice/jobs', body);
  submitted.set(job.id, { type: 'voice', body });
  center.track(job);
  return job;
}

export function recipeOf(job) {
  const known = submitted.get(job.id);
  if (known) return known;
  if (isVoice(job)) {
    // A clone is never re-run from history: it needs a fresh permission confirmation.
    return job.operation === 'voice_clone' ? null : { type: 'voice', body: { ...(job.request || {}) } };
  }
  if (isMusic(job)) {
    const req = job.request || {};
    const body = musicBodyFromRequest(req, job.operation || req.operation);
    if (job.parent_asset_id) body.source_asset_id = job.parent_asset_id;
    if (job.reference_asset_id) body.reference_asset_id = job.reference_asset_id;
    if (job.title) body.title = job.title;
    return { type: 'music', body };
  }
  const p = { ...(job.params || {}) };
  // Wan text-to-video with LoRAs (Build V3 WAN): resend the validated video request.
  if (p.wan && p.wan.request) return { type: 'video', body: { ...p.wan.request } };
  if (job.prompt) p.prompt = job.prompt;
  // A masked edit keeps only the mask's metadata; it cannot be re-sent without the mask itself.
  if (p.mask && typeof p.mask === 'object') return { type: 'media', body: p, needsMask: true };
  return { type: 'media', body: p };
}

export async function retryJob(job) {
  const recipe = recipeOf(job);
  if (!recipe) throw new Error('Start a voice clone again from the Voice page (it needs your permission confirmation).');
  const { type, body, needsMask } = recipe;
  if (type === 'voice') {
    const nextVoice = await submitVoice(body);
    toast('Submitted again with the same recipe.', 'ok');
    return nextVoice;
  }
  if (needsMask) throw new Error('This edit used a mask. Open it in Images and paint the mask again.');
  const next = type === 'music' ? await submitMusic(body) : type === 'video' ? await submitVideo(body) : await submitMedia(body);
  toast('Submitted again with the same recipe.', 'ok');
  return next;
}

export async function cancelJob(job) {
  const path = isVoice(job) ? `/api/voice/jobs/${job.id}/cancel`
    : isMusic(job) ? `/api/music/jobs/${job.id}/cancel` : `/api/media/jobs/${job.id}/cancel`;
  const next = await api.post(path, {});
  center.update(next);
  return next;
}

// ------------------------------------------------------------ job center
// Tracks active jobs, polls each every 2 s while the tab is visible, and
// notifies subscribers. Terminal jobs stop polling.
class JobCenter {
  constructor() {
    this.jobs = new Map();
    this.subs = new Set();
    this.timer = null;
    this.listTimer = null;
    this.busy = false;
    this.enabled = false;
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && this.enabled) { this.pollNow(); this.refreshLists(); }
    });
  }

  start() {
    this.enabled = true;
    this.refreshLists();
    this.schedule();
  }

  stop() {
    this.enabled = false;
    clearTimeout(this.timer);
    clearTimeout(this.listTimer);
    this.jobs.clear();
    this.emit(null);
  }

  subscribe(fn) {
    this.subs.add(fn);
    return () => this.subs.delete(fn);
  }

  emit(job) {
    for (const fn of [...this.subs]) {
      try { fn(job); } catch (err) { console.warn('job subscriber failed', err); }
    }
  }

  track(job) {
    this.update(job);
    this.schedule(200);
  }

  update(job) {
    if (!job || !job.id) return;
    const prev = this.jobs.get(job.id);
    this.jobs.set(job.id, job);
    const was = prev ? phaseOf(prev).key : null;
    const now = phaseOf(job).key;
    if (prev && was !== now) {
      if (now === 'COMPLETE') toast(`${jobTitle(job)} is ready.`, 'ok');
      else if (now === 'FAILED') toast(`${jobTitle(job)} failed.`, 'danger');
    }
    this.emit(job);
  }

  active() {
    return [...this.jobs.values()].filter((j) => !isTerminal(j));
  }

  all() {
    return [...this.jobs.values()];
  }

  schedule(ms = 2000) {
    clearTimeout(this.timer);
    if (!this.enabled) return;
    this.timer = setTimeout(() => this.pollNow(), ms);
  }

  async pollNow() {
    if (!this.enabled || this.busy) return;
    if (document.hidden) { this.schedule(4000); return; }
    const active = this.active();
    this.busy = true;
    try {
      await Promise.all(active.map(async (j) => {
        const path = isVoice(j) ? `/api/voice/jobs/${j.id}` : isMusic(j) ? `/api/music/jobs/${j.id}` : `/api/media/jobs/${j.id}`;
        try {
          this.update(await api.get(path));
        } catch (err) {
          if (err.status === 404) this.update({ ...j, phase: 'failed', status: 'failed', error: isMusic(j) || isVoice(j) ? { message: 'This job no longer exists.' } : 'This job no longer exists.' });
        }
      }));
    } finally {
      this.busy = false;
    }
    if (this.active().length) this.schedule(2000);
    else this.schedule(8000);
  }

  // Full lists (History page and the activity tray): every 8 s while visible.
  async refreshLists() {
    clearTimeout(this.listTimer);
    if (!this.enabled) return null;
    let result = null;
    if (!document.hidden) {
      const [media, music, voice] = await Promise.allSettled([
        api.get('/api/media/jobs'), api.get('/api/music/jobs?limit=100'), api.get('/api/voice/jobs?limit=100'),
      ]);
      const mediaJobs = media.status === 'fulfilled' ? media.value.jobs || [] : [];
      const musicJobs = music.status === 'fulfilled' ? music.value.jobs || [] : [];
      const voiceJobs = voice.status === 'fulfilled' ? voice.value.jobs || [] : [];
      for (const j of [...mediaJobs, ...musicJobs, ...voiceJobs]) {
        const prev = this.jobs.get(j.id);
        if (!prev || JSON.stringify(prev) !== JSON.stringify(j)) this.update(j);
      }
      result = {
        jobs: [...mediaJobs, ...musicJobs, ...voiceJobs],
        musicError: music.status === 'rejected' ? music.reason.message : null,
        voiceError: voice.status === 'rejected' ? voice.reason.message : null,
        mediaError: media.status === 'rejected' ? media.reason.message : null,
      };
      if (this.active().length) this.schedule(1000);
    }
    this.listTimer = setTimeout(() => this.refreshLists(), 8000);
    return result;
  }
}

export const center = new JobCenter();

// ------------------------------------------------------------ elapsed ticker
const tickers = new Set();
setInterval(() => {
  for (const el of tickers) {
    if (!el.isConnected) { tickers.delete(el); continue; }
    const base = Number(el.dataset.base || 0);
    const since = Number(el.dataset.since || 0);
    el.textContent = elapsed(base + (since ? (Date.now() - since) / 1000 : 0));
  }
}, 1000);

function elapsedEl(job) {
  const el = h('span', { class: 'job-elapsed', 'aria-label': 'Elapsed time' });
  const secs = jobElapsed(job) || 0;
  el.textContent = elapsed(secs);
  if (!isTerminal(job)) {
    el.dataset.base = String(secs);
    el.dataset.since = String(Date.now());
    tickers.add(el);
  }
  return el;
}

export function phaseBadge(job) {
  const p = phaseOf(job);
  return h('span', { class: `phase-badge phase-${p.tone}${p.terminal ? '' : ' is-live'}`, dataset: { phase: p.key } },
    h('span', { class: 'phase-dot', 'aria-hidden': 'true' }), p.label);
}

function waitingInfo(job) {
  const w = job.waiting;
  const p = phaseOf(job);
  if (!w || p.key !== 'WAITING') return null;
  const gib = (x) => `${Number(x).toFixed(0)} GiB`;
  return h('div', { class: 'job-why' },
    icon('info', { size: 16 }),
    h('div', {},
      h('p', { class: 'job-why-title' }, 'Why waiting: ', w.reason || 'waiting for GPU capacity'),
      w.detail ? h('p', { class: 'job-why-text' }, w.detail) : null,
      (w.need_gib !== undefined && w.need_gib !== null && w.available_gib !== undefined && w.available_gib !== null)
        ? h('p', { class: 'job-why-text' }, `Needs ${gib(w.need_gib)} · ${gib(w.available_gib)} available`) : null,
      w.next ? h('p', { class: 'job-why-text' }, 'Next: ', w.next) : null));
}

// A self-updating job card. opts: { onOpen(job), compact, showPrompt }
export function jobCard(job, opts = {}) {
  const root = h('article', { class: `job-card${opts.compact ? ' job-card-compact' : ''}`, dataset: { job: job.id } });
  let current = job;
  const render = () => {
    const j = current;
    const p = phaseOf(j);
    const err = p.key === 'FAILED' ? (j.error_hint ? { text: j.error_hint, detail: jobError(j) } : friendlyError(jobError(j))) : null;
    const detail = isMusic(j) ? (j.phase_detail || j.detail) : j.detail;
    const actions = [];
    if (canCancel(j)) {
      actions.push(button('Cancel', { icon: 'x', size: 'sm', variant: 'ghost', onClick: async (ev) => {
        ev.currentTarget.disabled = true;
        try { current = await cancelJob(j); render(); toast('Cancel requested.', 'ok'); } catch (e) { toast(e.message, 'danger'); render(); }
      } }));
    }
    if (p.key === 'FAILED' || p.key === 'CANCELLED') {
      actions.push(button('Retry', { icon: 'refresh', size: 'sm', variant: 'secondary', onClick: async (ev) => {
        ev.currentTarget.disabled = true;
        try { const next = await retryJob(j); if (opts.onRetry) opts.onRetry(next); } catch (e) { toast(friendlyError(e.message).text, 'danger'); ev.currentTarget.disabled = false; }
      } }));
    }
    if (p.key === 'COMPLETE' && opts.onOpen) {
      actions.push(button('Open result', { icon: 'chevronRight', size: 'sm', variant: 'secondary', onClick: () => opts.onOpen(j) }));
    }
    const kind = jobKind(j);
    replace(root,
      h('div', { class: 'job-head' },
        h('span', { class: `job-kind job-kind-${kind}`, 'aria-hidden': 'true' }, icon({ music: 'music', video: 'video', voice: 'mic' }[kind] || 'image', { size: 16 })),
        h('div', { class: 'job-meta' },
          h('p', { class: 'job-title' }, jobTitle(j)),
          opts.showPrompt !== false && jobPrompt(j) ? h('p', { class: 'job-prompt' }, jobPrompt(j)) : null),
        h('div', { class: 'job-side' }, phaseBadge(j), elapsedEl(j))),
      p.terminal ? null : progressBar(jobProgress(j), `${jobTitle(j)} progress`),
      !p.terminal && detail && p.key !== 'WAITING' ? h('p', { class: 'job-detail' }, detail) : null,
      !p.terminal && j.queue_position ? h('p', { class: 'job-detail' }, `Position ${j.queue_position} in the queue`) : null,
      waitingInfo(j),
      err ? h('div', { class: 'callout callout-danger job-error', role: 'alert' },
        icon('alert', { size: 16, cls: 'callout-ic' }),
        h('div', { class: 'callout-body' }, h('p', { class: 'callout-title' }, err.text),
          err.detail ? h('details', { class: 'err-detail' }, h('summary', {}, 'Details'), h('p', {}, err.detail)) : null)) : null,
      actions.length ? h('div', { class: 'job-actions' }, actions) : null);
  };
  render();
  const unsub = center.subscribe((u) => {
    if (!root.isConnected && root.dataset.mounted) { unsub(); return; }
    if (u && u.id === current.id && u !== current) {
      const before = phaseOf(current).key;
      current = u;
      render();
      if (opts.onChange && before !== phaseOf(u).key) opts.onChange(u);
    }
  });
  requestAnimationFrame(() => { root.dataset.mounted = '1'; });
  root.update = (u) => { current = u; render(); };
  return root;
}
