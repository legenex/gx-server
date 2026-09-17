// SETUP (D-037): Kilo Code, Open WebUI and OpenAI-compatible clients.
// Live values come from the server; the connection test uses a key the user
// pastes, which is sent once to this server and never stored or logged.
import { api } from '../api.js';
import {
  h, clear, card, kv, toast, errorBox, copyButton, codeBlock, table,
} from '../dom.js';

let root;
let info = null;
let tab = 'kilo';

const TABS = [['kilo', 'Kilo Code'], ['openwebui', 'Open WebUI'], ['generic', 'OpenAI-compatible clients']];

function copyRow(label, value, id) {
  return [label, h('span', { class: 'copy-row' }, h('code', { id }, value), copyButton(() => value, 'Copy'))];
}

function aliasChips(list) {
  return h('div', { class: 'checks' }, list.map((a) => h('span', { class: 'chip' }, h('code', {}, a), copyButton(() => a, 'Copy'))));
}

function keyWorkflow(client) {
  return card('API key',
    h('p', {}, 'Use a key created in this Control Center. The gateway master key is never shown and must not be used in clients.'),
    h('div', { class: 'btn-row' },
      h('a', { class: 'btn btn-primary', href: `#/keys/${client}`, id: `create-key-${client}` }, 'Create API key'),
      h('a', { class: 'btn', href: '#/keys' }, 'Select an existing key')),
    h('p', { class: 'small muted' }, 'A key\'s secret is shown once when it is created. If you no longer have it, use Replace on the API Keys page: the new key keeps the same aliases and expiry.'));
}

function testCard(client) {
  const input = h('input', { type: 'password', id: `test-key-${client}`, autocomplete: 'off', spellcheck: 'false',
    placeholder: 'sk-…', 'aria-describedby': `test-hint-${client}` });
  const out = h('div', { class: 'test-result', 'aria-live': 'polite', id: `test-result-${client}` });
  const btn = h('button', { type: 'button', class: 'btn btn-primary', id: `test-${client}` }, 'Test connection');
  btn.addEventListener('click', async () => {
    const secret = input.value.trim();
    if (!secret) { input.focus(); toast('Paste the key first.', 'warn'); return; }
    btn.disabled = true;
    clear(out).append(h('p', { class: 'loading' }, h('span', { class: 'spin', 'aria-hidden': 'true' }),
      client === 'kilo' ? 'Listing models, sending a Kilo-shaped request to gx-auto and reading the routing decision…'
        : 'Listing models and sending a short request…'));
    try {
      const r = await api.post('/api/setup/test', { client, secret });
      clear(out).append(
        h('p', {}, h('span', { class: `badge badge-${r.connected ? 'ok' : 'crit'}`, id: `test-verdict-${client}` },
          r.connected ? 'CONNECTED' : 'FAILED'), ' ', r.connected ? '' : r.summary),
        table(['Check', 'Result', 'Detail'], r.checks.map((c) => [c.check,
          h('span', { class: `badge badge-${c.ok ? 'ok' : 'crit'}` }, c.ok ? 'PASS' : 'FAIL'),
          `${c.status ? `HTTP ${c.status} · ` : ''}${c.detail || ''}${c.ms !== undefined ? ` · ${c.ms} ms` : ''}`]),
        { caption: 'Connection test' }));
    } catch (e) {
      clear(out).append(errorBox(e));
    } finally {
      btn.disabled = false;
      input.value = '';
    }
  });
  return card('Test connection',
    h('label', { for: `test-key-${client}` }, 'Gateway key to test'),
    h('div', { class: 'btn-row' }, input, btn),
    h('p', { class: 'small muted', id: `test-hint-${client}` }, 'The key is sent to this server for the test only. It is not stored, logged or shown again.'),
    out);
}

