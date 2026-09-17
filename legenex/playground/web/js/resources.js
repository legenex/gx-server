// Resource summary wording and the profile switch flow (plan -> confirm -> apply).
import { api } from './api.js';
import { confirmDialog, h, toast } from './dom.js';

// status -> [tone, friendly words]
export const STATUS = {
  Ready: ['ok', 'Ready'],
  Idle: ['idle', 'Idle'],
  Loading: ['busy', 'Warming up'],
  Starting: ['busy', 'Starting'],
  Working: ['accent', 'Creating'],
  Running: ['accent', 'Running'],
  Waiting: ['warn', 'Waiting for memory'],
  Unloading: ['warn', 'Making room'],
  Releasing: ['warn', 'Releasing'],
  Paused: ['warn', 'Paused'],
  Unavailable: ['bad', 'Unavailable'],
};
const RANK = { bad: 5, warn: 4, busy: 3, accent: 2, ok: 1, idle: 0 };

export function statusInfo(status) {
  const [tone, words] = STATUS[status] || ['idle', status || 'Unknown'];
  return { tone, words };
}

export function worstStatus(rows) {
  let worst = null;
  for (const r of rows || []) {
    const info = statusInfo(r.status);
    if (!worst || RANK[info.tone] > RANK[worst.tone]) worst = { ...info, row: r };
  }
  return worst || { tone: 'idle', words: 'Unknown' };
}

export const ALLOWED_PROFILES = ['auto', 'text', 'media', 'music', 'max'];

let summaryCache = { at: 0, data: null };
export async function getSummary(force = false) {
  if (!force && summaryCache.data && Date.now() - summaryCache.at < 5000) return summaryCache.data;
  const data = await api.get('/api/resources/summary');
  summaryCache = { at: Date.now(), data };
  window.dispatchEvent(new CustomEvent('gx-resources', { detail: data }));
  return data;
}

function aliasList(title, items) {
  if (!items || !items.length) return null;
  return h('div', { class: 'plan-group' },
    h('p', { class: 'plan-label' }, title),
    h('ul', { class: 'plan-list' }, items.map((x) => h('li', {}, typeof x === 'string' ? x : (x.alias || x.label || x.id || JSON.stringify(x))))));
}

// Returns true when the profile was applied.
export async function switchProfile(target) {
  if (!ALLOWED_PROFILES.includes(target)) return false;
  let plan;
  try {
    plan = await api.get(`/api/resources/profile/plan?to=${encodeURIComponent(target)}`);
  } catch (err) {
    toast(err.message, 'danger');
    return false;
  }
  const drains = (plan.drains_now || []).length + (plan.may_drain || []).length;
  let confirm;
  if (plan.needs_confirm || drains || (plan.conflicts || []).length || (plan.active || []).length) {
    const queued = Object.entries(plan.queued || {}).filter(([, n]) => n);
    const details = h('div', { class: 'plan' },
      aliasList('Stays loaded', plan.stays),
      aliasList('Unloads now', plan.drains_now),
      aliasList('May unload when needed', plan.may_drain),
      aliasList('Running now (finishes first)', plan.active),
      queued.length ? aliasList('Queued', queued.map(([k, n]) => `${k}: ${n}`)) : null,
      aliasList('Conflicts', plan.conflicts));
    const result = await confirmDialog({
      title: `Switch to ${plan.label}?`,
      message: plan.summary || '',
      details,
      okLabel: `Switch to ${plan.label}`,
      danger: Boolean(plan.needs_confirm),
      phrase: plan.needs_confirm && plan.confirm_phrase ? plan.confirm_phrase : null,
    });
    if (!result) return false;
    confirm = plan.needs_confirm ? (plan.confirm_phrase ? result : true) : undefined;
  }
  try {
    const body = { profile: target };
    if (confirm !== undefined) body.confirm = confirm;
    await api.post('/api/resources/profile', body);
    toast(`Profile set to ${plan.label}.`, 'ok');
    await getSummary(true);
    return true;
  } catch (err) {
    toast(err.message, 'danger');
    return false;
  }
}
