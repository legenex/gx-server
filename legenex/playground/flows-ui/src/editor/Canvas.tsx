// The infinite canvas (@xyflow/react): pan, zoom, fit view, minimap, typed
// connections with curved edges, drag-to-move and drop-to-add. Everything the
// canvas does is also reachable without a pointer (Palette, Outline,
// Connect dialog, keyboard shortcuts).
import {
  Background, BackgroundVariant, Controls, MiniMap, ReactFlow, applyNodeChanges, useReactFlow,
} from '@xyflow/react';
import type {
  Connection, Edge, EdgeChange, FinalConnectionState, IsValidConnection, NodeChange, OnConnectEnd, Viewport,
} from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useApp, useEditor, useEditorState } from '../context';
import { checkConnection, nodeName, resolveTypes } from '../model/graph';
import type { CardNode } from './NodeCard';
import { NodeCard } from './NodeCard';

const nodeTypes = { gx: NodeCard };
export const DRAG_MIME = 'application/x-gx-flow-node';

const ARIA = {
  'node.a11yDescription.default': 'Press Enter or Space to select the node. Use the arrow keys to move a selected node. '
    + 'Press Delete to remove it and C to connect it.',
  'node.a11yDescription.keyboardDisabled': 'Keyboard moving is off.',
  'edge.a11yDescription.default': 'Press Enter or Space to select the connection, then Delete to remove it.',
  'controls.ariaLabel': 'Canvas zoom controls',
  'controls.zoomIn.ariaLabel': 'Zoom in',
  'controls.zoomOut.ariaLabel': 'Zoom out',
  'controls.fitView.ariaLabel': 'Fit the whole flow in view',
  'controls.interactive.ariaLabel': 'Toggle editing',
  'minimap.ariaLabel': 'Mini map of the flow',
  'handle.ariaLabel': 'Connection point',
};

