// Connections / API Setup: live gateway URL, key reveal, Kilo, OpenWebUI, curl, tests.
import { api } from '../api.js';
import {
  h, clear, card, kv, toast, errorBox, copyButton, codeBlock, table, levelBadge,
} from '../dom.js';

let root;
let info = null;
let tab = 'kilo';
let revealed = null;

const TABS = [['kilo', 'Kilo Code'], ['openwebui', 'Open WebUI'], ['generic', 'OpenAI-compatible clients']];

function copyRow(label, value, id) {
  return [label, h('span', { class: 'copy-row' }, h('code', { id }, value), copyButton(() => value, 'Copy'))];
}

function aliasChips(list) {
  return h('div', { class: 'checks' }, list.map((a) => h('span', { class: 'chip' }, h('code', {}, a), copyButton(() => a, 'Copy'))));
}

function keyCard() {
  const ak = info.api_key || {};
  const shown = revealed || ak.masked;
  const valueEl = h('code', { id: 'gw-key', class: 'secret' }, shown);
  const revealBtn = h('button', { type: 'button', class: 'btn btn-sm', id: 'gw-key-reveal' },
    revealed ? 'Hide' : 'Reveal');
  revealBtn.addEventListener('click', async () => {
    if (revealed) {
      revealed = null;
      render();
      return;
    }
    revealBtn.disabled = true;
    try {
      const r = await api.post('/api/connections/key/reveal', {});
      revealed = (r.api_key && r.api_key.key) || null;
      if (!revealed) toast('Key is not available in this session.', 'warn');
      render();
    } catch (e) {
      toast(e.message, 'crit');
    } finally {
      revealBtn.disabled = false;
    }
  });
  const copyBtn = copyButton(() => revealed || '', 'Copy');
  if (!revealed) copyBtn.disabled = true;
  return card('API key',
    h('p', {}, ak.label || 'Gateway key for normal API clients.'),
    h('p', { class: 'small muted' }, ak.source || ''),
    h('div', { class: 'btn-row' }, valueEl, revealBtn, copyBtn),
    h('p', { class: 'small muted' }, 'Masked until you click Reveal. Never committed, never in static HTML.'));
}

function testResults(out, r) {
  clear(out).append(
    h('p', {}, h('span', { class: `badge badge-${r.ok ? 'ok' : 'crit'}`, id: 'conn-test-verdict' },
      r.ok ? 'PASS' : 'FAIL'), ' ', r.summary || ''),
    table(['Check', 'Result', 'Detail'], (r.checks || []).map((c) => [c.check,
      h('span', { class: `badge badge-${c.ok ? 'ok' : 'crit'}` }, c.ok ? 'PASS' : 'FAIL'),
      `${c.status ? `HTTP ${c.status} · ` : ''}${c.detail || ''}${c.ms !== undefined ? ` · ${c.ms} ms` : ''}`]),
    { caption: 'Connection test' }));
}

function liveTests() {
  const out = h('div', { class: 'test-result', 'aria-live': 'polite', id: 'conn-test-result' });
  function run(target, label) {
    return h('button', {
      type: 'button', class: 'btn', id: `test-${target}`,
      onclick: async (ev) => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        clear(out).append(h('p', { class: 'loading' }, h('span', { class: 'spin', 'aria-hidden': 'true' }), `Testing ${target}…`));
        try {
          testResults(out, await api.post('/api/connections/test', { target }));
        } catch (e) {
          clear(out).append(errorBox(e));
        } finally {
          btn.disabled = false;
        }
      },
    }, label);
  }
  return card('Connection test',
    h('p', {}, 'Uses the live gateway key on the server. Does not start gx-max.'),
    h('div', { class: 'btn-row' },
      run('gateway', 'Test Gateway'),
      run('gx-mini', 'Test gx-mini'),
      run('gx-code', 'Test gx-code'),
      run('gx-auto', 'Test gx-auto')),
    out);
}

function gatewayCard() {
  const g = info.gateway || {};
  const status = g.healthy ? 'Healthy' : (g.status || 'Unhealthy');
  return card('Gateway',
    kv([
      ['Status', levelBadge(g.healthy ? 'ok' : 'crit', status)],
      copyRow('OpenAI compatible base URL', g.public_url || info.gateway_url, 'conn-public-url'),
      copyRow('Internal service URL (OpenWebUI on gx10-01)', g.internal_url || info.local_gateway_url, 'conn-internal-url'),
      ['API path', h('code', {}, g.api_path || '/v1')],
      ['Authentication', g.auth || 'enabled'],
      ['Public logical models', aliasChips(g.models || info.text_aliases || [])],
    ]));
}

