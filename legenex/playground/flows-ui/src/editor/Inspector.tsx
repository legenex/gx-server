// Right-side Inspector (a bottom sheet on phones): every setting of the
// selected node, its run state (execution time, model used, resource and job
// state), logs, the exact payload and all outputs.
import { useEffect, useId, useRef, useState } from 'react';

import { CATEGORY_TONE, useApp, useEditor, useEditorState } from '../context';
import type { InspectorTab } from '../context';
import { nodeName } from '../model/graph';
import type { NodeRunDetail } from '../types';
import { Btn, Icon, StatusBadge, seconds, when } from '../ui';
import { FieldEditor } from './fields';
import { menuItems } from './NodeCard';
import { ValuePreview } from './preview';

const TABS: { id: InspectorTab; label: string }[] = [
  { id: 'settings', label: 'Settings' }, { id: 'run', label: 'Run' }, { id: 'outputs', label: 'Outputs' },
  { id: 'logs', label: 'Logs' }, { id: 'payload', label: 'Payload' },
];

export function Inspector({ nodeId, tab, onTab, onClose }: {
  nodeId: string; tab: InspectorTab; onTab: (tab: InspectorTab) => void; onClose: () => void;
}) {
  const { cat, api, live } = useApp();
  const { store, actions, run, running } = useEditor();
  const state = useEditorState(store);
  const node = state.doc.nodes.find((n) => n.id === nodeId);
  const spec = node ? cat.get(node.type) : undefined;
  const baseId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [detail, setDetail] = useState<NodeRunDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  const nodeRun = run?.nodes?.[nodeId];
  const runId = run?.id;
  const status = nodeRun?.status;

  useEffect(() => { headingRef.current?.focus(); }, [nodeId]);
  useEffect(() => {
    if (!runId || !nodeRun || (tab !== 'logs' && tab !== 'payload')) { setDetail(null); return undefined; }
    let alive = true;
    api.nodeDetail(runId, nodeId).then((d) => { if (alive) { setDetail(d); setDetailError(''); } },
      (err: unknown) => { if (alive) setDetailError(err instanceof Error ? err.message : String(err)); });
    return () => { alive = false; };
  }, [api, runId, nodeId, tab, status, nodeRun?.log_count, nodeRun]);

  if (!node || !spec) return null;
  const name = nodeName(node, cat);
  const connected = new Set(state.doc.edges.filter((e) => e.target === nodeId).map((e) => e.target_port));
  const outputs = Object.entries(nodeRun?.outputs ?? {});
  const unavailable = !spec.available ? spec.unavailable_reason : live.get(spec.type);
  const nodeActive = nodeRun ? ['queued', 'waiting', 'running'].includes(nodeRun.status) : false;

  const onTabKey = (ev: React.KeyboardEvent) => {
    const i = TABS.findIndex((t) => t.id === tab);
    let next = -1;
    if (ev.key === 'ArrowRight') next = (i + 1) % TABS.length;
    if (ev.key === 'ArrowLeft') next = (i - 1 + TABS.length) % TABS.length;
    if (ev.key === 'Home') next = 0;
    if (ev.key === 'End') next = TABS.length - 1;
    if (next >= 0) {
      ev.preventDefault();
      const t = TABS[next];
      if (t) {
        onTab(t.id);
        document.getElementById(`${baseId}-tab-${t.id}`)?.focus();
      }
    }
  };

  return (
    <aside className="gxf-inspector" aria-labelledby={`${baseId}-title`}
      style={{ ['--gxf-cat' as string]: CATEGORY_TONE[spec.category] ?? 'var(--muted)' }}
      onKeyDown={(ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); onClose(); } }}>
      <div className="gxf-sheet-grip" aria-hidden="true" />
      <header className="gxf-inspector-head">
        <span className="gxf-cat-dot" aria-hidden="true" />
        <div className="gxf-node-titles">
          <h2 id={`${baseId}-title`} ref={headingRef} tabIndex={-1} className="gxf-panel-title">{name}</h2>
          <span className="gxf-node-type">{spec.label} · {spec.backend}</span>
        </div>
        <StatusBadge status={node.disabled ? 'bypassed' : status ?? 'idle'} />
        <button type="button" className="icon-btn icon-btn-ghost" aria-label="Close the inspector" onClick={onClose}>
          <Icon name="close" />
        </button>
      </header>
      <div className="gxf-row gxf-inspector-actions">
        {nodeActive ? (
          <Btn size="sm" icon="stop" onClick={() => { actions.cancelNode(nodeId); }}>Cancel</Btn>
        ) : (
          <Btn size="sm" variant="primary" icon="play" disabled={running || Boolean(unavailable)}
            onClick={() => { actions.run('node', nodeId); }}>Run node</Btn>
        )}
        <Btn size="sm" icon="refresh" disabled={running || Boolean(unavailable)}
          onClick={() => { actions.run('regenerate', nodeId); }}>Regenerate</Btn>
        <Btn size="sm" icon="lock" onClick={() => { actions.toggleLocked([nodeId]); }} aria-pressed={node.locked}>
          {node.locked ? 'Unlock' : 'Lock'}
        </Btn>
        <Btn size="sm" icon="bypass" disabled={node.locked} aria-pressed={node.disabled}
          onClick={() => { actions.toggleDisabled([nodeId]); }}>{node.disabled ? 'Enable' : 'Bypass'}</Btn>
      </div>
      <div role="tablist" aria-label="Inspector sections" className="gxf-tabs" onKeyDown={onTabKey}>
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" id={`${baseId}-tab-${t.id}`}
            aria-controls={`${baseId}-panel`} aria-selected={tab === t.id} tabIndex={tab === t.id ? 0 : -1}
            className={`gxf-tab${tab === t.id ? ' is-active' : ''}`} onClick={() => { onTab(t.id); }}>
            {t.label}
            {t.id === 'outputs' && outputs.length ? <span className="gxf-count">{outputs.reduce((n, [, v]) => n + v.length, 0)}</span> : null}
          </button>
        ))}
      </div>
      <div className="gxf-inspector-body" role="tabpanel" id={`${baseId}-panel`}
        aria-labelledby={`${baseId}-tab-${tab}`} tabIndex={0}>
        {tab === 'settings' ? (
          <div className="stack-sm">
            {unavailable ? <p className="gxf-node-warn" role="note"><Icon name="warn" /> {unavailable}</p> : null}
            <p className="gxf-muted small">{spec.description}</p>
            {node.locked ? <p className="gxf-note">Locked: settings are read-only and the last result is reused.</p> : null}
            <div className="field">
              <label className="field-label" htmlFor={`${baseId}-name`}>Node name</label>
              <input id={`${baseId}-name`} className="input" value={node.label} placeholder={spec.label}
                maxLength={80} disabled={node.locked}
                onChange={(ev) => { actions.guard(() => { store.rename(nodeId, ev.target.value); }); }} />
            </div>
            {spec.fields.map((f) => (
              <FieldEditor key={f.id} field={f} node={node} place="inspector" disabled={node.locked}
                connected={connected} onChange={(v) => { actions.guard(() => { store.updateConfig(nodeId, { [f.id]: v }); }); }} />
            ))}
            <div className="field">
              <label className="field-label" htmlFor={`${baseId}-notes`}>Notes</label>
              <textarea id={`${baseId}-notes`} className="input textarea" rows={3} maxLength={2000}
                value={node.notes} onChange={(ev) => { store.setNotes(nodeId, ev.target.value); }} />
            </div>
            <details className="gxf-more-actions">
              <summary>More actions</summary>
              <ul className="gxf-action-list">
                {menuItems(spec, nodeId, node.locked, node.disabled, running, nodeActive, actions)
                  .filter((m) => !m.label.startsWith('Inspect'))
                  .map((m) => (
                    <li key={m.label}>
                      <button type="button" className={`btn btn-sm ${m.danger ? 'btn-danger' : 'btn-ghost'}`}
                        disabled={m.disabled} onClick={m.onSelect}>{m.label}</button>
                    </li>
                  ))}
              </ul>
            </details>
          </div>
        ) : null}
        {tab === 'run' ? (
          nodeRun ? (
            <dl className="gxf-kv">
              <dt>Status</dt><dd><StatusBadge status={nodeRun.status} /> {nodeRun.detail}</dd>
              {nodeRun.error ? <><dt>Error</dt><dd className="gxf-error-text">{nodeRun.error}</dd></> : null}
              <dt>Execution time</dt><dd>{seconds(nodeRun.duration_s)}{nodeRun.cached ? ' (reused from cache)' : ''}</dd>
              <dt>Started</dt><dd>{when(nodeRun.started_at)}</dd>
              <dt>Model used</dt><dd>{nodeRun.model ?? '—'}</dd>
              <dt>Resource state</dt>
              <dd>{nodeRun.resource ? String(nodeRun.resource.reason ?? nodeRun.resource.code ?? '')
                : nodeActive ? 'Admitted' : '—'}</dd>
              <dt>Jobs</dt>
              <dd>{nodeRun.jobs.length ? nodeRun.jobs.map((j) => `${j.kind} ${j.id}`).join(', ') : '—'}</dd>
              <dt>Run</dt><dd>{run ? `${run.mode} · ${run.id}` : '—'}</dd>
            </dl>
          ) : <p className="gxf-muted">This node has not run in the selected run yet.</p>
        ) : null}
        {tab === 'outputs' ? (
          outputs.length ? outputs.map(([port, values]) => (
            <section key={port} className="gxf-output-group">
              <h3 className="gxf-sub">{spec.outputs.find((p) => p.id === port)?.label ?? port} · {values.length}</h3>
              {values.map((v, i) => <ValuePreview key={`${port}-${String(i)}`} value={v} />)}
            </section>
          )) : <p className="gxf-muted">No outputs yet. Run the node to see results here.</p>
        ) : null}
        {tab === 'logs' ? (
          detailError ? <p className="gxf-error-text" role="alert">{detailError}</p>
            : detail?.logs.length ? (
              <ol className="gxf-logs" aria-live="polite">
                {detail.logs.map((l, i) => (
                  <li key={`${String(l.ts)}-${String(i)}`}>
                    <time dateTime={new Date(l.ts * 1000).toISOString()}>{new Date(l.ts * 1000).toLocaleTimeString()}</time>
                    <span>{l.msg}</span>
                  </li>
                ))}
              </ol>
            ) : <p className="gxf-muted">{nodeRun ? 'No log lines yet.' : 'Run the node to see its log.'}</p>
        ) : null}
        {tab === 'payload' ? (
          detail ? <pre className="gxf-json-preview">{JSON.stringify(detail.payload, null, 2)}</pre>
            : <p className="gxf-muted">{nodeRun ? 'Loading…' : 'Run the node to see what was sent.'}</p>
        ) : null}
      </div>
    </aside>
  );
}
