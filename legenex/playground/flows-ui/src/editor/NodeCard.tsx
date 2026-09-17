// A node on the canvas: compact creative card with typed, labelled handles,
// inline key settings, live status, previews and a keyboard-accessible menu.
import { Handle, Position } from '@xyflow/react';
import type { Node, NodeProps } from '@xyflow/react';
import { memo, useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties } from 'react';

import { CATEGORY_TONE, useApp, useEditor, useEditorState } from '../context';
import type { InspectorTab, RunMode } from '../context';
import { nodeName, resolveTypes } from '../model/graph';
import type { NodeRun, NodeTypeSpec, PortSpec } from '../types';
import { Btn, Icon, StatusBadge, seconds } from '../ui';
import { FieldEditor } from './fields';
import { ValuePreview } from './preview';

export type CardNode = Node<Record<string, never>, 'gx'>;

const ACTIVE = new Set(['queued', 'waiting', 'running']);

interface MenuItem { label: string; onSelect: () => void; disabled?: boolean; danger?: boolean }

export function NodeMenu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    listRef.current?.querySelector<HTMLButtonElement>('button:not([disabled])')?.focus();
    const onDoc = (ev: MouseEvent) => {
      if (!listRef.current?.contains(ev.target as globalThis.Node) && ev.target !== buttonRef.current) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => { document.removeEventListener('mousedown', onDoc); };
  }, [open]);
  const onKey = (ev: React.KeyboardEvent) => {
    const buttons = [...(listRef.current?.querySelectorAll<HTMLButtonElement>('button:not([disabled])') ?? [])];
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); setOpen(false); buttonRef.current?.focus(); }
    if (ev.key === 'ArrowDown') { ev.preventDefault(); buttons[(i + 1) % buttons.length]?.focus(); }
    if (ev.key === 'ArrowUp') { ev.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length]?.focus(); }
    if (ev.key === 'Home') { ev.preventDefault(); buttons[0]?.focus(); }
    if (ev.key === 'End') { ev.preventDefault(); buttons[buttons.length - 1]?.focus(); }
    if (ev.key === 'Tab') setOpen(false);
  };
  return (
    <div className="gxf-menu nodrag">
      <button ref={buttonRef} type="button" className="icon-btn icon-btn-ghost gxf-icon-btn" aria-haspopup="menu"
        aria-expanded={open} aria-controls={open ? menuId : undefined} aria-label={label} title={label}
        onClick={(ev) => { ev.stopPropagation(); setOpen((v) => !v); }}>
        <Icon name="more" />
      </button>
      {open ? (
        <ul ref={listRef} id={menuId} role="menu" className="gxf-menu-list" aria-label={label} onKeyDown={onKey}>
          {items.map((item) => (
            <li role="none" key={item.label}>
              <button type="button" role="menuitem" disabled={item.disabled}
                className={item.danger ? 'is-danger' : undefined}
                onClick={(ev) => { ev.stopPropagation(); setOpen(false); item.onSelect(); buttonRef.current?.focus(); }}>
                {item.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function PortRow({ port, dir, nodeId, type, connected, nodeLabel }: {
  port: PortSpec; dir: 'in' | 'out'; nodeId: string; type: string | null; connected: boolean; nodeLabel: string;
}) {
  const shown = type ?? (port.types.includes('any') ? 'any' : port.types.join('/'));
  const req = dir === 'in' && port.required ? ', required' : '';
  const many = dir === 'in' && port.multiple ? ', accepts several' : '';
  return (
    <div className={`gxf-port gxf-port-${dir}`} data-port={port.id}>
      {/* The handle is the pointer target only: @xyflow renders a plain <div>,
          where aria-label is prohibited (WCAG / aria-prohibited-attr). The port
          is named by the visible text next to it, and connecting without a
          pointer goes through the Outline's Connect dialog. */}
      <Handle type={dir === 'in' ? 'target' : 'source'} position={dir === 'in' ? Position.Left : Position.Right}
        id={port.id} className={`gxf-handle gxf-type-${shown.split('/')[0] ?? 'any'}${connected ? ' is-connected' : ''}`}
        title={`${nodeLabel} ${dir === 'in' ? 'input' : 'output'} ${port.label} (${shown}${req}${many})`}
        aria-hidden="true" data-node={nodeId} />
      <span className="gxf-port-label">
        {port.label}
        {dir === 'in' && port.required ? <span aria-hidden="true">*</span> : null}
        <span className="gxf-port-type">{shown}</span>
      </span>
    </div>
  );
}

function runSummary(run: NodeRun | undefined): string {
  if (!run) return '';
  const parts = [];
  if (run.duration_s !== null) parts.push(seconds(run.duration_s));
  if (run.cached) parts.push('from cache');
  return parts.join(' · ');
}

export function menuItems(spec: NodeTypeSpec, nodeId: string, locked: boolean, disabled: boolean, running: boolean,
  nodeRunning: boolean, actions: ReturnType<typeof useEditor>['actions']): MenuItem[] {
  const run = (mode: RunMode) => () => { actions.run(mode, nodeId); };
  const inspect = (tab: InspectorTab) => () => { actions.inspect(nodeId, tab); };
  return [
    { label: 'Run node', onSelect: run('node'), disabled: running || !spec.available },
    { label: 'Run from here', onSelect: run('from'), disabled: running },
    { label: 'Run downstream', onSelect: run('downstream'), disabled: running },
    { label: 'Regenerate (ignore cache)', onSelect: run('regenerate'), disabled: running || !spec.available },
    { label: 'Cancel node', onSelect: () => { actions.cancelNode(nodeId); }, disabled: !nodeRunning },
    { label: 'Connect to…', onSelect: () => { actions.connectFrom(nodeId); }, disabled: !spec.outputs.length },
    { label: 'Inspect settings', onSelect: inspect('settings') },
    { label: 'Inspect logs', onSelect: inspect('logs') },
    { label: 'Inspect payload', onSelect: inspect('payload') },
    { label: 'Duplicate', onSelect: () => { actions.duplicate([nodeId]); } },
    { label: disabled ? 'Enable (stop bypassing)' : 'Bypass', onSelect: () => { actions.toggleDisabled([nodeId]); },
      disabled: locked },
    { label: locked ? 'Unlock' : 'Lock (keep result)', onSelect: () => { actions.toggleLocked([nodeId]); } },
    { label: 'Delete', onSelect: () => { actions.remove([nodeId]); }, danger: true, disabled: locked },
  ];
}

function NodeCardInner({ id, selected }: NodeProps<CardNode>) {
  const { cat, live } = useApp();
  const { store, actions, run, running } = useEditor();
  const state = useEditorState(store);
  const node = state.doc.nodes.find((n) => n.id === id);
  const spec = node ? cat.get(node.type) : undefined;
  if (!node || !spec) return null;
  const name = nodeName(node, cat);
  const nodeRun = run?.nodes?.[id];
  const status = node.disabled ? 'bypassed' : nodeRun?.status ?? 'idle';
  const types = resolveTypes(state.doc, cat);
  const connectedIn = new Set(state.doc.edges.filter((e) => e.target === id).map((e) => e.target_port));
  const connectedOut = new Set(state.doc.edges.filter((e) => e.source === id).map((e) => e.source_port));
  const unavailable = !spec.available ? spec.unavailable_reason : live.get(spec.type);
  const nodeActive = nodeRun ? ACTIVE.has(nodeRun.status) : false;
  const outputs = Object.values(nodeRun?.outputs ?? {}).flat();
  const cardFields = spec.fields.filter((f) => f.card);
  const tone = CATEGORY_TONE[spec.category] ?? 'var(--muted)';
  const style = { '--gxf-cat': tone } as CSSProperties;
  const change = (fieldId: string) => (value: unknown) => {
    actions.guard(() => { store.updateConfig(id, { [fieldId]: value }); });
  };
  return (
    <div className={['gxf-node', selected ? 'is-selected' : '', node.disabled ? 'is-bypassed' : '',
      node.locked ? 'is-locked' : '', unavailable ? 'is-unavailable' : '', `is-${status}`].filter(Boolean).join(' ')}
    style={style} data-node-id={id} data-node-type={spec.type} data-status={status}>
      <header className="gxf-node-head">
        <span className="gxf-cat-dot" aria-hidden="true" />
        <div className="gxf-node-titles">
          <span className="gxf-node-name">{name}</span>
          <span className="gxf-node-type">{spec.label}</span>
        </div>
        {node.locked ? <Icon name="lock" label="Locked" /> : null}
        <StatusBadge status={status} compact />
        <NodeMenu label={`Actions for ${name}`}
          items={menuItems(spec, id, node.locked, node.disabled, running, nodeActive, actions)} />
      </header>
      {unavailable ? <p className="gxf-node-warn" role="note"><Icon name="warn" /> {unavailable}</p> : null}
      <div className="gxf-ports">
        <div className="gxf-ports-in">
          {spec.inputs.map((p) => <PortRow key={p.id} port={p} dir="in" nodeId={id} type={null}
            connected={connectedIn.has(p.id)} nodeLabel={name} />)}
        </div>
        <div className="gxf-ports-out">
          {spec.outputs.map((p) => <PortRow key={p.id} port={p} dir="out" nodeId={id}
            type={types.get(`${id}:${p.id}`) ?? null} connected={connectedOut.has(p.id)} nodeLabel={name} />)}
        </div>
      </div>
      {cardFields.length ? (
        <div className="gxf-node-body">
          {cardFields.map((f) => <FieldEditor key={f.id} field={f} node={node} place="card"
            disabled={node.locked} connected={connectedIn} onChange={change(f.id)} />)}
        </div>
      ) : null}
      {outputs.length ? (
        <div className="gxf-node-preview">
          {outputs.slice(0, 4).map((v, i) => <ValuePreview key={`${v.asset_id ?? v.type}-${String(i)}`} value={v} compact />)}
          {outputs.length > 4 ? <p className="gxf-muted small">+{outputs.length - 4} more in the Inspector</p> : null}
        </div>
      ) : null}
      {nodeRun && (nodeRun.detail || nodeRun.error) ? (
        <p className={`gxf-node-detail${nodeRun.status === 'failed' ? ' is-error' : ''}`}>
          {nodeRun.error ?? nodeRun.detail}
        </p>
      ) : null}
      {nodeRun?.progress !== null && nodeRun?.progress !== undefined && nodeActive ? (
        <progress className="gxf-progress" max={1} value={nodeRun.progress} aria-label={`${name} progress`} />
      ) : null}
      <footer className="gxf-node-foot">
        {nodeActive ? (
          <Btn size="sm" variant="ghost" icon="stop" className="nodrag" onClick={() => { actions.cancelNode(id); }}>
            Cancel
          </Btn>
        ) : (
          <Btn size="sm" variant="ghost" icon="play" className="nodrag" disabled={running || Boolean(unavailable)}
            onClick={() => { actions.run('node', id); }} aria-label={`Run ${name}`}>Run</Btn>
        )}
        <span className="gxf-node-meta">{runSummary(nodeRun)}</span>
        {nodeRun?.model ? <span className="gxf-node-model" title={nodeRun.model}>{nodeRun.model}</span> : null}
        <Btn size="sm" variant="ghost" icon="inspector" label={`Inspect ${name}`} className="nodrag"
          onClick={() => { actions.inspect(id); }} />
      </footer>
    </div>
  );
}

export const NodeCard = memo(NodeCardInner);
