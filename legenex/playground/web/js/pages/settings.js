// Settings: Playground preferences (saved per user on the server), the
// secure-connection (HTTPS) helper with the local CA download, and how to get
// API access. API keys are never shown here; they live in the Control Center.
import { api, getConfig, getMediaOptions } from '../api.js';
import { h, replace, toast, uid } from '../dom.js';
import { href } from '../nav.js';
import { setPrefsCache } from '../prefs.js';
import { secureContextInfo } from '../realtime.js';
import { button, callout, card, disclosure, field, kv, linkButton, pageHeader, select, skeletonLines } from '../ui.js';

const THEMES = [['dark', 'Dark'], ['light', 'Light'], ['system', 'Match this device']];
const MOTION = [['system', 'Match this device'], ['reduce', 'Reduce motion']];
const DENSITY = [['comfortable', 'Comfortable'], ['compact', 'Compact']];
const DURATIONS = [['', 'Let the model decide'], ['30', '30 seconds'], ['60', '1 minute'], ['90', '1.5 minutes'],
  ['120', '2 minutes'], ['180', '3 minutes'], ['240', '4 minutes']];

const INSTALL_STEPS = [
  ['Windows', ['Download the certificate below and open it.', 'Choose Install Certificate, then Current User.',
    'Choose "Place all certificates in the following store", then Browse and pick Trusted Root Certification Authorities.',
    'Finish, confirm the security warning, and restart the browser.']],
  ['macOS', ['Download the certificate and open it. Keychain Access adds it to your login keychain.',
    'Double-click "GX-Playground local CA", open Trust, and set "When using this certificate" to Always Trust.',
    'Close the window and confirm with your password.']],
  ['iPhone and iPad', ['Open this page in Safari and tap Download certificate, then Allow.',
    'Open Settings > General > VPN & Device Management and install the downloaded profile.',
    'Open Settings > General > About > Certificate Trust Settings and turn on full trust for "GX-Playground local CA".']],
  ['Android', ['Download the certificate.',
    'Open Settings > Security & privacy > More security settings > Encryption & credentials > Install a certificate > CA certificate.',
    'Pick the downloaded file. Chrome then trusts the HTTPS address.']],
  ['Linux (Chrome or Chromium)', ['Download the certificate.', 'Open chrome://settings/certificates, then Authorities > Import.',
    'Pick the file and tick "Trust this certificate for identifying websites".']],
  ['Firefox (any desktop)', ['Download the certificate.', 'Open Settings > Privacy & Security > Certificates > View Certificates > Authorities > Import.',
    'Pick the file and tick "Trust this CA to identify websites".']],
];

function radioGroup(legend, name, options, value, onChange) {
  const set = h('fieldset', { class: 'radio-group' }, h('legend', { class: 'field-label' }, legend));
  for (const [v, label] of options) {
    const id = uid(name);
    const input = h('input', { type: 'radio', name, id, value: v, checked: v === value });
    input.addEventListener('change', () => { if (input.checked) onChange(v); });
    set.append(h('label', { class: 'radio', for: id }, input, h('span', {}, label)));
  }
  return set;
}

