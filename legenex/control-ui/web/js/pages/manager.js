// MODEL MANAGER (D-034): installed models, Hugging Face lookup, staged
// install -> verify -> test -> assign -> accept / rollback -> delete-if-unused.
import { api } from '../api.js';
import {
  h, clear, toast, errorBox, kv, table, bytes, stateBadge, ago, short, confirmDialog, duration,
} from '../dom.js';

let root;
let invEl;
let jobsEl;
let lookupEl;
let searchEl;
let tokenEl;

const yes = (v) => (v === true ? 'yes' : v === false ? 'no' : (v || '—'));

function hfLink(repo, rev) {
  if (!repo) return '—';
  const url = `https://huggingface.co/${repo}${rev ? `/tree/${rev}` : ''}`;
  return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, repo);
}

async function runJob(path, body, label) {
  try {
    const job = await api.post(path, body);
    toast(`${label}: started`, 'ok');
    watchJob(job.id);
    return job;
  } catch (e) {
    toast(`${label}: ${e.message}`, 'crit', 9000);
    return null;
  }
}

function watchJob(id) {
  const out = h('pre', { class: 'job-output', tabindex: '0', 'aria-live': 'polite' });
  const card = h('section', { class: 'card', id: `mmjob-${id}` }, h('h3', {}, 'Job'), out);
  jobsEl.prepend(card);
  const tick = async () => {
    try {
      const j = await api.get(`/api/manager/jobs/${id}`);
      clear(card).append(
        h('div', { class: 'card-head' }, h('h3', { class: 'card-title' }, j.label), stateBadge(j.state)),
        j.progress !== null && j.progress !== undefined
          ? h('progress', { max: 1, value: j.progress, 'aria-label': 'progress' }) : null,
        h('p', { class: 'muted small' }, `${duration(j.elapsed_seconds)}${j.progress ? ` · ${Math.round(j.progress * 100)}%` : ''}`),
        Object.keys(j.result || {}).length ? kv(Object.entries(j.result).map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)])) : null,
        out);
      out.textContent = (j.output || []).join('\n');
      out.scrollTop = out.scrollHeight;
      if (j.state === 'running') setTimeout(tick, 3000);
      else { toast(`${j.label}: ${j.state}`, j.state === 'succeeded' ? 'ok' : 'crit', 8000); loadInventory(); }
    } catch (e) { card.append(errorBox(e)); }
  };
  tick();
}

function aliasTable(inv) {
  const aliases = inv.aliases || {};
  const rows = Object.entries(aliases).map(([alias, a]) => {
    const point = (inv.rollback || {})[alias];
    const prev = a.previous;
    const acts = h('div', { class: 'btn-row' });
    if (['gx-mini', 'gx-fast', 'gx-reason'].includes(alias)) {
      acts.append(h('button', {
        type: 'button', class: 'btn btn-sm', 'data-probe': alias,
        onclick: async (ev) => {
          ev.target.disabled = true;
          try {
            const r = await api.post('/api/manager/probe', { alias });
            toast(`${alias}: ${r.ok ? 'real completion OK' : 'FAILED'} ${r.answer || r.error || ''} (${r.seconds ?? '—'} s)`, r.ok ? 'ok' : 'crit', 9000);
          } catch (e) { toast(e.message, 'crit'); } finally { ev.target.disabled = false; }
        },
      }, 'Test through gateway'));
    }
    if (point) {
      acts.append(
        h('button', { type: 'button', class: 'btn btn-sm btn-danger', onclick: () => runJob('/api/manager/rollback', { alias }, `Roll back ${alias}`) }, 'Roll back'),
        h('button', {
          type: 'button', class: 'btn btn-sm',
          onclick: async () => { await api.post('/api/manager/accept', { alias }); toast(`${alias} accepted`); loadInventory(); },
        }, 'Accept'));
    } else if (prev && !String(prev.status || '').includes('accepted')) {
      acts.append(h('button', {
        type: 'button', class: 'btn btn-sm', 'data-accept': alias,
        onclick: async () => {
          const ok = await confirmDialog({
            title: `Accept the current ${alias}?`,
            body: `This marks ${prev.repository || prev.path} as superseded so its files can be deleted from the inventory below.`,
            okLabel: 'Accept', danger: false,
          });
          if (!ok.ok) return;
          await api.post('/api/manager/accept', { alias });
          toast(`${alias}: previous model marked superseded`);
          loadInventory();
        },
      }, 'Accept (allow cleanup)'));
    }
    return [
      h('strong', {}, alias),
      h('span', {}, hfLink(a.repository, a.revision), a.interim ? h('span', { class: 'badge badge-crit' }, 'interim') : null),
      h('code', {}, short(a.revision, 12)),
      a.node || '—', a.runtime || '—', a.uncensored || '—',
      prev ? h('span', { class: 'small' }, `${prev.repository || prev.path} — ${prev.status || ''}`) : '—',
      acts,
    ];
  });
  return table(['Alias', 'Repository', 'Revision', 'Node', 'Runtime', 'Uncensored', 'Previous / rollback', 'Actions'], rows,
    { caption: 'Alias bindings' });
}

