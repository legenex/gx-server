// GX-Playground shell: session, theme, top bar, activity tray, router.
import { api, ApiError, getConfig, onUnauthenticated, setCsrf } from './api.js';
import { openCommandBar } from './command.js';
import { byId, clear, h, storeGet, storeSet, toast, replace } from './dom.js';
import { hydrateIcons, icon } from './icons.js';
import { center } from './jobs.js';
import { navigate, parseHash } from './nav.js';
import { getSummary, statusInfo, worstStatus } from './resources.js';
import { callout, loading } from './ui.js';
import dashboard from './pages/dashboard.js';
import images from './pages/images.js';
import video from './pages/video.js';
import music from './pages/music.js';
import library from './pages/library.js';
import historyPage from './pages/history.js';

const $ = byId;
const PAGES = { dashboard, images, video, music, library, history: historyPage };

const state = { user: null, cleanup: null, route: '', firstRoute: true, resTimer: null };

// ------------------------------------------------------------------ theme
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = $('theme-btn');
  const next = theme === 'dark' ? 'light' : 'dark';
  btn.setAttribute('aria-label', `Switch to ${next} theme`);
  btn.title = `Switch to ${next} theme`;
  replace(btn, icon(theme === 'dark' ? 'sun' : 'moon', { size: 18 }));
  window.dispatchEvent(new CustomEvent('gx-theme', { detail: theme }));
}

function initTheme() {
  const saved = storeGet('theme', null);
  applyTheme(saved === 'light' || saved === 'dark' ? saved : 'dark');
  $('theme-btn').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    storeSet('theme', next);
    applyTheme(next);
  });
}

// ------------------------------------------------------------------ auth
function showLogin(message, tone = 'danger') {
  stopApp();
  $('boot').hidden = true;
  $('app-view').hidden = true;
  $('login-view').hidden = false;
  document.title = 'Sign in · GX-Playground';
  const err = $('login-error');
  err.hidden = !message;
  err.textContent = message || '';
  err.className = `form-error form-${tone}`;
  const user = $('login-user');
  setTimeout(() => (user.value ? $('login-pass') : user).focus(), 0);
}

function showApp() {
  $('boot').hidden = true;
  $('login-view').hidden = true;
  $('app-view').hidden = false;
  $('user-name').textContent = state.user;
  center.start();
  refreshResources();
  getConfig().then((cfg) => {
    const link = $('cc-link');
    if (cfg && /^https?:\/\//.test(cfg.control_center_url || '')) {
      // Same host as this page (only the port differs), so the host-scoped
      // session cookie carries over and one sign-in covers both apps.
      const url = new URL(cfg.control_center_url);
      url.hostname = location.hostname;
      link.href = url.href;
      link.hidden = false;
    }
    $('app-version').textContent = cfg && cfg.version ? `v${cfg.version}` : '';
  }).catch(() => {});
  route();
}

function stopApp() {
  state.user = null;
  setCsrf(null);
  center.stop();
  clearTimeout(state.resTimer);
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } }
  state.cleanup = null;
  state.route = '';
  clear($('main'));
}

async function checkSession() {
  try {
    const s = await api.get('/api/session');
    if (s.authenticated) {
      state.user = s.user;
      setCsrf(s.csrf);
      showApp();
    } else {
      showLogin(s.configured === false ? 'No password is configured yet. Ask the administrator to run gx-ui-passwd on gx10-01.' : '');
    }
  } catch (err) {
    showLogin(err.message);
  }
}

function initLogin() {
  $('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const user = $('login-user');
    const pass = $('login-pass');
    const err = $('login-error');
    if (!user.value.trim() || !pass.value) {
      err.hidden = false;
      err.className = 'form-error form-danger';
      err.textContent = 'Enter your username and password.';
      (user.value.trim() ? pass : user).focus();
      return;
    }
    const btn = $('login-submit');
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    try {
      const res = await api.post('/api/login', { username: user.value.trim(), password: pass.value });
      pass.value = '';
      state.user = res.user;
      setCsrf(res.csrf);
      if (!location.hash || location.hash === '#/') history.replaceState(null, '', '#/dashboard');
      showApp();
    } catch (e) {
      pass.value = '';
      showLogin(e instanceof ApiError ? e.message : 'Sign-in failed. Please try again.');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sign in';
    }
  });
  $('logout-btn').addEventListener('click', async () => {
    try { await api.post('/api/logout', {}); } catch { /* already signed out */ }
    showLogin('You are signed out.', 'ok');
  });
  onUnauthenticated(() => {
    if (state.user) showLogin('Your session expired. Please sign in again.');
  });
}

