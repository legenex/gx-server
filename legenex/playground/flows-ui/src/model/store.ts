// The editor state: the flow document, selection, undo/redo history and the
// dirty/revision counters the autosave uses. Framework-free (tested with
// vitest); React reads it through useSyncExternalStore.
import type { FlowDoc, FlowEdge, FlowNode } from '../types';
import type { CatalogIndex, EdgeCheck } from './graph';
import { checkConnection, defaultConfig, newId, nodeName } from './graph';

export interface EditorState {
  doc: FlowDoc;
  selected: string[];
  revision: number;
  savedRevision: number;
  canUndo: boolean;
  canRedo: boolean;
}

export const HISTORY_LIMIT = 100;
const COALESCE_MS = 800;

export class LockedError extends Error {
  constructor(name: string) {
    super(`"${name}" is locked. Unlock it to change it.`);
    this.name = 'LockedError';
  }
}

function clone<T>(v: T): T {
  return structuredClone(v);
}

export function emptyDoc(name = 'Untitled flow'): FlowDoc {
  return { schema: 1, name, description: '', nodes: [], edges: [], variables: {}, viewport: { x: 0, y: 0, zoom: 1 } };
}

export class EditorStore {
  private state: EditorState;
  private past: FlowDoc[] = [];
  private future: FlowDoc[] = [];
  private listeners = new Set<() => void>();
  private lastKey = '';
  private lastAt = 0;
  private moveBase: FlowDoc | null = null;
  readonly cat: CatalogIndex;
  now: () => number = () => Date.now();