function installedTable(inv) {
  const rows = (inv.installed || []).map((m) => {
    const acts = h('div', { class: 'btn-row' });
    const testKey = `${m.node}:${m.path}`;
    const test = (inv.tests || {})[testKey];
    if (['gguf', 'vllm', 'staging'].includes(m.category)) {
      acts.append(h('button', {
        type: 'button', class: 'btn btn-sm',
        onclick: () => runJob('/api/manager/test', { node: m.node, path: m.path, runtime: m.category === 'gguf' ? 'llama.cpp' : 'vllm' },
          `Test ${m.name}`),
      }, 'Test-serve'));
    }
    if (m.category === 'gguf' && m.node === 'node1' && !m.aliases.length) {
      acts.append(assignButton('gx-mini', m));
    }
    if (m.category === 'vllm' && !m.aliases.length) {
      acts.append(assignButton(m.node === 'node1' ? 'gx-fast' : 'gx-reason', m));
    }
    if (m.url) acts.append(h('a', { class: 'btn btn-sm btn-ghost', href: m.url, target: '_blank', rel: 'noopener noreferrer' }, 'HF'));
    acts.append(h('button', {
      type: 'button', class: 'btn btn-sm btn-danger', disabled: !m.deletable,
      title: m.deletable ? 'Delete these files' : `In use: ${(m.referenced_by || []).join(', ')}`,
      onclick: async () => {
        const res = await confirmDialog({
          title: `Delete ${m.name} from ${m.node === 'node1' ? 'gx10-01' : 'gx10-02'}?`,
          body: h('div', {}, h('p', {}, `${m.path} (${bytes(m.size)}) is removed permanently.`)),
          phrase: m.name, okLabel: 'Delete files',
        });
        if (res.ok) runJob('/api/manager/delete', { node: m.node, path: m.path, confirm: res.phrase }, `Delete ${m.name}`);
      },
    }, 'Delete'));
    return [
      m.node === 'node1' ? 'gx10-01' : 'gx10-02',
      h('span', {}, h('strong', {}, m.name), h('br'), h('code', { class: 'small' }, m.path)),
      hfLink(m.repository, m.revision),
      h('code', {}, short(m.revision, 10)),
      bytes(m.size),
      m.verified ? stateBadge('ok', 'sha256') : stateBadge('unknown', 'no manifest'),
      m.aliases.length ? m.aliases.join(', ') : (m.referenced_by && m.referenced_by.length ? m.referenced_by.join(', ') : 'unused'),
      test ? h('span', { class: test.correct ? '' : 'text-crit' }, `${test.correct ? 'OK' : 'FAILED'} ${ago(test.at)} · load ${test.load_seconds}s`) : '—',
      acts,
    ];
  });
  return table(['Node', 'Model', 'Repository', 'Revision', 'Disk', 'Verified', 'Used by', 'Last test', 'Actions'], rows,
    { caption: 'Installed models', empty: 'No model directories found.' });
}

