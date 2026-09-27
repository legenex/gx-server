// UPDATES: registry pins vs live state (submodule commit, image digest, model
// revisions) plus an upstream CHECK. The dashboard is check-only: there is
// no auto-update button anywhere, by design.
import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, levelBadge, table, errorBox, spinner, short, clock,
} from '../dom.js';

let root;

const KIND_LABEL = {
  runtime_commit: 'Mia runtime commit',
  image_digest: 'Container image',
  model_revision: 'Model revision',
};

function pinRow(p) {
  return [
    KIND_LABEL[p.kind] || p.kind,
    p.name,
    h('code', { class: 'small' }, p.pin !== null && p.pin !== undefined ? String(p.pin).slice(0, 64) : '—'),
    h('code', { class: 'small' }, p.live !== null && p.live !== undefined ? String(p.live).slice(0, 64) : '—'),
    p.match ? stateBadge('ok', 'match') : levelBadge('warn', 'drift'),
    p.detail || '',
  ];
}

function render(d) {
  const drift = d.drift || [];
  clear(root).append(
    drift.length
      ? h('p', { class: 'callout callout-warning', role: 'note' },
          h('strong', {}, `${drift.length} pin(s) drifted: `),
          drift.map((p) => p.name).join(', '))
      : h('p', { class: 'callout callout-tip' }, 'No drift: every pin matches the live state.'),
    card('Pins vs live',
      table(['Kind', 'Name', 'Registry pin', 'Live', 'Status', 'Detail'],
        (d.pins || []).map(pinRow), { caption: 'Pins vs live', empty: 'No pins are registered.' }),
      kv([
        ['Mia remote', d.mia_remote ? h('code', { class: 'small' }, d.mia_remote) : '—'],
        ['Registry', d.registry_ok ? stateBadge('ok', 'schema 2') : stateBadge('error', 'not schema 2')],
        ['Policy', d.policy || ''],
      ])),
    h('div', { class: 'check-holder' }));
}

async function runCheck() {
  const holder = root.querySelector('.check-holder');
  clear(holder).append(spinner('Checking upstream (GitHub + Hugging Face)…'));
  try {
    const d = await api.post('/api/updates/check', {});
    clear(holder).append(card('Upstream check result',
      table(['Source', 'Name', 'Checked', 'Upstream', 'Pin', 'Match'],
        (d.results || []).map((r) => [
          r.kind || '—',
          r.name || '—',
          r.checked ? stateBadge('ok', 'checked') : stateBadge('unknown', `unavailable: ${r.reason || 'not checked'}`),
          r.upstream ? h('code', { class: 'small' }, short(r.upstream, 20)) : '—',
          r.pin ? h('code', { class: 'small' }, short(r.pin, 20)) : '—',
          r.match === undefined ? '—' : (r.match ? stateBadge('ok', 'match') : levelBadge('warn', 'drift')),
        ]), { caption: 'Upstream check', empty: 'Nothing was checked.' }),
      kv([
        ['Drift (model revisions vs upstream)', (d.drift || []).map((r) => r.name).join(', ') || 'none'],
        ['Generated', d.generated_at ? clock(d.generated_at) : '—'],
        ['Policy', d.policy || ''],
      ]),
      h('p', { class: 'muted small' },
        'This button only queries upstream APIs. Nothing is updated automatically, ever.')));
  } catch (err) {
    clear(holder).append(errorBox(err));
  }
}

export default {
  title: 'Updates',
  interval: 0,
  async mount(el) {
    root = el;
    const btn = h('button', { type: 'button', class: 'btn btn-primary btn-sm', id: 'update-check' },
      'Check for updates');
    btn.addEventListener('click', () => runCheck());
    clear(root).append(
      h('p', { class: 'lead' }, 'Registry pins versus live state: the Mia runtime submodule commit, its '
        + 'container image and each model pack\'s revision. The check queries upstream and reports drift — '
        + 'it never updates anything.'),
      btn, spinner());
    render(await api.get('/api/updates'));
  },
};