function kiloPanel() {
  const k = info.kilo;
  const version = k.extension
    ? `Kilo Code ${k.extension} installed on gx10-01${k.cli ? ` (CLI ${k.cli})` : ''}; steps verified against ${k.verified_against}${k.matches === false ? ' — your version differs, labels may have moved' : ''}.`
    : `Steps verified against Kilo Code ${k.verified_against || k.verified_version}.`;
  return [
    card('Kilo Code',
      h('p', { class: 'lead' }, 'Recommended coding model: ', h('strong', {}, 'gx-code'), '.'),
      h('ul', {}, info.gx_auto.routes.map((r) => h('li', {}, `${r.when} → `, h('code', {}, r.to)))),
      h('p', {}, info.gx_auto.gx_max),
      h('p', { class: 'small muted' }, version)),
    card('Connection settings',
      kv([
        ['Provider', h('strong', {}, k.provider_api)],
        copyRow('Base URL', info.gateway_url, 'kilo-base-url'),
        ['API key', 'Reveal on this page, then paste into Kilo'],
        copyRow('Recommended model', k.recommended_model || 'gx-code', 'kilo-model'),
      ]),
      h('p', {}, 'Available alternatives:'),
      aliasChips(k.alternatives || ['gx-mini', 'gx-auto', 'gx-max'])),
    card('Manual setup (Kilo Code settings)',
      h('ol', { class: 'steps' }, k.manual_steps.map((s) => h('li', {}, s)))),
    card('Config file',
      h('p', {}, 'Kilo Code reads ', h('code', {}, k.config_file), '. Schema matches the installed extension. The snippet uses ',
        h('code', {}, k.key_env), ' or replace ', h('code', {}, '{env:GX_API_KEY}'), ' after Reveal.'),
      codeBlock(k.config_example, 'json'),
      h('div', { class: 'btn-row' }, copyButton(() => k.config_example, 'Copy config'))),
  ];
}

const IDENTITY_STATE = {
  ok: ['ok', 'In sync'], missing: ['warn', 'Missing'], drift: ['warn', 'Out of date'],
  inactive: ['warn', 'Disabled in Open WebUI'], foreign: ['crit', 'Edited outside the sync'],
};

function identityTable(out, r) {
  if (r.offline) {
    clear(out).append(h('p', { class: 'small muted' }, 'Not available in offline mode.'));
    return;
  }
  clear(out).append(
    h('p', {}, h('span', { class: `badge badge-${r.in_sync ? 'ok' : 'warn'}`, id: 'owui-identity-verdict' },
      r.in_sync ? 'IN SYNC' : 'NEEDS SYNC'),
    ` Open WebUI ${r.open_webui_version || '?'}${r.version_ok ? '' : ` (sync verified against ${r.verified_against})`}`),
    table(['Alias', 'State', 'Underlying model', 'Facts'], r.items.map((i) => {
      const [tone, label] = IDENTITY_STATE[i.state] || ['warn', i.state];
      return [h('code', {}, i.id), h('span', { class: `badge badge-${tone}` }, label),
        i.repository ? h('code', {}, i.repository) : 'router (no single model)',
        i.facts_verified ? 'verified' : 'repository only'];
    }), { caption: 'Model identity entries in Open WebUI' }),
    ...(r.written && r.written.length ? [h('p', { class: 'small' }, `Updated: ${r.written.join(', ')}`)] : []),
    ...(r.skipped && r.skipped.length ? [h('p', { class: 'small' },
      `Left alone: ${r.skipped.map((x) => `${x.id} (${x.reason})`).join('; ')}`)] : []));
}

