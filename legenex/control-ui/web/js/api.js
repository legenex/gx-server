// Same-origin API client. The session lives in an HttpOnly cookie; the CSRF
// token is held in memory only and sent on every state-changing request.

let csrf = null;
let csrfRefresh = null;
const listeners = new Set();

export class ApiError extends Error {
  constructor(status, message, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function onUnauthenticated(fn) { listeners.add(fn); }
export function setCsrf(token) { csrf = token || null; }
export function getCsrf() { return csrf; }

async function parse(res) {
  const type = res.headers.get('Content-Type') || '';
  if (type.includes('application/json')) return res.json();
  return res.text();
}

async function refreshCsrf() {
  if (csrfRefresh) return csrfRefresh;
  csrfRefresh = (async () => {
    try {
      const res = await fetch('/api/session', {
        method: 'GET', credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
      if (!res.ok) return null;
      const data = await parse(res);
      if (data && data.authenticated && data.csrf) {
        csrf = data.csrf;
        return csrf;
      }
      return null;
    } catch {
      return null;
    } finally {
      csrfRefresh = null;
    }
  })();
  return csrfRefresh;
}

function unauthorized(path) {
  if (path !== '/api/login') listeners.forEach((fn) => fn());
}

export async function request(method, path, body, { signal, raw, _csrfRetry } = {}) {
  const headers = { Accept: 'application/json' };
  const opts = { method, headers, credentials: 'same-origin', signal, cache: 'no-store' };
  if (method !== 'GET') {
    if (!csrf) await refreshCsrf();
    headers['Content-Type'] = 'application/json';
    if (csrf) headers['X-CSRF-Token'] = csrf;
    opts.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError(0, 'The control UI backend is not reachable (restarting or network down).', 'network');
  }
  if (raw) return res;
  const data = await parse(res);
  if (res.status === 401) unauthorized(path);
  if (!res.ok) {
    const err = data && data.error;
    const code = err && err.code ? err.code : 'http';
    if (method !== 'GET' && res.status === 403 && code === 'csrf' && !_csrfRetry) {
      const used = csrf;
      const fresh = await refreshCsrf();
      if (fresh && fresh !== used) {
        return request(method, path, body, { signal, raw, _csrfRetry: true });
      }
    }
    const msg = err && err.message ? err.message : `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, code);
  }
  return data;
}

export const api = {
  get: (path, opts) => request('GET', path, null, opts),
  post: (path, body, opts) => request('POST', path, body, opts),
};

export async function runAction(name, confirm, args = {}) {
  // The server refuses any body key other than `confirm` and the action's
  // declared args, so extra keys surface as a 400 instead of being ignored.
  const body = { ...args };
  if (confirm !== undefined) body.confirm = confirm;
  return api.post(`/api/actions/${encodeURIComponent(name)}`, body);
}

export async function waitJob(id, onUpdate, { interval = 2000, signal } = {}) {
  for (;;) {
    const job = await api.get(`/api/actions/jobs/${id}`, { signal });
    if (onUpdate) onUpdate(job);
    if (job.state !== 'running') return job;
    await new Promise((r) => setTimeout(r, interval));
  }
}

// Live SSE stream (/api/stream): queue, lifecycle and telemetry events every
// ~2 s. SSE may be absent (older backend, buffering proxy) and must be
// treated as a pure ENHANCEMENT: pages keep their normal polling refresh as
// the fallback, so a stream that never opens changes nothing.
export function openStream(handlers = {}) {
  if (typeof EventSource !== 'function') return null;
  let es;
  try {
    es = new EventSource('/api/stream');
  } catch {
    return null;
  }
  for (const kind of ['queue', 'lifecycle', 'telemetry']) {
    es.addEventListener(kind, (ev) => {
      if (!handlers[kind]) return;
      try {
        const parsed = JSON.parse(ev.data);
        handlers[kind](parsed && parsed.data !== undefined ? parsed.data : parsed, parsed);
      } catch { /* one malformed event never breaks the page */ }
    });
  }
  // Any stream failure (auth, absent endpoint, proxy) falls back to polling.
  es.onerror = () => { try { es.close(); } catch { /* already gone */ } };
  return { close: () => { try { es.close(); } catch { /* already gone */ } } };
}

// Raw-body upload. The CSRF token is sent as a header like every other
// state-changing request; the file never touches any third party.
export async function upload(path, file, { title, onProgress, headers = {} } = {}) {
  if (!csrf) await refreshCsrf();
  const send = (token) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
    if (token) xhr.setRequestHeader('X-CSRF-Token', token);
    if (title) xhr.setRequestHeader('X-Title', encodeURIComponent(title).slice(0, 600));
    xhr.upload.onprogress = (ev) => { if (onProgress && ev.lengthComputable) onProgress(ev.loaded / ev.total); };
    xhr.onerror = () => reject(new ApiError(0, 'Upload failed (network).', 'network'));
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 401) unauthorized(path);
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else {
        const e = data && data.error;
        reject(new ApiError(xhr.status, (e && e.message) || `HTTP ${xhr.status}`, (e && e.code) || 'http'));
      }
    };
    xhr.send(file);
  });
  try {
    return await send(csrf);
  } catch (err) {
    if (err instanceof ApiError && err.status === 403 && err.code === 'csrf') {
      const used = csrf;
      const fresh = await refreshCsrf();
      if (fresh && fresh !== used) return send(fresh);
    }
    throw err;
  }
}

// The file manager's streamed upload: the server reads the target directory
// and filename from X-Path / X-Filename headers (both URL-encoded) and
// streams the raw body to disk, capped at 512 MiB server-side.
export async function uploadFile(dir, file, { onProgress } = {}) {
  return upload('/api/files/upload', file, {
    onProgress,
    headers: {
      'X-Path': encodeURIComponent(dir).slice(0, 600),
      'X-Filename': encodeURIComponent(file.name).slice(0, 400),
    },
  });
}
