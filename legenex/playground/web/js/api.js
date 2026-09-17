// Same-origin API client for the Playground proxy. The session is an HttpOnly
// cookie; the CSRF token lives in memory only and is sent on every POST.

let csrf = null;
const unauthListeners = new Set();

export class ApiError extends Error {
  constructor(status, message, code) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export function onUnauthenticated(fn) { unauthListeners.add(fn); }
export function setCsrf(token) { csrf = token || null; }

function unauthorized(path) {
  if (path !== '/api/login') unauthListeners.forEach((fn) => fn());
}

const TIMEOUT_MS = 30_000;

export async function request(method, path, body, { signal, timeout = TIMEOUT_MS } = {}) {
  const headers = { Accept: 'application/json' };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new DOMException('timeout', 'TimeoutError')), timeout);
  if (signal) {
    if (signal.aborted) ctrl.abort(signal.reason);
    else signal.addEventListener('abort', () => ctrl.abort(signal.reason), { once: true });
  }
  const opts = { method, headers, credentials: 'same-origin', cache: 'no-store', signal: ctrl.signal };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (csrf) headers['X-CSRF-Token'] = csrf;
    opts.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    if (signal && signal.aborted) throw err;
    if (ctrl.signal.aborted) throw new ApiError(0, 'The request timed out. Please try again.', 'timeout');
    throw new ApiError(0, 'GX-Playground is not reachable right now. Check your connection and try again.', 'network');
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  const type = res.headers.get('Content-Type') || '';
  if (type.includes('application/json')) {
    try { data = await res.json(); } catch { data = null; }
  }
  if (res.status === 401) unauthorized(path);
  if (!res.ok) {
    const e = data && data.error;
    const msg = e && typeof e === 'object' ? e.message : (typeof e === 'string' ? e : `Request failed (HTTP ${res.status})`);
    throw new ApiError(res.status, msg, e && e.code ? e.code : 'http');
  }
  return data;
}

// Retries idempotent GETs on network errors and 502/503 with backoff.
async function getWithRetry(path, opts = {}) {
  let delay = 400;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await request('GET', path, null, opts);
    } catch (err) {
      const retryable = err instanceof ApiError && (err.status === 0 || err.status === 502 || err.status === 503)
        && err.code !== 'timeout';
      if (!retryable || attempt >= 2 || (opts.signal && opts.signal.aborted)) throw err;
      await new Promise((r) => setTimeout(r, delay));
      delay *= 2;
    }
  }
}

export const api = {
  get: (path, opts) => getWithRetry(path, opts),
  post: (path, body, opts) => request('POST', path, body, opts),
};

export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : '';
}

// Raw-body upload with progress. Same-origin, CSRF header, never a third party.
export function upload(path, file, { title, filename, onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.setRequestHeader('Accept', 'application/json');
    if (csrf) xhr.setRequestHeader('X-CSRF-Token', csrf);
    if (title) xhr.setRequestHeader('X-Title', encodeURIComponent(title).slice(0, 600));
    if (filename) xhr.setRequestHeader('X-Filename', encodeURIComponent(filename).slice(0, 360));
    xhr.upload.onprogress = (ev) => {
      if (onProgress && ev.lengthComputable) onProgress(ev.loaded / ev.total);
    };
    xhr.onerror = () => reject(new ApiError(0, 'The upload failed (network). Please try again.', 'network'));
    xhr.onabort = () => reject(new ApiError(0, 'Upload cancelled.', 'aborted'));
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 401) unauthorized(path);
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else {
        const e = data && data.error;
        reject(new ApiError(xhr.status, (e && e.message) || `Upload failed (HTTP ${xhr.status})`, (e && e.code) || 'http'));
      }
    };
    if (signal) signal.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(file);
  });
}

// ------------------------------------------------------------ shared caches
const cache = new Map();

export function cached(key, loader, ttlMs = 60_000) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < ttlMs) return hit.promise;
  const promise = loader().catch((err) => { cache.delete(key); throw err; });
  cache.set(key, { at: Date.now(), promise });
  return promise;
}

export function invalidate(key) { cache.delete(key); }

export const getMediaOptions = () => cached('media-options', () => api.get('/api/media/options'), 300_000);
export const getMusicModel = () => cached('music-model', () => api.get('/api/music/model'), 60_000);
export const getConfig = () => cached('pg-config', () => api.get('/pg/config'), 600_000);
export const getAsset = (id) => api.get(`/api/media/assets/${encodeURIComponent(id)}`);
export const updateAsset = (id, body) => api.post(`/api/media/assets/${encodeURIComponent(id)}`, body);
export const deleteAssets = (ids) => api.post('/api/media/delete', { ids, confirm: true });
export const searchAssets = (params, opts) => api.get(`/api/media/assets${qs(params)}`, opts);
