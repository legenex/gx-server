// API KEYS: LiteLLM virtual keys for gateway clients (Kilo Code, Open WebUI,
// scripts, agents). The master key never reaches the browser; a new key's
// secret is shown exactly once. When LiteLLM is not reachable the backend
// degrades honestly and this page says why instead of showing a red banner.
import { api } from '../api.js';
import { h, clear, toast, errorBox, table, stateBadge, confirmDialog, copyButton, codeBlock } from '../dom.js';

// The only public aliases in the V4.1 world.
const ALIASES = ['gx-max', 'gx-auto'];
let listEl;
let secretEl;
let gatewayUrl = '';

function showSecret(created) {
  let secret = created.secret;
  const input = h('input', { type: 'text', readonly: true, value: secret, id: 'new-key-secret', class: 'secret', 'aria-label': 'New API key' });
  const test = h('button', { type: 'button', class: 'btn btn-sm', id: 'new-key-test' }, 'Test this key');
  const result = h('p', { class: 'small', 'aria-live': 'polite' });
  test.addEventListener('click', async () => {
    test.disabled = true;
    result.textContent = 'Testing: listing models and asking the gateway…';
    try {
      const model = created.models.includes('gx-auto') ? 'gx-auto' : created.models[0];
      const r = await api.post('/api/keys/test', { secret, model });
      result.textContent = `GET /v1/models → ${r.models_status} (${(r.models || []).join(', ')}); chat → ${r.chat_status}`
        + `${r.chat_answer ? ` “${r.chat_answer}”` : ''}${r.chat_error ? ` (${r.chat_error})` : ''}`
        + ` in ${r.chat_seconds ?? '—'} s`;
    } catch (e) { result.textContent = e.message; } finally { test.disabled = false; }
  });
  const hide = h('button', {
    type: 'button', class: 'btn btn-sm btn-ghost',
    onclick: () => { secret = ''; clear(secretEl); },
  }, 'I have copied it: hide');
  clear(secretEl).append(h('section', { class: 'card callout callout-warning', role: 'alert' },
    h('h2', { class: 'card-title' }, `New key “${created.name}”`),
    h('p', {}, h('strong', {}, 'Copy it now. '), 'The key is not stored by this interface and cannot be shown again.'),
    h('div', { class: 'btn-row' }, input, copyButton(() => secret, 'Copy key')),
    h('p', { class: 'small' }, `Allowed aliases: ${created.models.join(', ')} · Expires: ${created.expires || 'never'}`),
    h('div', { class: 'btn-row' }, test, hide), result,
    gatewayUrl ? codeBlock(`curl ${gatewayUrl}/models -H "Authorization: Bearer <paste the key>"`, 'bash') : null));
  input.focus();
  input.select();
}

async function loadKeys() {
  try {
    const data = await api.get('/api/keys');
    gatewayUrl = data.gateway_url || gatewayUrl;
    if (data.unavailable) {
      clear(listEl).append(h('p', { class: 'callout callout-warning', role: 'note' },
        `Key management is unavailable: ${data.reason || 'LiteLLM did not answer'}. `
        + 'The gateway itself may still be serving; only key management needs the master key.'));
      return;
    }
    clear(listEl).append(table(['Name', 'Key', 'Created', 'Expires', 'Allowed aliases', 'Last used', 'Status', 'Actions'],
      (data.keys || []).map((k) => [
        h('strong', {}, k.name), h('code', {}, k.masked), (k.created || '').replace('T', ' ').slice(0, 16),
        k.expires ? k.expires.replace('T', ' ').slice(0, 16) : 'never',
        (k.models || []).length ? k.models.join(', ') : 'all',
        k.last_used ? k.last_used.replace('T', ' ').slice(0, 16) : 'never',
        stateBadge(k.status === 'active' ? 'ok' : 'error', k.status),
        h('div', { class: 'btn-row' },
          h('button', {
            type: 'button', class: 'btn btn-sm', 'data-replace': k.name,
            onclick: async () => {
              const res = await confirmDialog({ title: `Replace “${k.name}”?`, body: 'A new key with the same settings is created and this one stops working immediately. Update your clients with the new key.', okLabel: 'Replace' });
              if (!res.ok) return;
              try { showSecret(await api.post(`/api/keys/${k.id}/replace`, { confirm: true })); await loadKeys(); } catch (e) { toast(e.message, 'crit'); }
            },
          }, 'Replace'),
          h('button', {
            type: 'button', class: 'btn btn-sm btn-danger', 'data-revoke': k.name,
            onclick: async () => {
              const res = await confirmDialog({ title: `Revoke “${k.name}”?`, body: 'Every client using this key is refused from now on.', phrase: k.name, okLabel: 'Revoke' });
              if (!res.ok) return;
              try { await api.post(`/api/keys/${k.id}/revoke`, { confirm: true }); toast(`Revoked ${k.name}`); await loadKeys(); } catch (e) { toast(e.message, 'crit'); }
            },
          }, 'Revoke')),
      ]), { caption: 'Gateway API keys', empty: 'No keys yet.' }));
  } catch (e) { clear(listEl).append(errorBox(e)); }
}

