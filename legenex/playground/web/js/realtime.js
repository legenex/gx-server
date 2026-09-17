// Realtime helpers shared by the Live and Call Agents pages (plt.md sections 1.6 and 2).
// The WebSocket always goes to THIS origin (/rt/<service>/<session>); the
// Playground tunnels it to gx10-02. Microphone and camera need a secure
// context, so pages check secureContextInfo() first and show
// secureContextCallout() when it is not one.
import { getConfig } from './api.js';
import { h } from './dom.js';
import { href } from './nav.js';
import { callout, linkButton } from './ui.js';

const SESSION_RE = /^(call|live)_[0-9a-f]{32}$/;

export function realtimeUrl(service, sessionId, ticket) {
  if (!['call', 'live'].includes(service) || !SESSION_RE.test(sessionId) || !sessionId.startsWith(`${service}_`)) {
    throw new Error('invalid realtime session');
  }
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const query = ticket ? `?ticket=${encodeURIComponent(ticket)}` : '';
  return `${scheme}//${location.host}/rt/${service}/${sessionId}${query}`;
}

export function openRealtime(service, sessionId, { protocols = [], binaryType = 'arraybuffer' } = {}) {
  const ws = new WebSocket(realtimeUrl(service, sessionId), protocols);
  ws.binaryType = binaryType;
  return ws;
}

// What the page needs to know before asking for the microphone or camera.
export async function secureContextInfo() {
  const secure = Boolean(window.isSecureContext);
  const media = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  let tls = null;
  try {
    const cfg = await getConfig();
    tls = cfg && cfg.tls ? cfg.tls : null;
  } catch {
    tls = null;
  }
  const port = tls && tls.enabled ? tls.port : null;
  const httpsUrl = port ? `https://${location.hostname}:${port}${location.pathname}${location.hash}` : null;
  let reason = '';
  if (!secure) {
    reason = 'This page is not a secure context, so the browser blocks the microphone and camera.';
  } else if (!media) {
    reason = 'This browser does not offer microphone or camera access.';
  }
  return { secure, media, ok: secure && media, https: location.protocol === 'https:', httpsUrl, tls, reason };
}

// A callout that explains how to reach the secure (HTTPS) address.
export function secureContextCallout(info) {
  const actions = [];
  if (info.httpsUrl) actions.push(linkButton('Open the secure address', info.httpsUrl, { icon: 'shield', variant: 'primary', size: 'sm' }));
  actions.push(linkButton('HTTPS setup help', href('settings', { focus: 'https' }), { icon: 'settings', size: 'sm', variant: 'ghost' }));
  const text = h('div', { class: 'stack-sm' },
    h('p', {}, info.reason || 'Microphone and camera are not available here.'),
    info.httpsUrl
      ? h('p', {}, 'Use the HTTPS address instead. The first time, install the GX-Playground certificate on this device (Settings explains how).')
      : h('p', {}, 'Open GX-Playground on this computer at http://127.0.0.1:8090, or ask the administrator to enable the HTTPS listener.'));
  return callout('warn', 'Microphone and camera need a secure connection', text, actions);
}
