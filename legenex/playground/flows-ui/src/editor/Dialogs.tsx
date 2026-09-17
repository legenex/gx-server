// Dialogs: keyboard connect, templates, AI flow creation, run history,
// versions, flow variables, HTTP secrets and the shortcut list.
import { useEffect, useId, useState } from 'react';

import { useApp, useEditor, useEditorState } from '../context';
import { connectOptions, indexCatalog, nodeName } from '../model/graph';
import type { AiResult, FlowRun, TemplateItem } from '../types';
import { HttpError } from '../types';
import { Btn, Modal, StatusBadge, ago, seconds, when } from '../ui';

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function ConnectDialog({ sourceId, onClose, onDone }: {
  sourceId: string; onClose: () => void; onDone: (message: string) => void;
}) {
  const { cat } = useApp();
  const { store } = useEditor();
  const { doc } = useEditorState(store);
  const source = doc.nodes.find((n) => n.id === sourceId);
  const options = connectOptions(doc, cat, sourceId);
  const [choice, setChoice] = useState(0);
  const groupId = useId();
  if (!source) return null;
  const name = nodeName(source, cat);
  return (
    <Modal title={`Connect ${name}`} onClose={onClose} footer={(
      <>
        <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
        <Btn variant="primary" disabled={!options.length} onClick={() => {
          const o = options[choice];
          if (!o) return;
          const result = store.connect(o.source, o.sourcePort, o.target, o.targetPort);
          onDone(result.ok ? `Connected ${name} ${o.label}` : result.reason);
          onClose();
        }}>Connect</Btn>
      </>
    )}>
      {options.length ? (
        <fieldset className="gxf-radio-list">
          <legend id={groupId}>Choose where the output of “{name}” should go. Only compatible inputs are listed.</legend>
          {options.map((o, i) => (
            <label key={`${o.sourcePort}-${o.target}-${o.targetPort}`} className="gxf-radio">
              <input type="radio" name="gxf-connect" checked={choice === i} onChange={() => { setChoice(i); }} />
              <span>{o.label}{o.type ? <span className="gxf-port-type">{o.type}</span> : null}</span>
            </label>
          ))}
        </fieldset>
      ) : <p>No compatible input is free. Add a node that accepts this output first, or remove a connection.</p>}
    </Modal>
  );
}