  constructor(cat: CatalogIndex, doc: FlowDoc = emptyDoc()) {
    this.cat = cat;
    this.state = { doc: clone(doc), selected: [], revision: 0, savedRevision: 0, canUndo: false, canRedo: false };
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getState = (): EditorState => this.state;

  get doc(): FlowDoc {
    return this.state.doc;
  }

  get dirty(): boolean {
    return this.state.revision !== this.state.savedRevision;
  }

  private emit(next: Partial<EditorState>): void {
    this.state = { ...this.state, ...next, canUndo: this.past.length > 0, canRedo: this.future.length > 0 };
    for (const fn of [...this.listeners]) fn();
  }

  /** Apply a change to a copy of the document, recording one undo step. */
  private change(mutate: (doc: FlowDoc) => void, coalesceKey = ''): void {
    const before = this.state.doc;
    const after = clone(before);
    mutate(after);
    const now = this.now();
    const merge = coalesceKey !== '' && coalesceKey === this.lastKey && now - this.lastAt < COALESCE_MS;
    if (!merge) {
      this.past.push(before);
      if (this.past.length > HISTORY_LIMIT) this.past.shift();
    }
    this.lastKey = coalesceKey;
    this.lastAt = now;
    this.future = [];
    this.emit({ doc: after, revision: this.state.revision + 1 });
  }

  load(doc: FlowDoc, { resetHistory = true }: { resetHistory?: boolean } = {}): void {
    if (resetHistory) {
      this.past = [];
      this.future = [];
    }
    const selected = this.state.selected.filter((id) => doc.nodes.some((n) => n.id === id));
    this.emit({ doc: clone(doc), selected, revision: this.state.revision + 1, savedRevision: this.state.revision + 1 });
  }

  markSaved(revision: number): void {
    this.emit({ savedRevision: Math.max(this.state.savedRevision, revision) });
  }

  undo(): void {
    const prev = this.past.pop();
    if (!prev) return;
    this.future.push(this.state.doc);
    this.lastKey = '';
    this.emit({ doc: prev, revision: this.state.revision + 1,
      selected: this.state.selected.filter((id) => prev.nodes.some((n) => n.id === id)) });
  }

  redo(): void {
    const next = this.future.pop();
    if (!next) return;
    this.past.push(this.state.doc);
    this.lastKey = '';
    this.emit({ doc: next, revision: this.state.revision + 1,
      selected: this.state.selected.filter((id) => next.nodes.some((n) => n.id === id)) });
  }

  select(ids: string[]): void {
    const same = ids.length === this.state.selected.length && ids.every((id, i) => this.state.selected[i] === id);
    if (!same) this.emit({ selected: [...ids] });
  }

  node(id: string): FlowNode | undefined {
    return this.state.doc.nodes.find((n) => n.id === id);
  }

  private assertUnlocked(node: FlowNode | undefined): asserts node is FlowNode {
    if (!node) throw new Error('That node no longer exists.');
    if (node.locked) throw new LockedError(nodeName(node, this.cat));
  }

  // ------------------------------------------------------------- document
  setName(name: string): void {
    this.change((d) => { d.name = name.slice(0, 120); }, 'doc:name');
  }

  setDescription(text: string): void {
    this.change((d) => { d.description = text.slice(0, 2000); }, 'doc:description');
  }

  setVariables(vars: Record<string, string>): void {
    this.change((d) => { d.variables = { ...vars }; }, 'doc:variables');
  }

  setViewport(viewport: FlowDoc['viewport']): void {
    const v = this.state.doc.viewport;
    if (Math.abs(v.x - viewport.x) < 0.5 && Math.abs(v.y - viewport.y) < 0.5 && Math.abs(v.zoom - viewport.zoom) < 0.001) return;
    // The view is remembered with the flow but is not an undoable edit.
    this.emit({ doc: { ...this.state.doc, viewport: { ...viewport } }, revision: this.state.revision + 1 });
  }

  // ---------------------------------------------------------------- nodes
  addNode(type: string, position: { x: number; y: number }, config: Record<string, unknown> = {}): string {
    const nt = this.cat.get(type);
    if (!nt) throw new Error(`Unknown node type ${type}`);
    const id = newId(type.split('.').pop() ?? 'node', this.state.doc.nodes.map((n) => n.id));
    const node: FlowNode = { id, type, label: '', notes: '', position: { ...position },
      config: { ...defaultConfig(nt), ...config }, disabled: false, locked: false };
    this.change((d) => { d.nodes.push(node); });
    this.select([id]);
    return id;
  }

  updateConfig(id: string, patch: Record<string, unknown>): void {
    this.assertUnlocked(this.node(id));
    const keys = Object.keys(patch).sort().join(',');
    this.change((d) => {
      const n = d.nodes.find((x) => x.id === id);
      if (!n) return;
      for (const [k, v] of Object.entries(patch)) {
        if (v === undefined || v === null || v === '') Reflect.deleteProperty(n.config, k);
        else n.config[k] = v;
      }
    }, `cfg:${id}:${keys}`);
  }

  rename(id: string, label: string): void {
    this.assertUnlocked(this.node(id));
    this.change((d) => {
      const n = d.nodes.find((x) => x.id === id);
      if (n) n.label = label.trim().slice(0, 80);
    }, `label:${id}`);
  }

  setNotes(id: string, notes: string): void {
    this.change((d) => {
      const n = d.nodes.find((x) => x.id === id);
      if (n) n.notes = notes.slice(0, 2000);
    }, `notes:${id}`);
  }

  /** Live drag: positions change without history; endMove() records one step. */
  moveNodes(moves: { id: string; x: number; y: number }[], { live = false }: { live?: boolean } = {}): void {
    if (!moves.length) return;
    if (live) {
      this.moveBase ??= this.state.doc;
      const doc = clone(this.state.doc);
      for (const m of moves) {
        const n = doc.nodes.find((x) => x.id === m.id);
        if (n) n.position = { x: m.x, y: m.y };
      }
      this.emit({ doc, revision: this.state.revision + 1 });
      return;
    }
    this.change((d) => {
      for (const m of moves) {
        const n = d.nodes.find((x) => x.id === m.id);
        if (n) n.position = { x: m.x, y: m.y };
      }
    });
  }

  endMove(): void {
    if (!this.moveBase) return;
    const base = this.moveBase;
    this.moveBase = null;
    if (JSON.stringify(base.nodes.map((n) => n.position)) === JSON.stringify(this.state.doc.nodes.map((n) => n.position))) {
      return;
    }
    this.past.push(base);
    if (this.past.length > HISTORY_LIMIT) this.past.shift();
    this.future = [];
    this.lastKey = '';
    this.emit({});
  }

  deleteNodes(ids: string[]): { deleted: string[]; locked: string[] } {
    const locked = ids.filter((id) => this.node(id)?.locked);
    const deleted = ids.filter((id) => this.node(id) && !locked.includes(id));
    if (deleted.length) {
      this.change((d) => {
        d.nodes = d.nodes.filter((n) => !deleted.includes(n.id));
        d.edges = d.edges.filter((e) => !deleted.includes(e.source) && !deleted.includes(e.target));
      });
      this.select(this.state.selected.filter((id) => !deleted.includes(id)));
    }
    return { deleted, locked };
  }

  duplicateNodes(ids: string[]): string[] {
    const originals = ids.map((id) => this.node(id)).filter((n): n is FlowNode => Boolean(n));
    if (!originals.length) return [];
    const map = new Map<string, string>();
    const taken = this.state.doc.nodes.map((n) => n.id);
    for (const n of originals) {
      const id = newId(n.type.split('.').pop() ?? 'node', [...taken, ...map.values()]);
      map.set(n.id, id);
    }
    this.change((d) => {
      for (const n of originals) {
        d.nodes.push({ ...clone(n), id: map.get(n.id) as string, locked: false,
          label: n.label ? `${n.label} copy`.slice(0, 80) : '', position: { x: n.position.x + 40, y: n.position.y + 40 } });
      }
      // keep connections between the duplicated nodes
      for (const e of this.state.doc.edges) {
        const s = map.get(e.source);
        const t = map.get(e.target);
        if (s && t) d.edges.push({ ...e, id: newId('e', d.edges.map((x) => x.id)), source: s, target: t });
      }
    });
    const created = [...map.values()];
    this.select(created);
    return created;
  }

  toggleDisabled(ids: string[]): void {
    const nodes = ids.map((id) => this.node(id)).filter((n): n is FlowNode => Boolean(n));
    const lockedNode = nodes.find((n) => n.locked);
    if (lockedNode) throw new LockedError(nodeName(lockedNode, this.cat));
    const value = !nodes.every((n) => n.disabled);
    this.change((d) => { for (const n of d.nodes) if (ids.includes(n.id)) n.disabled = value; });
  }

  toggleLocked(ids: string[]): void {
    const nodes = ids.map((id) => this.node(id)).filter((n): n is FlowNode => Boolean(n));
    const value = !nodes.every((n) => n.locked);
    this.change((d) => { for (const n of d.nodes) if (ids.includes(n.id)) n.locked = value; });
  }

  // ---------------------------------------------------------------- edges
  connect(source: string, sourcePort: string, target: string, targetPort: string): EdgeCheck {
    const check = checkConnection(this.state.doc, this.cat, source, sourcePort, target, targetPort);
    if (!check.ok) return check;
    // Idempotent: the edge is already there, so there is nothing to add and no
    // undo step to record.
    if (check.duplicate) return check;
    const edge: FlowEdge = { id: newId('e', this.state.doc.edges.map((e) => e.id)), source, source_port: sourcePort,
      target, target_port: targetPort };
    this.change((d) => { d.edges.push(edge); });
    return check;
  }

  disconnect(edgeIds: string[]): void {
    if (!edgeIds.length) return;
    this.change((d) => { d.edges = d.edges.filter((e) => !edgeIds.includes(e.id)); });
  }

  reconnect(edgeId: string, source: string, sourcePort: string, target: string, targetPort: string): EdgeCheck {
    const check = checkConnection(this.state.doc, this.cat, source, sourcePort, target, targetPort, edgeId);
    if (!check.ok) return check;
    if (check.duplicate) {
      this.disconnect([edgeId]);
      return check;
    }
    this.change((d) => {
      const edge = d.edges.find((e) => e.id === edgeId);
      if (!edge) return;
      edge.source = source;
      edge.source_port = sourcePort;
      edge.target = target;
      edge.target_port = targetPort;
    });
    return check;
  }

  /** Add a pre-built graph (AI draft or template) next to the existing nodes. */
  replaceDoc(doc: FlowDoc): void {
    this.change((d) => {
      d.name = doc.name;
      d.description = doc.description;
      d.nodes = clone(doc.nodes);
      d.edges = clone(doc.edges);
      d.variables = { ...doc.variables };
    });
  }

  serialize(): FlowDoc {
    return clone(this.state.doc);
  }
}