function httpsSection(info, cfg) {
  const tls = (cfg && cfg.tls) || {};
  const status = info.secure
    ? callout('ok', 'This page is a secure context', info.https
      ? 'You are on the HTTPS address. Microphone and camera can be used on the Live and Call Agents pages.'
      : 'This address counts as secure in your browser (for example 127.0.0.1). Microphone and camera can be used.')
    : callout('warn', 'This page is not a secure context', 'Browsers block the microphone and camera on plain http:// addresses other than localhost. Use the HTTPS address below.');
  const parts = [status];
  if (tls.enabled) {
    const httpsUrl = `https://${location.hostname}:${tls.port}/`;
    parts.push(kv([
      ['HTTPS address', h('a', { href: httpsUrl }, httpsUrl)],
      ['Certificate names', (tls.names || []).join(', ')],
      ['Valid until', tls.server_not_after ? new Date(tls.server_not_after * 1000).toLocaleDateString() : null],
      ['CA fingerprint (SHA-256)', h('code', { class: 'code-inline code-wrap', id: 'ca-fingerprint' }, tls.ca_fingerprint_sha256 || '')],
    ]));
    parts.push(h('p', {}, 'The HTTPS address uses a certificate from a private certificate authority (CA) created on gx10-01. Trust that CA once on each device you use. It can only vouch for gx10-01 and its Tailscale names and addresses, never for other websites.'));
    parts.push(h('div', { class: 'row-wrap' },
      linkButton('Download certificate', '/pg/ca.crt', { icon: 'download', variant: 'primary', download: 'gx-playground-ca.crt', attrs: { id: 'ca-download' } }),
      info.https ? null : linkButton('Open the HTTPS address', httpsUrl, { icon: 'shield' })));
    parts.push(h('p', { class: 'muted small' }, 'When your device shows the certificate, check that its SHA-256 fingerprint matches the one above before you trust it.'));
    parts.push(h('div', { class: 'stack-sm' }, INSTALL_STEPS.map(([os, steps]) => disclosure(os, h('ol', { class: 'steps' }, steps.map((s) => h('li', {}, s))), { ic: 'shield' }))));
    parts.push(h('p', { class: 'muted small' }, 'Optional: an administrator can enable HTTPS certificates in the Tailscale admin console (DNS > HTTPS Certificates). Browsers then trust gx10-01.taila7ef6a.ts.net without installing anything. That is a change in the tailnet settings, not on this cluster.'));
  } else {
    parts.push(callout('info', 'The HTTPS listener is not enabled on this host', 'Ask the administrator to run legenex/playground/scripts/tls-setup.sh on gx10-01. Until then, open GX-Playground on gx10-01 itself at http://127.0.0.1:8090 to use the microphone or camera.'));
  }
  const micResult = h('p', { class: 'small', role: 'status' });
  const micBtn = button('Test microphone access', { icon: 'mic', size: 'sm', onClick: async () => {
    micBtn.disabled = true;
    micResult.textContent = 'Asking the browser…';
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      for (const t of stream.getTracks()) t.stop();
      micResult.textContent = 'The microphone works here. Nothing was recorded.';
    } catch (err) {
      micResult.textContent = err && err.name === 'NotAllowedError'
        ? 'Access was blocked. Allow the microphone for this site in the browser settings and try again.'
        : `The microphone is not available: ${err && err.message ? err.message : 'unknown error'}.`;
    } finally {
      micBtn.disabled = false;
    }
  } });
  if (info.ok) parts.push(h('div', { class: 'row-wrap' }, micBtn, micResult));
  return card('Secure connection (HTTPS)', h('div', { class: 'stack' }, parts), { cls: 'settings-https', sub: 'Needed for the microphone and camera' });
}

function apiSection(cfg) {
  const origin = location.origin;
  const control = cfg && cfg.control_center_url ? new URL(cfg.control_center_url) : null;
  if (control) control.hostname = location.hostname;
  const keysUrl = control ? `${control.origin}/#/keys` : null;
  const wsOrigin = origin.replace(/^http/, 'ws');
  return card('API access', h('div', { class: 'stack' },
    h('p', {}, 'Apps and scripts use a gateway API key. Create one in the Control Center under API Keys, choose which models it may use (for example gx-voice, gx-call or gx-live), and copy it right away: it is shown only once. Keys are never shown in GX-Playground.'),
    kv([
      ['Chat, images, video, speech', cfg && cfg.gateway_url ? h('code', { class: 'code-inline' }, cfg.gateway_url) : null],
      ['Music API', h('code', { class: 'code-inline' }, `${origin}/v1/music`)],
      ['Voice, call and live APIs', h('code', { class: 'code-inline' }, `${origin}/v1/voice · /v1/call · /v1/live`)],
      ['Realtime WebSocket', h('code', { class: 'code-inline' }, `${wsOrigin}/rt/{call|live}/{session}?ticket=…`)],
    ]),
    h('p', { class: 'muted small' }, 'Send the key as "Authorization: Bearer <key>". A realtime session answers with a one-time ticket that is valid for 60 seconds.'),
    h('div', { class: 'row-wrap' },
      keysUrl ? linkButton('Create an API key', keysUrl, { icon: 'external', external: true, attrs: { 'aria-label': 'Create an API key in the Control Center (opens in a new tab)' } }) : null,
      control ? linkButton('API documentation', `${control.origin}/#/docs/04-api`, { icon: 'external', variant: 'ghost', external: true, attrs: { 'aria-label': 'API documentation in the Control Center (opens in a new tab)' } }) : null)),
  { sub: 'Keys are created and revoked in the Control Center' });
}