export function TemplatesDialog({ onClose, onUse, canSave, onSaveCurrent }: {
  onClose: () => void;
  onUse: (templateId: string, name: string) => Promise<void>;
  canSave: boolean;
  /** Required when `canSave` is true (the editor); the flow browser has no current flow. */
  onSaveCurrent?: (name: string, description: string) => Promise<void>;
}) {
  const { api, host } = useApp();
  const [items, setItems] = useState<TemplateItem[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const nameId = useId();
  const descId = useId();
  const load = () => { api.templates().then(setItems, (e: unknown) => { setError(errorText(e)); }); };
  useEffect(load, [api]);
  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setError('');
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy('');
    }
  };
  return (
    <Modal title="Templates" wide onClose={onClose}>
      {error ? <p className="form-error form-danger" role="alert">{error}</p> : null}
      {!items ? <p aria-busy="true">Loading templates…</p> : (
        <ul className="gxf-template-grid">
          {items.map((t) => (
            <li key={t.id} className="gxf-template" data-template={t.id}>
              <h3>{t.name} {t.builtin ? <span className="badge">Built-in</span> : null}</h3>
              <p className="gxf-muted small">{t.description}</p>
              <p className="gxf-muted xsmall">{t.node_count} nodes · {t.node_types.slice(0, 6).join(', ')}</p>
              <div className="gxf-row">
                <Btn size="sm" variant="primary" disabled={Boolean(busy)}
                  onClick={() => { void act(`use-${t.id}`, () => onUse(t.id, t.name)); }}>
                  {busy === `use-${t.id}` ? 'Creating…' : 'Use template'}
                </Btn>
                <Btn size="sm" disabled={Boolean(busy)} onClick={() => {
                  void act(`dup-${t.id}`, async () => { await api.duplicateTemplate(t.id); load(); host.toast('Template duplicated'); });
                }}>Duplicate</Btn>
                {!t.builtin ? (
                  <Btn size="sm" variant="danger" disabled={Boolean(busy)} onClick={() => {
                    void act(`del-${t.id}`, async () => {
                      if (!await host.confirm({ title: 'Delete template', message: `Delete “${t.name}”? Flows made from it stay.`, okLabel: 'Delete', danger: true })) return;
                      await api.deleteTemplate(t.id);
                      load();
                    });
                  }}>Delete</Btn>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
      {canSave && onSaveCurrent ? (
        <form className="gxf-save-template" onSubmit={(ev) => {
          ev.preventDefault();
          void act('save', async () => { await onSaveCurrent(name.trim(), desc.trim()); setName(''); setDesc(''); load(); });
        }}>
          <h3 className="gxf-sub">Save the current flow as a template</h3>
          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor={nameId}>Template name</label>
              <input id={nameId} className="input" required maxLength={120} value={name}
                onChange={(ev) => { setName(ev.target.value); }} />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={descId}>Description</label>
              <input id={descId} className="input" maxLength={2000} value={desc} onChange={(ev) => { setDesc(ev.target.value); }} />
            </div>
          </div>
          <Btn type="submit" disabled={!name.trim() || Boolean(busy)}>{busy === 'save' ? 'Saving…' : 'Save as template'}</Btn>
        </form>
      ) : null}
    </Modal>
  );
}

const EXAMPLE = 'Create a 30-second MVA Meta ad showing a woman whose BMW was rear-ended. Use a trustworthy female voice, '
  + 'cinematic visuals, subtle background music and finish with a CTA.';

export function AiDialog({ onClose, onCreate }: {
  onClose: () => void; onCreate: (result: AiResult) => Promise<void>;
}) {
  const { api, catalog, options } = useApp();
  const [prompt, setPrompt] = useState('');
  const [model, setModel] = useState('gx-auto');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<AiResult | null>(null);
  const promptId = useId();
  const modelId = useId();
  const cat = indexCatalog(catalog);
  const generate = async () => {
    setBusy(true);
    setError('');
    setResult(null);
    try {
      setResult(await api.aiGenerate(prompt.trim(), model));
    } catch (e) {
      setError(e instanceof HttpError && e.status === 0 ? 'The request timed out or the connection dropped.' : errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="Create a flow with AI" wide onClose={onClose} footer={(
      <>
        <Btn variant="ghost" onClick={onClose}>Close</Btn>
        {result ? <Btn onClick={() => { void generate(); }} disabled={busy}>Try again</Btn> : null}
        {result ? (
          <Btn variant="primary" disabled={busy} onClick={() => {
            setBusy(true);
            onCreate(result).catch((e: unknown) => { setError(errorText(e)); setBusy(false); });
          }}>Create this flow</Btn>
        ) : (
          <Btn variant="primary" icon="sparkles" disabled={busy || prompt.trim().length < 8}
            onClick={() => { void generate(); }}>{busy ? 'Designing…' : 'Generate flow'}</Btn>
        )}
      </>
    )}>
      <div className="stack-sm">
        <div className="field">
          <label className="field-label" htmlFor={promptId}>Describe what the flow should produce</label>
          <textarea id={promptId} className="input textarea" rows={5} maxLength={2000} value={prompt}
            placeholder={EXAMPLE} data-autofocus="" onChange={(ev) => { setPrompt(ev.target.value); }} />
          <div className="gxf-row">
            <Btn size="sm" variant="ghost" onClick={() => { setPrompt(EXAMPLE); }}>Use the example</Btn>
            <span className="gxf-muted small">{prompt.length}/2000</span>
          </div>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={modelId}>Designer model</label>
          <select id={modelId} className="input select" value={model} onChange={(ev) => { setModel(ev.target.value); }}>
            {options.llm_models.filter((m) => m !== 'gx-max').map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
        <p className="gxf-muted small">The graph is designed server-side with the chosen gateway model, checked against the
          node catalogue and the connection rules, and opened for editing. Nothing runs until you press Run.</p>
        <div aria-live="polite">
          {busy && !result ? <p aria-busy="true">Designing the flow… this can take a minute.</p> : null}
          {error ? <p className="form-error form-danger" role="alert">{error}</p> : null}
          {result ? (
            <div className="gxf-ai-result">
              <h3 className="gxf-sub">{result.graph.name}</h3>
              <p className="gxf-muted small">{result.graph.nodes.length} nodes, {result.graph.edges.length} connections ·
                {' '}{result.model}{result.model_used ? ` → ${result.model_used}` : ''} · {result.attempts} attempt(s) · {seconds(result.seconds)}</p>
              <ol className="gxf-ai-nodes">
                {result.graph.nodes.map((n) => <li key={n.id}>{nodeName(n, cat)} <span className="gxf-node-type">{cat.get(n.type)?.label}</span></li>)}
              </ol>
              {result.warnings.length ? (
                <details><summary>{result.warnings.length} adjustment(s)</summary>
                  <ul>{result.warnings.map((w) => <li key={w}>{w}</li>)}</ul></details>
              ) : null}
              {result.readiness.length ? (
                <p className="gxf-note">Before running, fill in: {result.readiness.map((i) => i.message).join('; ')}</p>
              ) : <p className="gxf-note">Ready to run after your review.</p>}
            </div>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}

export function HistoryDialog({ flowId, currentRunId, onClose, onOpen }: {
  flowId: string; currentRunId: string | null; onClose: () => void; onOpen: (run: FlowRun) => void;
}) {
  const { api } = useApp();
  const [runs, setRuns] = useState<FlowRun[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { api.runs(flowId).then(setRuns, (e: unknown) => { setError(errorText(e)); }); }, [api, flowId]);
  return (
    <Modal title="Run history" wide onClose={onClose}>
      {error ? <p className="form-error form-danger" role="alert">{error}</p> : null}
      {!runs ? <p aria-busy="true">Loading…</p> : !runs.length ? <p className="gxf-muted">This flow has not run yet.</p> : (
        <div className="gxf-table-wrap">
          <table className="gxf-table">
            <caption className="sr-only">Runs of this flow, newest first</caption>
            <thead><tr><th scope="col">Started</th><th scope="col">Mode</th><th scope="col">Result</th>
              <th scope="col">Duration</th><th scope="col">Nodes</th><th scope="col">Models</th>
              <th scope="col">Assets</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} aria-current={r.id === currentRunId ? 'true' : undefined}>
                  <td><time dateTime={new Date(r.created_at * 1000).toISOString()}>{when(r.created_at)}</time></td>
                  <td>{r.mode}{r.target_node ? ` (${r.target_node})` : ''}</td>
                  <td><StatusBadge status={r.status === 'succeeded' ? 'succeeded' : r.status} />
                    {r.error ? <div className="gxf-error-text small">{r.error}</div> : null}
                    {r.summary.resource_waits?.length ? <div className="gxf-muted xsmall">waited: {r.summary.resource_waits[0]?.reason}</div> : null}
                  </td>
                  <td>{seconds(r.duration_s)}</td>
                  <td>{Object.entries(r.node_counts ?? {}).map(([k, v]) => `${String(v)} ${k}`).join(', ')}</td>
                  <td className="small">{(r.summary.models ?? []).join('; ') || '—'}</td>
                  <td>{r.summary.assets?.length ?? 0}</td>
                  <td><Btn size="sm" onClick={() => { onOpen(r); onClose(); }}>Show</Btn></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

export function VersionsDialog({ flowId, onClose, onRestore }: {
  flowId: string; onClose: () => void; onRestore: (version: number) => Promise<void>;
}) {
  const { api } = useApp();
  const [items, setItems] = useState<{ version: number; name: string; created_at: number; author: string }[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { api.versions(flowId).then(setItems, (e: unknown) => { setError(errorText(e)); }); }, [api, flowId]);
  return (
    <Modal title="Versions" onClose={onClose}>
      {error ? <p className="form-error form-danger" role="alert">{error}</p> : null}
      {!items ? <p aria-busy="true">Loading…</p> : (
        <ol className="gxf-versions">
          {items.map((v, i) => (
            <li key={v.version}>
              <span>v{v.version} · {v.name} · {ago(v.created_at)} · {v.author}</span>
              {i === 0 ? <span className="badge">current</span> : (
                <Btn size="sm" onClick={() => { onRestore(v.version).catch((e: unknown) => { setError(errorText(e)); }); }}>
                  Restore
                </Btn>
              )}
            </li>
          ))}
        </ol>
      )}
    </Modal>
  );
}

export function VariablesDialog({ onClose }: { onClose: () => void }) {
  const { store } = useEditor();
  const { doc } = useEditorState(store);
  const [rows, setRows] = useState(Object.entries(doc.variables).map(([key, value]) => ({ key, value })));
  const [error, setError] = useState('');
  const save = () => {
    const out: Record<string, string> = {};
    for (const r of rows) {
      if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(r.key)) { setError(`“${r.key}” is not a valid name (letters, digits, _).`); return; }
      if (r.key in out) { setError(`“${r.key}” is used twice.`); return; }
      out[r.key] = r.value.slice(0, 2000);
    }
    store.setVariables(out);
    onClose();
  };
  return (
    <Modal title="Flow variables" onClose={onClose} footer={(
      <><Btn variant="ghost" onClick={onClose}>Cancel</Btn><Btn variant="primary" onClick={save}>Save variables</Btn></>
    )}>
      <p className="gxf-muted small">Use them in prompts as {'{{name}}'}.</p>
      {error ? <p className="form-error form-danger" role="alert">{error}</p> : null}
      <div className="gxf-pairs">
        {rows.map((r, i) => (
          <div className="gxf-pair" key={String(i)}>
            <input className="input" aria-label={`Variable name ${String(i + 1)}`} value={r.key}
              onChange={(ev) => { setRows(rows.map((x, j) => (j === i ? { ...x, key: ev.target.value } : x))); }} />
            <input className="input" aria-label={`Variable value ${String(i + 1)}`} value={r.value}
              onChange={(ev) => { setRows(rows.map((x, j) => (j === i ? { ...x, value: ev.target.value } : x))); }} />
            <Btn size="sm" variant="ghost" icon="trash" label={`Remove variable ${String(i + 1)}`}
              onClick={() => { setRows(rows.filter((_, j) => j !== i)); }} />
          </div>
        ))}
        <Btn size="sm" icon="plus" disabled={rows.length >= 64}
          onClick={() => { setRows([...rows, { key: '', value: '' }]); }}>Add variable</Btn>
      </div>
    </Modal>
  );
}

export function SecretsDialog({ onClose }: { onClose: () => void }) {
  const { api } = useApp();
  const [items, setItems] = useState<{ name: string; updated_at: number; length: number }[] | null>(null);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const nameId = useId();
  const valueId = useId();
  const load = () => { api.secrets().then(setItems, (e: unknown) => { setError(errorText(e)); }); };
  useEffect(load, [api]);
  return (
    <Modal title="HTTP secrets" onClose={onClose}>
      <p className="gxf-muted small">Webhook and API Request nodes reference these by name in their headers. Values are
        stored on gx10-01 (mode 0600) and are never shown again or sent to the browser.</p>
      {error ? <p className="form-error form-danger" role="alert">{error}</p> : null}
      {!items ? <p aria-busy="true">Loading…</p> : (
        <ul className="gxf-versions">
          {items.map((s) => (
            <li key={s.name}>
              <code>{s.name}</code> <span className="gxf-muted small">updated {ago(s.updated_at)}</span>
              <Btn size="sm" variant="danger" onClick={() => {
                api.deleteSecret(s.name).then(load, (e: unknown) => { setError(errorText(e)); });
              }} aria-label={`Delete secret ${s.name}`}>Delete</Btn>
            </li>
          ))}
          {!items.length ? <li className="gxf-muted">No secrets stored.</li> : null}
        </ul>
      )}
      <form className="stack-sm" autoComplete="off" onSubmit={(ev) => {
        ev.preventDefault();
        setError('');
        api.setSecret(name.trim(), value).then(() => { setName(''); setValue(''); load(); },
          (e: unknown) => { setError(errorText(e)); });
      }}>
        <div className="grid-2">
          <div className="field">
            <label className="field-label" htmlFor={nameId}>Name</label>
            <input id={nameId} className="input" value={name} pattern="[A-Za-z][A-Za-z0-9_\-]{0,63}" required
              onChange={(ev) => { setName(ev.target.value); }} />
          </div>
          <div className="field">
            <label className="field-label" htmlFor={valueId}>Value</label>
            <input id={valueId} className="input" type="password" value={value} required maxLength={4096}
              autoComplete="new-password" onChange={(ev) => { setValue(ev.target.value); }} />
          </div>
        </div>
        <Btn type="submit" disabled={!name.trim() || !value}>Store secret</Btn>
      </form>
    </Modal>
  );
}

export const SHORTCUTS: [string, string][] = [
  ['Ctrl + Z / Ctrl + Shift + Z (Ctrl + Y)', 'Undo / redo'],
  ['Ctrl + S', 'Save now'],
  ['Ctrl + Enter', 'Run the selected node'],
  ['Ctrl + Shift + Enter', 'Run the whole flow'],
  ['Delete / Backspace', 'Delete the selection'],
  ['Ctrl + D', 'Duplicate the selection'],
  ['C', 'Connect the selected node (keyboard connection dialog)'],
  ['I or Enter', 'Open the Inspector for the selected node'],
  ['B', 'Bypass / enable the selection'],
  ['L', 'Lock / unlock the selection'],
  ['F', 'Fit the flow in view'],
  ['/', 'Search the node library'],
  ['O', 'Toggle the outline view'],
  ['Tab / Shift + Tab', 'Move between nodes and controls'],
  ['Arrow keys', 'Move the selected node (canvas)'],
  ['Escape', 'Close the Inspector or a dialog'],
  ['?', 'Show this list'],
];

export function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Keyboard shortcuts" onClose={onClose}>
      <table className="gxf-table">
        <caption className="sr-only">Shortcuts on the Creative Flows canvas</caption>
        <thead><tr><th scope="col">Keys</th><th scope="col">Action</th></tr></thead>
        <tbody>{SHORTCUTS.map(([k, v]) => <tr key={k}><td><kbd>{k}</kbd></td><td>{v}</td></tr>)}</tbody>
      </table>
      <p className="gxf-muted small">On macOS use ⌘ instead of Ctrl.</p>
    </Modal>
  );
}