function assignButton(alias, m) {
  return h('button', {
    type: 'button', class: 'btn btn-sm btn-primary',
    onclick: async () => {
      const preview = await api.post('/api/manager/assign', { alias, path: m.path, dry_run: true });
      watchJob(preview.id);
      const res = await confirmDialog({
        title: `Replace ${alias} with ${m.name}?`,
        body: h('div', {},
          h('p', {}, `The binding is changed, ${alias} is restarted and a real completion is sent through LiteLLM. `
            + 'If that fails, the previous binding is restored automatically. The previous model stays on disk until you Accept.'),
          h('p', { class: 'callout callout-warning' }, `${alias} is unavailable while it restarts.`)),
        phrase: alias, okLabel: `Replace ${alias}`,
      });
      if (res.ok) runJob('/api/manager/assign', { alias, path: m.path }, `Assign ${alias}`);
    },
  }, `Assign to ${alias}`);
}

function diskCards(inv) {
  const card = (label, d) => h('div', { class: 'metric' },
    h('div', { class: 'metric-head' }, h('span', {}, label),
      h('strong', {}, d ? `${bytes(d.free)} free of ${bytes(d.total)}` : 'unavailable')));
  return h('div', { class: 'grid-2' }, card('gx10-01 /srv/models', (inv.disk || {}).node1), card('gx10-02 /srv/models', (inv.disk || {}).node2));
}

async function loadInventory() {
  try {
    const inv = await api.get('/api/manager/inventory');
    clear(invEl).append(
      diskCards(inv),
      inv.node2_error ? h('p', { class: 'callout callout-warning' }, inv.node2_error) : null,
      h('h2', {}, 'Alias bindings'), aliasTable(inv),
      h('h2', {}, 'Installed models'), installedTable(inv));
    renderToken(inv.hf_token);
  } catch (e) { clear(invEl).append(errorBox(e)); }
}

function renderToken(state) {
  const input = h('input', { type: 'password', id: 'hf-token', autocomplete: 'off', placeholder: 'hf_…', 'aria-label': 'Hugging Face token' });
  clear(tokenEl).append(
    h('p', {}, state && state.configured
      ? (state.valid ? `Token configured for Hugging Face user “${state.user}”.` : `Token configured but rejected: ${state.error}`)
      : 'No token configured: public repositories only. Gated models (for example the gx-reason target) need a read token '
        + 'whose account has accepted the model terms.'),
    h('div', { class: 'btn-row' }, input,
      h('button', {
        type: 'button', class: 'btn btn-sm',
        onclick: async () => {
          try { const s = await api.post('/api/manager/hf-token', { token: input.value }); input.value = ''; toast(`Token saved (${s.user})`); loadInventory(); } catch (e) { toast(e.message, 'crit'); }
        },
      }, 'Save token'),
      state && state.configured ? h('button', {
        type: 'button', class: 'btn btn-sm btn-ghost',
        onclick: async () => { await api.post('/api/manager/hf-token', { clear: true }); toast('Token removed'); loadInventory(); },
      }, 'Remove token') : null),
    h('p', { class: 'muted small' }, 'Stored on gx10-01 in /srv/projects/gx-cluster/secrets/hf/token (0600). It is never shown again or sent to the browser.'));
}

