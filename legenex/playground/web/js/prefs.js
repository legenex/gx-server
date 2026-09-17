// The signed-in user's Playground preferences (Settings > /api/preferences).
// app.js loads them before the first page mounts and keeps a copy on this
// device; pages read defaults with pref(key). A preference the page cannot
// use (for example a size the chosen model does not offer) is ignored there.
import { storeGet, storeSet } from './dom.js';

export function pref(key, fallback = null) {
  const prefs = storeGet('prefs', {}) || {};
  const value = prefs[key];
  return value === undefined || value === null || value === '' ? fallback : value;
}

export function setPrefsCache(prefs) {
  storeSet('prefs', prefs && typeof prefs === 'object' ? prefs : {});
}
