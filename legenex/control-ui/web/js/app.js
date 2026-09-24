import { api, ApiError, onUnauthenticated, setCsrf } from './api.js';
import { clear, h, levelBadge, toast, errorBox, spinner } from './dom.js';
import dashboard from './pages/dashboard.js';
import models from './pages/models.js';
import runtime from './pages/runtime.js';
import cluster from './pages/cluster.js';
import jobs from './pages/jobs.js';
import logs from './pages/logs.js';
import playground from './pages/playground.js';
import docs from './pages/docs.js';
import settings from './pages/settings.js';
import creative from './pages/creative.js';
import manager from './pages/manager.js';
import keys from './pages/keys.js';
import resources from './pages/resources.js';
import storage from './pages/storage.js';
import setup from './pages/setup.js';
import backup from './pages/backup.js';

const PAGES = {
  dashboard, models, resources, storage, manager, keys, setup, connections: setup, runtime, cluster, jobs, logs, playground, docs,
  settings, backup, create: creative, library: creative,
};
const $ = (id) => document.getElementById(id);

const state = {
  user: null,
  page: null,
  pageName: '',
  timer: null,
  paused: false,
  lastRefresh: 0,
  inflight: null,
};

// ------------------------------------------------------------------ theme
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('gxui-theme', theme); } catch { /* private mode */ }
}
function initTheme() {
  let theme = 'dark';
  try { theme = localStorage.getItem('gxui-theme') || 'dark'; } catch { /* ignore */ }
  applyTheme(theme);
  $('theme-btn').addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
}

// ------------------------------------------------------------------ auth
function showLogin(message) {
  stopPolling();
  state.user = null;
  $('app-view').hidden = true;
  $('login-view').hidden = false;
  const err = $('login-error');
  err.hidden = !message;
  err.textContent = message || '';
  setTimeout(() => $('login-pass').focus(), 0);
}

function showApp() {
  $('login-view').hidden = true;
  $('app-view').hidden = false;
  $('user-chip').textContent = state.user;
  route();
}

async function checkSession() {
  try {
    const s = await api.get('/api/session');
    if (s.authenticated) {
      state.user = s.user;
      setCsrf(s.csrf);
      showApp();
    } else {
      showLogin(s.configured ? '' : 'No admin password is configured yet. Run gx-ui-passwd on gx10-01.');
    }
  } catch (err) {
    showLogin(err.message);
  }
}

function initLogin() {
  $('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = $('login-submit');
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    try {
      const res = await api.post('/api/login', {
        username: $('login-user').value.trim(),
        password: $('login-pass').value,
      });
      $('login-pass').value = '';
      state.user = res.user;
      setCsrf(res.csrf);
      if (!location.hash || location.hash === '#/') location.hash = '#/dashboard';
      showApp();
    } catch (err) {
      showLogin(err instanceof ApiError ? err.message : 'Sign-in failed');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  });
  $('logout-btn').addEventListener('click', async () => {
    try { await api.post('/api/logout', {}); } catch { /* already gone */ }
    setCsrf(null);
    showLogin('Signed out.');
  });
  onUnauthenticated(() => {
    if (state.user) showLogin('Your session expired. Please sign in again.');
  });
}

// ---------------------------------------------------------------- polling
function stopPolling() {
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
}