function renderLookup(info) {
  const planBtn = h('button', { type: 'button', class: 'btn btn-primary' }, 'Plan install');
  const node = h('select', { id: 'mm-node' }, ['node1', 'node2'].map((n) => h('option', { value: n, selected: n === (info.suggested_node === 'node2' ? 'node2' : 'node1') }, n === 'node1' ? 'gx10-01' : 'gx10-02')));
  const cat = h('select', { id: 'mm-cat' }, ['gguf', 'vllm', 'deepseek', 'staging'].map((c) => h('option', { value: c, selected: c === info.suggested_target.category }, c)));
  const name = h('input', { id: 'mm-name', value: info.suggested_target.name });
  const include = h('input', { id: 'mm-include', placeholder: 'e.g. *Q4_K_M.gguf, mmproj-*' });
  const planOut = h('div', { class: 'plan-out' });
  planBtn.addEventListener('click', async () => {
    try {
      const plan = await api.post('/api/manager/plan', {
        repository: info.repository, revision: info.revision, node: node.value, category: cat.value, name: name.value,
        include: include.value,
      });
      const pf = plan.preflight || {};
      const pfClass = { SAFE: 'ok', TIGHT: 'warn', BLOCKED: 'crit' }[pf.status] || 'unknown';
      clear(planOut).append(kv([
        ['Target', `${plan.target} on ${plan.node}`], ['Task', plan.task || '—'],
        ['Memory estimate', `${plan.memory_estimate_gib} GiB`],
        ['Runtime', (plan.runtimes || []).join(', ') || '—'], ['trust_remote_code', yes(plan.trust_remote_code)],
      ]), h('section', { class: 'card preflight', 'aria-label': 'Disk preflight', id: 'mm-preflight' },
        h('div', { class: 'card-head' }, h('h3', {}, 'Disk preflight'),
          h('span', { class: `badge badge-${pfClass}`, id: 'mm-preflight-status' }, pf.status || '—')),
        kv([
          ['Current free', bytes(pf.current_free_bytes)],
          ['Download', bytes(pf.download_bytes)],
          ['Staging copy', pf.staging_bytes ? bytes(pf.staging_bytes) : 'none (written in place)'],
          ['Final installed size', bytes(pf.final_installed_bytes)],
          ['Cache duplication', pf.cache_duplication ? 'yes' : 'no'],
          ['Peak requirement', bytes(pf.peak_bytes)],
          ['Free afterwards', bytes(pf.free_after_bytes)],
          ['Required headroom', bytes(pf.required_headroom_bytes)],
        ]),
        h('p', { class: 'small' }, pf.explanation || ''),
        pf.status && pf.status !== 'SAFE'
          ? h('p', {}, h('a', { class: 'btn btn-sm', href: '#/storage', id: 'mm-open-storage' }, 'Open Storage Cleanup')) : null),
      plan.warnings.length ? h('ul', { class: 'problems' }, plan.warnings.map((w) => h('li', {}, w))) : null,
      h('button', {
        type: 'button', class: 'btn btn-primary', disabled: !plan.ok, id: 'mm-stage',
        onclick: () => runJob('/api/manager/stage', plan, `Install ${info.repository}`),
      }, plan.ok ? 'Download and verify' : 'Cannot install'));
    } catch (e) { clear(planOut).append(errorBox(e)); }
  });
  clear(lookupEl).append(
    h('h3', {}, hfLink(info.repository, info.revision)),
    kv([
      ['Revision (pinned)', h('code', {}, info.revision)], ['Author', info.author], ['Task', info.task || '—'],
      ['Kind', info.kind], ['Formats', info.formats.join(', ') || '—'], ['Quantization', info.quantization.join(', ') || '—'],
      ['Architecture', (info.architecture || []).toString() || '—'], ['Parameters', info.parameters ? `${(info.parameters / 1e9).toFixed(2)} B` : '—'],
      ['MoE', yes(info.moe)], ['Vision', yes(info.vision)], ['Context', info.context ? info.context.toLocaleString() : '—'],
      ['Size', bytes(info.size_bytes)], ['Files', String(info.file_count)], ['Licence', info.licence || '—'],
      ['Gated', info.gated ? `${info.gated}${info.accessible ? ' (accessible)' : ' — NOT accessible with the current token'}` : 'no'],
      ['Uncensored / abliterated', yes(info.uncensored)], ['trust_remote_code', yes(info.trust_remote_code)],
      ['Base model', Array.isArray(info.base_model) ? info.base_model.join(', ') : (info.base_model || '—')],
      ['Likely runtime', (info.runtimes || []).join(', ') || 'none recognised'],
      ['Candidate aliases', (info.candidate_aliases || []).join(', ') || '—'],
      ['Updated', info.last_modified || '—'], ['Downloads', String(info.downloads ?? '—')],
    ]),
    (info.warnings || []).length ? h('ul', { class: 'problems' }, info.warnings.map((w) => h('li', {}, w))) : null,
    info.access_note ? h('p', { class: 'callout callout-warning' }, info.access_note) : null,
    h('details', {}, h('summary', {}, `Files (${info.file_count})`),
      table(['Path', 'Size'], info.files.map((f) => [f.path, bytes(f.size)]), { caption: 'Files' })),
    info.readme_excerpt ? h('details', {}, h('summary', {}, 'Model card (untrusted text)'), h('pre', { class: 'code card-text', tabindex: '0' }, info.readme_excerpt)) : null,
    h('h3', {}, 'Install'),
    h('div', { class: 'form-grid' },
      h('div', { class: 'field' }, h('label', { for: 'mm-node' }, 'Node'), node),
      h('div', { class: 'field' }, h('label', { for: 'mm-cat' }, 'Folder'), cat),
      h('div', { class: 'field' }, h('label', { for: 'mm-name' }, 'Directory name'), name),
      h('div', { class: 'field' }, h('label', { for: 'mm-include' }, 'Only files matching (optional)'), include)),
    planBtn, planOut);
}

