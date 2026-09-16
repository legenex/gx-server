// Same-origin API client. The session lives in an HttpOnly cookie; the CSRF
// token is held in memory only and sent on every state-changing request.

let csrf = null;
const listeners = new Set();

export class ApiError extends Error {
  constructor(status, message, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function onUnauthenticated(fn) { listeners.add(fn); }
export function setCsrf(token) { csrf = token; }

async function parse(res) {
  const type = res.headers.get('Content-Type') || '';
  if (type.includes('application/json')) return res.json();
  return res.text();
}

export async function request(method, path, body, { signal, raw } = {}) {
  const headers = { Accept: 'application/json' };
  const opts = { method, headers, credentials: 'same-origin', signal, cache: 'no-store' };
  if (method !== 'GET') {
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
  if (res.status === 401 && path !== '/api/login') {
    listeners.forEach((fn) => fn());
  }
  if (!res.ok) {
    const msg = data && data.error ? data.error.message : `HTTP ${res.status}`;
    throw new ApiError(res.status, msg, data && data.error ? data.error.code : 'http');
  }
  return data;
}

export const api = {
  get: (path, opts) => request('GET', path, null, opts),
  post: (path, body, opts) => request('POST', path, body, opts),
};

export async function runAction(name, confirm) {
  return api.post(`/api/actions/${encodeURIComponent(name)}`, confirm === undefined ? {} : { confirm });
}

export async function waitJob(id, onUpdate, { interval = 2000, signal } = {}) {
  for (;;) {
    const job = await api.get(`/api/actions/jobs/${id}`, { signal });
    if (onUpdate) onUpdate(job);
    if (job.state !== 'running') return job;
    await new Promise((r) => setTimeout(r, interval));
  }
}