function kiloPanel() {
  const k = info.kilo;
  const version = k.extension
    ? `Kilo Code ${k.extension} installed on gx10-01${k.cli ? ` (CLI ${k.cli})` : ''}; steps verified against ${k.verified_against}${k.matches === false ? ' — your version differs, labels may have moved' : ''}.`
    : `Steps verified against Kilo Code ${k.verified_against || k.verified_version}.`;
  return [
    card('Kilo Code',
      h('p', { class: 'lead' }, 'Recommended model: ', h('strong', {}, 'gx-auto'), '. It picks the right tier for every Kilo request:'),
      h('ul', {}, info.gx_auto.routes.map((r) => h('li', {}, `${r.when} → `, h('code', {}, r.to)))),
      h('p', {}, info.gx_auto.gx_max),
      h('p', { class: 'small muted' }, version)),
    card('Connection settings',
      kv([
        ['Provider API', h('strong', {}, k.provider_api)],
        copyRow('Base URL', info.gateway_url, 'kilo-base-url'),
        ['API key', h('span', {}, 'a key from ', h('a', { href: '#/keys' }, 'API Keys'), ' (never the master key)')],
        copyRow('Model', 'gx-auto', 'kilo-model'),
      ]),
      h('p', {}, 'Explicit aliases, if you want to skip routing:'),
      aliasChips(['gx-mini', 'gx-fast', 'gx-reason', 'gx-max']),
      h('p', { class: 'small muted' }, 'gx-image, gx-video and gx-music are not chat models; Kilo Code cannot use them. Use GX-Playground.')),
    keyWorkflow('kilo'),
    card('Manual setup (Kilo Code settings)',
      h('ol', { class: 'steps' }, k.manual_steps.map((s) => h('li', {}, s)))),
    card('Config file',
      h('p', {}, 'Kilo Code reads ', h('code', {}, k.config_file), '. This complete configuration uses the environment variable ',
        h('code', {}, k.key_env), ' for the key (set it to your key before starting VS Code or kilo), or replace ',
        h('code', {}, '{env:GX_API_KEY}'), ' with the key in a file that is never committed.'),
      codeBlock(k.config_example, 'json'),
      h('div', { class: 'btn-row' }, copyButton(() => k.config_example, 'Copy config'))),
    testCard('kilo'),
    card('Troubleshooting', h('ul', {},
      h('li', {}, '401 / "invalid key": the key was revoked, expired or mistyped. Create or replace it on the API Keys page.'),
      h('li', {}, 'gx-auto missing from the model list: the key does not allow gx-auto. Replace it with a key that does.'),
      h('li', {}, 'Slow first answer: gx-reason loads on demand (about 6-7 minutes cold). See Resource Control.'),
      h('li', {}, 'Timeouts in long agent turns: keep "timeout" at 900000 in the config.'))),
  ];
}

function openwebuiPanel() {
  const o = info.openwebui;
  const version = o.version
    ? `Open WebUI ${o.version} runs on gx10-01; steps verified against ${o.verified_against}${o.matches === false ? ' — your version differs, labels may have moved' : ''}.`
    : `Steps verified against Open WebUI ${o.verified_against}.`;
  return [
    card('Open WebUI',
      h('p', { class: 'lead' }, 'Connect Open WebUI to the cluster through the LiteLLM gateway with a key from API Keys.'),
      h('p', { class: 'small muted' }, version),
      h('p', {}, o.note)),
    card('Connection settings',
      kv([
        ['Connection Type', 'External'],
        copyRow('URL', info.gateway_url, 'owui-url'),
        o.version ? copyRow('URL (Open WebUI on gx10-01 itself)', o.base_url_same_host, 'owui-local-url')
          : ['URL (Open WebUI on gx10-01 itself)', undefined],
        ['Auth', 'Bearer'],
        ['API Key', h('span', {}, 'a key from ', h('a', { href: '#/keys' }, 'API Keys'), ' (never the master key)')],
        ['API Type', 'Chat Completions'],
        ['Advanced › Provider', 'Default'],
      ]),
      h('p', {}, 'Model IDs:'),
      aliasChips(['gx-auto', 'gx-mini', 'gx-fast', 'gx-reason', 'gx-max'])),
    keyWorkflow('openwebui'),
    card('Manual setup (Open WebUI)', h('ol', { class: 'steps' }, o.manual_steps.map((s) => h('li', {}, s)))),
    testCard('openwebui'),
    card('Troubleshooting', h('ul', {},
      h('li', {}, 'Verify Connection fails: check the URL ends in /v1 and Auth is Bearer.'),
      h('li', {}, 'No models listed: the key allows none of the Model IDs you entered.'),
      h('li', {}, 'Images, video and music: use GX-Playground; Open WebUI is set up for chat here.'))),
  ];
}