export function Canvas({ onReject, onAddAt, reducedMotion, theme }: {
  onReject: (reason: string) => void;
  onAddAt: (type: string, position: { x: number; y: number }) => void;
  reducedMotion: boolean;
  theme: 'dark' | 'light';
}) {
  const { cat } = useApp();
  const { store, run, actions } = useEditor();
  const state = useEditorState(store);
  const flow = useReactFlow();
  const [rfNodes, setRfNodes] = useState<CardNode[]>([]);
  const dragging = useRef(false);
  const doc = state.doc;

  useEffect(() => {
    setRfNodes((prev) => {
      const byId = new Map(prev.map((n) => [n.id, n]));
      return doc.nodes.map((n) => {
        const old = byId.get(n.id);
        const next: CardNode = {
          id: n.id, type: 'gx', data: {}, position: n.position,
          selected: state.selected.includes(n.id), draggable: !n.locked, deletable: false,
          ariaLabel: `${nodeName(n, cat)} (${cat.get(n.type)?.label ?? n.type})`,
        };
        if (old?.measured) next.measured = old.measured;
        if (dragging.current && old) next.position = old.position;
        return next;
      });
    });
  }, [doc.nodes, state.selected, cat]);

  const edges = useMemo<Edge[]>(() => {
    const types = resolveTypes(doc, cat);
    return doc.edges.map((e) => {
      const t = types.get(`${e.source}:${e.source_port}`) ?? 'any';
      const src = run?.nodes?.[e.source]?.status;
      const active = src === 'running' || run?.nodes?.[e.target]?.status === 'running';
      const srcNode = doc.nodes.find((n) => n.id === e.source);
      const dstNode = doc.nodes.find((n) => n.id === e.target);
      return {
        id: e.id, source: e.source, target: e.target, sourceHandle: e.source_port, targetHandle: e.target_port,
        className: `gxf-edge gxf-type-${t}`, animated: active && !reducedMotion, deletable: true,
        selected: state.selected.includes(e.id),
        ariaLabel: `Connection ${t}: ${srcNode ? nodeName(srcNode, cat) : e.source} to ${dstNode ? nodeName(dstNode, cat) : e.target}`,
      };
    });
  }, [doc, cat, run, reducedMotion, state.selected]);

  const onNodesChange = useCallback((changes: NodeChange<CardNode>[]) => {
    setRfNodes((prev) => applyNodeChanges(changes, prev));
    const moves: { id: string; x: number; y: number }[] = [];
    let stopped = false;
    let selection: string[] | null = null;
    for (const c of changes) {
      if (c.type === 'position' && c.position) {
        moves.push({ id: c.id, x: c.position.x, y: c.position.y });
        if (c.dragging === false) stopped = true;
        dragging.current = Boolean(c.dragging);
      } else if (c.type === 'select') {
        selection ??= [...store.getState().selected.filter((s) => !doc.edges.some((e) => e.id === s))];
        if (c.selected && !selection.includes(c.id)) selection.push(c.id);
        if (!c.selected) selection = selection.filter((s) => s !== c.id);
      }
    }
    if (moves.length) store.moveNodes(moves, { live: true });
    if (stopped) {
      dragging.current = false;
      store.endMove();
    }
    if (selection) store.select(selection);
  }, [store, doc.edges]);

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    const removed = changes.filter((c) => c.type === 'remove').map((c) => c.id);
    if (removed.length) store.disconnect(removed);
    const sel = changes.filter((c) => c.type === 'select');
    if (sel.length) {
      const current = store.getState().selected.filter((s) => doc.nodes.some((n) => n.id === s));
      store.select([...current, ...sel.filter((c) => c.selected).map((c) => c.id)]);
    }
  }, [store, doc.nodes]);

  const isValidConnection = useCallback<IsValidConnection>((c) => Boolean(c.source && c.target && c.sourceHandle
    && c.targetHandle && checkConnection(store.doc, cat, c.source, c.sourceHandle, c.target, c.targetHandle).ok),
  [store, cat]);

  const onConnect = useCallback((c: Connection) => {
    if (!c.sourceHandle || !c.targetHandle) return;
    const result = store.connect(c.source, c.sourceHandle, c.target, c.targetHandle);
    if (!result.ok) onReject(result.reason);
  }, [store, onReject]);

  const onConnectEnd = useCallback<OnConnectEnd>((_ev, cs: FinalConnectionState) => {
    if (cs.isValid !== false || !cs.fromNode || !cs.toNode || !cs.fromHandle?.id || !cs.toHandle?.id) return;
    const from = cs.fromHandle.type === 'source' ? cs.fromNode : cs.toNode;
    const to = cs.fromHandle.type === 'source' ? cs.toNode : cs.fromNode;
    const fromHandle = cs.fromHandle.type === 'source' ? cs.fromHandle.id : cs.toHandle.id;
    const toHandle = cs.fromHandle.type === 'source' ? cs.toHandle.id : cs.fromHandle.id;
    const result = checkConnection(store.doc, cat, from.id, fromHandle, to.id, toHandle);
    if (!result.ok) onReject(result.reason);
  }, [store, cat, onReject]);

  const onMoveEnd = useCallback((_: unknown, vp: Viewport) => { store.setViewport(vp); }, [store]);

  return (
    <div className="gxf-canvas"
      onDragOver={(ev) => { if (ev.dataTransfer.types.includes(DRAG_MIME)) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'copy'; } }}
      onDrop={(ev) => {
        const type = ev.dataTransfer.getData(DRAG_MIME);
        if (!type) return;
        ev.preventDefault();
        onAddAt(type, flow.screenToFlowPosition({ x: ev.clientX, y: ev.clientY }));
      }}>
      <ReactFlow<CardNode>
        nodes={rfNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        onMoveEnd={onMoveEnd}
        onNodeDoubleClick={(_, n) => { actions.inspect(n.id); }}
        defaultViewport={doc.viewport}
        minZoom={0.1}
        maxZoom={2.5}
        deleteKeyCode={null}
        selectionKeyCode="Shift"
        multiSelectionKeyCode={['Meta', 'Control']}
        panOnScroll={false}
        zoomOnDoubleClick={false}
        fitViewOptions={{ padding: 0.2, duration: reducedMotion ? 0 : 300 }}
        ariaLabelConfig={ARIA}
        defaultEdgeOptions={{ type: 'default' }}
        proOptions={{ hideAttribution: false }}
        colorMode={theme}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} />
        <Controls showInteractive={false} position="bottom-left" />
        <MiniMap pannable zoomable position="bottom-right" className="gxf-minimap"
          nodeClassName={(n) => `gxf-mini-${store.node(n.id)?.type.split('.')[0] ?? 'node'}`} />
      </ReactFlow>
    </div>
  );
}