export default {
  title: 'API Keys',
  interval: 0,
  async mount(el) {
    clear(el);
    const form = h('form', { id: 'key-form', class: 'card', novalidate: true });
    const name = h('input', { id: 'key-name', required: true, maxlength: 63, placeholder: 'e.g. kilo-code-laptop' });
    const boxes = ALIASES.map((a) => h('label', { class: 'check' },
      h('input', { type: 'checkbox', name: 'models', value: a, checked: a === 'gx-auto' }), a));
    const expiry = h('select', { id: 'key-expiry' }, [['never', 'Never'], ['1d', '1 day'], ['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['365d', '1 year']]
      .map(([v, l]) => h('option', { value: v }, l)));
    const rpm = h('input', { id: 'key-rpm', type: 'number', min: 1, max: 100000, placeholder: 'unlimited' });
    const par = h('input', { id: 'key-par', type: 'number', min: 1, max: 1000, placeholder: 'unlimited' });
    const err = h('p', { class: 'form-error', role: 'alert', hidden: true });
    form.append(
      h('h2', { class: 'card-title' }, 'Create an API key'),
      h('div', { class: 'form-grid' },
        h('div', { class: 'field' }, h('label', { for: 'key-name' }, 'Name'), name),
        h('div', { class: 'field' }, h('label', { for: 'key-expiry' }, 'Expiry'), expiry),
        h('div', { class: 'field' }, h('label', { for: 'key-rpm' }, 'Requests per minute (optional)'), rpm),
        h('div', { class: 'field' }, h('label', { for: 'key-par' }, 'Parallel requests (optional)'), par)),
      h('fieldset', {}, h('legend', {}, 'Allowed aliases'), h('div', { class: 'checks' }, boxes),
        h('p', { class: 'hint' }, 'gx-max serves DeepSeek V4.1 Flash directly; gx-auto lets the scheduler '
          + 'pick profile and reasoning per request. Both are the same single model.')),
      err,
      h('button', { type: 'submit', class: 'btn btn-primary', id: 'key-create' }, 'Create key'));
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.hidden = true;
      const models = [...form.querySelectorAll('input[name=models]:checked')].map((x) => x.value);
      const body = { name: name.value.trim(), models, expiry: expiry.value };
      const problem = !body.name ? 'Enter a name for the key.'
        : !/^[A-Za-z0-9][A-Za-z0-9 ._-]*$/.test(body.name) ? 'Use letters, digits, spaces, dot, dash or underscore in the name.'
          : !models.length ? 'Allow at least one alias.' : '';
      if (problem) { err.hidden = false; err.textContent = problem; name.focus(); return; }
      if (rpm.value) body.rpm_limit = Number(rpm.value);
      if (par.value) body.max_parallel_requests = Number(par.value);
      try {
        const created = await api.post('/api/keys', body);
        showSecret(created);
        name.value = '';
        await loadKeys();
      } catch (e) { err.hidden = false; err.textContent = e.message; }
    });
    secretEl = h('div', {});
    listEl = h('div', {});
    el.append(
      h('p', { class: 'lead' }, 'LiteLLM virtual keys for Kilo Code, Open WebUI, scripts and agents. Each key '
        + 'is limited to the aliases you pick (gx-max / gx-auto only) and can be revoked at any time. '
        + 'The gateway master key is never shown here.'),
      secretEl, form, h('h2', {}, 'Existing keys'), listEl);
    await loadKeys();
  },
  unmount() { if (secretEl) clear(secretEl); },
};