function identityCard() {
  const out = h('div', { 'aria-live': 'polite', id: 'owui-identity' }, h('p', { class: 'loading' },
    h('span', { class: 'spin', 'aria-hidden': 'true' }), 'Reading Open WebUI…'));
  const btn = h('button', { type: 'button', class: 'btn', id: 'owui-identity-sync' }, 'Sync identity from the registry');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      identityTable(out, await api.post('/api/setup/openwebui/identity/sync', {}));
      toast('Open WebUI identity entries updated.', 'ok');
    } catch (e) {
      clear(out).append(errorBox(e));
    } finally {
      btn.disabled = false;
    }
  });
  api.get('/api/setup/openwebui/identity').then((r) => identityTable(out, r)).catch((e) => clear(out).append(errorBox(e)));
  return card('Model identity (this cluster\'s Open WebUI)',
    h('p', {}, 'Each alias gets an Open WebUI model entry with a short, factual system prompt from the model registry.'),
    out, h('div', { class: 'btn-row' }, btn));
}

function openwebuiPanel() {
  const o = info.openwebui;
  const version = o.version
    ? `Open WebUI ${o.version} runs on gx10-01; steps verified against ${o.verified_against}${o.matches === false ? ' — your version differs, labels may have moved' : ''}.`
    : `Steps verified against Open WebUI ${o.verified_against}.`;
  return [
    card('Open WebUI',
      h('p', { class: 'lead' }, 'Production connection (already applied on this host):'),
      h('p', { class: 'small muted' }, version),
      h('p', {}, o.note)),
    card('Connection settings',
      kv([
        ['Connection type', o.connection_type || 'External'],
        copyRow('Base URL', o.base_url || 'http://127.0.0.1:4000/v1', 'owui-url'),
        ['Auth', o.auth || 'Bearer'],
        ['API key', 'same gateway key as Reveal on this page'],
        ['API Type', o.api_type || 'Chat Completions'],
        ['OpenAI API', o.enable_openai ? 'on' : 'off'],
        ['Ollama', o.enable_ollama ? 'on' : 'off'],
      ]),
      h('p', {}, 'Expected model list:'),
      aliasChips(o.model_ids || info.text_aliases)),
    card('Manual setup (Open WebUI)', h('ol', { class: 'steps' }, o.manual_steps.map((s) => h('li', {}, s)))),
    identityCard(),
  ];
}

function genericPanel() {
  const ex = info.generic.examples;
  return [
    card('OpenAI-compatible clients',
      kv([copyRow('Base URL', info.gateway_url, 'generic-base-url'),
        ['Authentication', 'Authorization: Bearer <gateway key>']]),
      h('p', {}, 'Public models:'), aliasChips(info.text_aliases)),
    card('curl', h('p', {}, 'Set your key once:'), codeBlock(ex.env, 'bash'),
      h('p', {}, 'List models:'), codeBlock(ex.curl_models, 'bash'),
      h('p', {}, 'A gx-code completion:'), codeBlock(ex.curl_chat, 'bash')),
    card('Python (openai package)', codeBlock(ex.python, 'python')),
    card('JavaScript (openai package)', codeBlock(ex.javascript, 'js')),
    card('gx-max (solver + reviewer)', h('p', { class: 'callout callout-warning' },
      'gx-max uses gx-code-01 as solver and gx-code-02 as reviewer. Do not fire it from a casual health test.'),
      codeBlock(ex.gx_max, 'bash')),
  ];
}

function render() {
  const tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Client' }, TABS.map(([id, label]) => h('button', {
    type: 'button', role: 'tab', id: `tab-${id}`, 'aria-selected': String(tab === id), 'aria-controls': 'setup-panel',
    class: `tab${tab === id ? ' active' : ''}`, tabindex: tab === id ? '0' : '-1',
    onclick: () => { tab = id; history.replaceState(null, '', `#/connections/${id}`); render(); document.getElementById(`tab-${id}`).focus(); },
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
    h('p', { class: 'lead' }, 'How to connect Kilo Code, OpenWebUI, Claude Code, curl and any OpenAI-compatible client. Values come from the live gateway.'),
    gatewayCard(),
    keyCard(),
    liveTests(),
    tabs,
    h('div', { id: 'setup-panel', role: 'tabpanel', 'aria-labelledby': `tab-${tab}`, class: 'stack' }, panelContent));
}

export default {
  title: 'Connections',
  interval: 0,
  async mount(el, { params }) {
    root = el;
    revealed = null;
    if (params && TABS.some(([t]) => t === params[0])) tab = params[0];
    info = await api.get('/api/connections');
    render();
  },
};