function genericPanel() {
  const ex = info.generic.examples;
  return [
    card('OpenAI-compatible clients',
      kv([copyRow('Base URL', info.gateway_url, 'generic-base-url'),
        ['Authentication', 'Authorization: Bearer <key from API Keys>']]),
      h('p', {}, 'Chat aliases:'), aliasChips(info.text_aliases),
      h('p', {}, 'Creative models are not chat models: ',
        Object.entries(info.creative_aliases).map(([a, where]) => h('span', { class: 'chip' }, h('code', {}, a), ` ${where}`)))),
    card('Shell', h('p', {}, 'Set your key once:'), codeBlock(ex.env, 'bash'),
      h('p', {}, 'List models:'), codeBlock(ex.curl_models, 'bash'),
      h('p', {}, 'A gx-mini completion:'), codeBlock(ex.curl_chat, 'bash')),
    card('Python (openai package)', codeBlock(ex.python, 'python')),
    card('JavaScript (openai package)', codeBlock(ex.javascript, 'js')),
    card('gx-max (heavy)', h('p', { class: 'callout callout-warning' }, 'gx-max takes over both nodes. The first request drains every other model and waits while it loads.'),
      codeBlock(ex.gx_max, 'bash')),
    card('Music API (GX-Playground)', kv([copyRow('Music API base', info.music_api_url, 'music-api-url')]),
      codeBlock(ex.music, 'bash'), h('p', { class: 'small' }, 'The key must allow gx-music. Full reference: Docs › GX-Playground and music.')),
    testCard('generic'),
  ];
}

function render() {
  const tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Client' }, TABS.map(([id, label]) => h('button', {
    type: 'button', role: 'tab', id: `tab-${id}`, 'aria-selected': String(tab === id), 'aria-controls': 'setup-panel',
    class: `tab${tab === id ? ' active' : ''}`, tabindex: tab === id ? '0' : '-1',
    onclick: () => { tab = id; history.replaceState(null, '', `#/setup/${id}`); render(); document.getElementById(`tab-${id}`).focus(); },
    onkeydown: (ev) => {
      const idx = TABS.findIndex(([t]) => t === tab);
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        ev.preventDefault();
        tab = TABS[(idx + (ev.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length][0];
        render();
        document.getElementById(`tab-${tab}`).focus();
      }
    },
  }, label)));
  const panelContent = tab === 'kilo' ? kiloPanel() : tab === 'openwebui' ? openwebuiPanel() : genericPanel();
  clear(root).append(
    h('p', { class: 'lead' }, 'Everything a client needs, with live values from this cluster. No SSH needed. ',
      h('a', { href: '#/keys' }, 'API Keys'), ' · ', h('a', { href: '#/models' }, 'Models'), ' · ',
      h('a', { href: '#/docs/clients' }, 'API docs')),
    tabs,
    h('div', { id: 'setup-panel', role: 'tabpanel', 'aria-labelledby': `tab-${tab}`, class: 'stack' }, panelContent));
}

export default {
  title: 'Setup',
  interval: 0,
  async mount(el, { params }) {
    root = el;
    if (params && TABS.some(([t]) => t === params[0])) tab = params[0];
    info = await api.get('/api/setup');
    render();
  },
};