export default {
  title: 'Model Manager',
  interval: 0,
  async mount(el) {
    clear(el);
    root = el;
    const ref = h('input', { id: 'mm-ref', placeholder: 'owner/name, owner/name@revision, or https://huggingface.co/…', 'aria-label': 'Repository or URL' });
    const q = h('input', { id: 'mm-q', type: 'search', placeholder: 'Search Hugging Face (e.g. qwen3 uncensored gguf)', 'aria-label': 'Search Hugging Face' });
    lookupEl = h('div', { class: 'lookup', 'aria-live': 'polite' });
    searchEl = h('div', { class: 'search-results' });
    invEl = h('div', {});
    jobsEl = h('div', { class: 'mm-jobs' });
    tokenEl = h('div', {});
    const lookup = async (value) => {
      clear(lookupEl).append(h('p', { class: 'loading' }, 'Looking up…'));
      try { renderLookup(await api.post('/api/manager/lookup', { ref: value })); } catch (e) { clear(lookupEl).append(errorBox(e)); }
    };
    const search = async () => {
      clear(searchEl).append(h('p', { class: 'loading' }, 'Searching…'));
      try {
        const res = await api.get(`/api/manager/search?q=${encodeURIComponent(q.value)}&limit=25`);
        clear(searchEl).append(table(['Repository', 'Task', 'Quant', 'Uncensored', 'Gated', 'Downloads', 'Updated', ''],
          res.results.map((r) => [
            hfLink(r.repository), r.task || '—', (r.quantization || []).join(', ') || '—', yes(r.uncensored),
            r.gated ? String(r.gated) : 'no', String(r.downloads ?? '—'), (r.last_modified || '').slice(0, 10),
            h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { ref.value = r.repository; lookup(r.repository); } }, 'Inspect'),
          ]), { caption: 'Search results', empty: 'No models found.' }));
      } catch (e) { clear(searchEl).append(errorBox(e)); }
    };
    root.append(
      h('p', { class: 'lead' }, 'Install, verify, test and assign models. A production alias only changes after the candidate '
        + 'produced a real answer, and the previous model stays available until you accept the change.'),
      h('section', { class: 'card' }, h('h2', { class: 'card-title' }, 'Find a model'),
        h('form', { class: 'btn-row', onsubmit: (ev) => { ev.preventDefault(); search(); } }, q, h('button', { type: 'submit', class: 'btn' }, 'Search')),
        searchEl,
        h('form', { class: 'btn-row', onsubmit: (ev) => { ev.preventDefault(); lookup(ref.value); } }, ref,
          h('button', { type: 'submit', class: 'btn btn-primary', id: 'mm-lookup' }, 'Inspect')),
        lookupEl),
      h('section', { class: 'card' }, h('h2', { class: 'card-title' }, 'Hugging Face access'), tokenEl),
      jobsEl,
      invEl);
    const jobs = await api.get('/api/manager/jobs');
    for (const j of jobs.jobs.filter((x) => x.state === 'running')) watchJob(j.id);
    await loadInventory();
  },
  unmount() { clear(jobsEl); },
};