async function refresh() {
  stopPolling();
  const page = state.page;
  if (!page || !page.refresh) return;
  if (!document.hidden && !state.paused) {
    const ctrl = new AbortController();
    state.inflight = ctrl;
    try {
      await page.refresh({ signal: ctrl.signal });
      state.lastRefresh = Date.now();
      $('refresh-note').textContent = `updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      if (err.name !== 'AbortError') $('refresh-note').textContent = `refresh failed: ${err.message}`;
    }
  }
  if (state.page === page && page.interval) {
    state.timer = setTimeout(refresh, page.interval * 1000);
  }
}

async function updateOverall() {
  try {
    const ov = await api.get('/api/overview');
    const box = clear($('overall-status'));
    const gx = ov.gxmax || {};
    box.append(levelBadge(ov.overall, `Cluster: ${{ ok: 'healthy', warn: 'degraded', crit: 'critical' }[ov.overall] || 'unknown'}`));
    if (gx.state && gx.state !== 'down') {
      box.append(h('span', { class: 'badge badge-warn' }, `gx-max ${gx.state}${gx.phase ? ` · ${gx.phase}` : ''}`));
    }
    return ov;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ route
function route() {
  if (!state.user) return;
  const hash = location.hash.replace(/^#\/?/, '');
  const [rawName, ...rest] = hash.split('/');
  const name = rawName.split('?')[0];
  const pageName = PAGES[name] ? name : 'dashboard';
  if (!PAGES[name]) {
    history.replaceState(null, '', '#/dashboard');
  }
  stopPolling();
  if (state.inflight) state.inflight.abort();
  if (state.page && state.page.unmount) state.page.unmount();

  for (const a of document.querySelectorAll('#sidenav a')) {
    if (a.dataset.page === pageName) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  document.body.classList.remove('nav-open');
  $('nav-toggle').setAttribute('aria-expanded', 'false');

  const firstRoute = !state.pageName;
  const main = clear($('main'));
  const page = PAGES[pageName];
  state.page = page;
  state.pageName = pageName;
  document.title = `${page.title} · GX Cluster Control`;
  main.append(h('h1', { class: 'page-title' }, page.title));
  const root = h('div', { class: 'page', id: `page-${pageName}` });
  main.append(root);
  root.append(spinner());
  Promise.resolve(page.mount(root, { params: rest, ctx: pageContext() }))
    .then(() => refresh())
    .catch((err) => { clear(root).append(errorBox(err)); });
  // Move focus to the new content on navigation (screen readers announce it),
  // but not on first load, so the skip link stays the first Tab stop.
  if (!firstRoute) main.focus({ preventScroll: true });
}

function pageContext() {
  return {
    user: state.user,
    refreshNow: () => { refresh(); updateOverall(); },
    updateOverall,
  };
}

function initShell() {
  window.addEventListener('hashchange', () => {
    // Only '#/page' hashes are routes; anything else is an in-page anchor.
    if (location.hash.startsWith('#/') || !location.hash) route();
  });
  document.querySelector('.skip-link').addEventListener('click', (ev) => {
    ev.preventDefault();
    const target = document.getElementById('main');
    target.focus();
    target.scrollIntoView({ block: 'start' });
  });
  // Close the mobile drawer on every nav click, including a click on the
  // page that is already open (which fires no hashchange).
  $('sidenav').addEventListener('click', (ev) => {
    if (!ev.target.closest('a')) return;
    document.body.classList.remove('nav-open');
    $('nav-toggle').setAttribute('aria-expanded', 'false');
  });
  $('nav-toggle').addEventListener('click', () => {
    const open = !document.body.classList.contains('nav-open');
    document.body.classList.toggle('nav-open', open);
    $('nav-toggle').setAttribute('aria-expanded', String(open));
  });
  $('pause-btn').addEventListener('click', () => {
    state.paused = !state.paused;
    $('pause-btn').setAttribute('aria-pressed', String(state.paused));
    $('pause-btn').classList.toggle('active', state.paused);
    toast(state.paused ? 'Auto-refresh paused' : 'Auto-refresh resumed', 'ok', 2000);
    if (!state.paused) refresh();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.user && Date.now() - state.lastRefresh > 4000) refresh();
  });
  // Top bar status: every 10 s while the app is visible.
  const tick = async () => {
    if (state.user && !document.hidden && !state.paused) await updateOverall();
    setTimeout(tick, 10000);
  };
  setTimeout(tick, 300);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && document.body.classList.contains('nav-open')) {
      document.body.classList.remove('nav-open');
      $('nav-toggle').setAttribute('aria-expanded', 'false');
    }
  });
}

initTheme();
initLogin();
initShell();
checkSession();