// --------------------------------------------------------------- top bar
async function refreshResources() {
  clearTimeout(state.resTimer);
  if (!state.user) return;
  if (!document.hidden) {
    try { await getSummary(true); } catch { /* the pill keeps its last state */ }
  }
  state.resTimer = setTimeout(refreshResources, 15000);
}

function renderPill(summary) {
  const pill = $('res-pill');
  const worst = worstStatus((summary.rows || []).filter((r) => r.key !== 'max' || r.status !== 'Idle'));
  const label = summary.maintenance ? 'Maintenance' : summary.profile_label;
  const tone = summary.maintenance ? 'warn' : worst.tone;
  replace(pill, h('span', { class: `dot dot-${tone}`, 'aria-hidden': 'true' }),
    h('span', { class: 'res-text' }, h('span', { class: 'res-profile' }, label), h('span', { class: 'res-state' }, worst.words)));
  pill.setAttribute('aria-label', `Resources: profile ${label}, ${worst.words}${worst.row ? ` (${worst.row.label})` : ''}`);
  pill.title = (summary.rows || []).map((r) => `${r.label}: ${statusInfo(r.status).words}`).join(' · ');
}

function renderTray() {
  const n = center.active().length;
  const count = $('tray-count');
  const badge = $('rail-badge');
  count.hidden = !n;
  badge.hidden = !n;
  count.textContent = String(n);
  badge.textContent = String(n);
  $('tray-btn').setAttribute('aria-label', n ? `Activity: ${n} active job${n === 1 ? '' : 's'}. Open History` : 'Activity: no active jobs. Open History');
  $('tray-btn').classList.toggle('is-busy', n > 0);
}

// ------------------------------------------------------------------ route
function route() {
  if (!state.user) return;
  const { page, rest, query } = parseHash();
  const name = PAGES[page] ? page : 'dashboard';
  if (!PAGES[page]) {
    history.replaceState(null, '', '#/dashboard');
  }
  const key = `${location.hash}#${Date.now()}`;
  state.route = key;
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } }
  state.cleanup = null;
  for (const a of document.querySelectorAll('.rail-link')) {
    if (a.dataset.page === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  const mod = PAGES[name];
  document.title = `${mod.title} · GX-Playground`;
  const main = $('main');
  const root = h('div', { class: `page page-${name}`, id: `page-${name}` }, loading());
  replace(main, root);
  document.body.dataset.page = name;
  const wasFirst = state.firstRoute;
  state.firstRoute = false;
  Promise.resolve()
    .then(() => mod.mount(root, { rest, query, user: state.user }))
    .then((cleanup) => {
      if (typeof cleanup === 'function') {
        if (state.route === key) state.cleanup = cleanup; else cleanup();
      }
      if (!wasFirst) {
        const heading = root.querySelector('h1');
        if (heading && !query.focus) heading.focus({ preventScroll: true });
      }
    })
    .catch((err) => {
      if (err && err.status === 401) return;
      replace(root, callout('danger', 'This page could not be loaded', err.message || String(err)));
    });
  window.scrollTo({ top: 0 });
}

function initShell() {
  window.addEventListener('hashchange', () => {
    if (location.hash.startsWith('#/') || !location.hash) route();
  });
  document.querySelector('.skip-link').addEventListener('click', (ev) => {
    ev.preventDefault();
    const target = $('app-view').hidden ? $('login-main') : $('main');
    target.setAttribute('tabindex', '-1');
    target.focus();
  });
  $('tray-btn').addEventListener('click', () => navigate('history'));
  $('cmd-btn').addEventListener('click', openCommandBar);
  document.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k' && state.user) {
      ev.preventDefault();
      openCommandBar();
    }
  });
  window.addEventListener('gx-resources', (ev) => renderPill(ev.detail));
  center.subscribe(renderTray);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.user) refreshResources();
  });
  hydrateIcons();
  if (/Mac|iPhone|iPad/.test(navigator.platform || '')) {
    for (const k of document.querySelectorAll('.cmd-kbd')) k.textContent = '⌘ K';
  }
  window.addEventListener('unhandledrejection', (ev) => {
    const err = ev.reason;
    if (err && err.name === 'ApiError' && err.status !== 401) {
      toast(err.message, 'danger');
      ev.preventDefault();
    }
  });
}

initTheme();
initLogin();
initShell();
checkSession();