export default {
  title: 'Settings',
  async mount(root, ctx) {
    replace(root, pageHeader('Settings', 'Your GX-Playground preferences, the secure connection for the microphone and camera, and API access.'), skeletonLines(6));
    const [prefsRes, cfg, options, info] = await Promise.all([
      api.get('/api/preferences').catch((err) => ({ error: err })),
      getConfig().catch(() => null),
      getMediaOptions().catch(() => ({})),
      secureContextInfo(),
    ]);
    if (prefsRes.error) {
      replace(root, pageHeader('Settings'), callout('danger', 'Your settings could not be loaded', prefsRes.error.message));
      return undefined;
    }
    const saved = { ...(prefsRes.preferences || {}) };
    const draft = { ...saved };
    const status = h('p', { class: 'small', role: 'status', id: 'settings-status' });

    const appearance = card('Appearance', h('div', { class: 'stack' },
      radioGroup('Theme', 'pref-theme', THEMES, draft.theme || document.documentElement.dataset.theme || 'dark', (v) => { draft.theme = v; }),
      radioGroup('Motion', 'pref-motion', MOTION, draft.reduced_motion || 'system', (v) => { draft.reduced_motion = v; }),
      radioGroup('Density', 'pref-density', DENSITY, draft.density || 'comfortable', (v) => { draft.density = v; })),
    { sub: 'Applies on every device you sign in on' });

    const genModels = (options.image_models || []).filter((m) => (m.operations || []).includes('generate'));
    const sizeOpts = (list) => [['', 'Model default'], ...(list || []).map((s) => [s, s.replace('x', ' × ')])];
    const imageModel = select([['', 'Default model'], ...genModels.map((m) => [m.id, m.label])], draft.default_image_model || '',
      { onChange: (v) => { draft.default_image_model = v || null; } });
    const imageSize = select(sizeOpts(options.image_sizes), draft.default_image_size || '', { onChange: (v) => { draft.default_image_size = v || null; } });
    const videoSize = select(sizeOpts(options.video_sizes), draft.default_video_size || '', { onChange: (v) => { draft.default_video_size = v || null; } });
    const musicLen = select(DURATIONS, draft.default_music_duration || '', { onChange: (v) => { draft.default_music_duration = v || null; } });
    const defaults = card('Creation defaults', h('div', { class: 'grid-2' },
      genModels.length ? field('Image model', imageModel, { hint: 'Used for new generations on the Images page' }) : null,
      field('Image size', imageSize, { hint: 'Ignored when the chosen model does not offer it' }),
      field('Video size', videoSize),
      field('Song length', musicLen, { hint: 'Music page; you can still change it per song' })),
    { sub: 'Starting values on the create pages' });

    const saveBtn = button('Save settings', { icon: 'check', variant: 'primary', attrs: { id: 'settings-save' }, onClick: async () => {
      const changed = {};
      for (const key of new Set([...Object.keys(saved), ...Object.keys(draft)])) {
        const before = saved[key] ?? null;
        const after = draft[key] ?? null;
        if (before !== after) changed[key] = after;
      }
      if (!Object.keys(changed).length) {
        status.textContent = 'Nothing changed.';
        return;
      }
      saveBtn.disabled = true;
      status.textContent = 'Saving…';
      try {
        const res = await api.post('/api/preferences', { preferences: changed });
        Object.keys(saved).forEach((k) => delete saved[k]);
        Object.assign(saved, res.preferences || {});
        setPrefsCache(saved);
        window.dispatchEvent(new CustomEvent('gx-preferences', { detail: { ...saved } }));
        status.textContent = 'Settings saved.';
        toast('Settings saved', 'ok');
      } catch (err) {
        status.textContent = `Not saved: ${err.message}`;
      } finally {
        saveBtn.disabled = false;
      }
    } });

    const https = httpsSection(info, cfg);
    https.id = 'https';
    replace(root,
      pageHeader('Settings', 'Your GX-Playground preferences, the secure connection for the microphone and camera, and API access.'),
      h('div', { class: 'settings-grid' },
        h('div', { class: 'stack' }, appearance, defaults, h('div', { class: 'row-wrap settings-actions' }, saveBtn, status)),
        h('div', { class: 'stack' }, https, apiSection(cfg),
          card('More', h('div', { class: 'stack-sm' },
            h('p', {}, 'Models, activity and your jobs:'),
            h('div', { class: 'row-wrap' },
              linkButton('Models', href('models'), { icon: 'cpu', size: 'sm', variant: 'ghost' }),
              linkButton('Logs', href('logs'), { icon: 'logs', size: 'sm', variant: 'ghost' }),
              linkButton('History', href('history'), { icon: 'history', size: 'sm', variant: 'ghost' }))),
          { level: 2 }))));
    if (ctx.query.focus === 'https') {
      const heading = https.querySelector('h2');
      if (heading) {
        heading.setAttribute('tabindex', '-1');
        heading.focus();
        https.scrollIntoView({ block: 'start' });
      }
    }
    return undefined;
  },
};
