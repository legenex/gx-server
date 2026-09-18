// Client-side mirror of the server's typed-edge and readiness rules
// (gx_control_ui/flows/schema.py). The server stays authoritative; this gives
// immediate feedback while connecting and editing.
import type { Catalog, FieldSpec, FlowDoc, FlowEdge, FlowNode, Issue, NodeTypeSpec, PortSpec } from '../types';

export type CatalogIndex = Map<string, NodeTypeSpec>;

export function indexCatalog(catalog: Catalog): CatalogIndex {
  return new Map(catalog.nodes.map((n) => [n.type, n]));
}

export function topoOrder(nodeIds: string[], edges: Pick<FlowEdge, 'source' | 'target'>[]): string[] | null {
  const indeg = new Map(nodeIds.map((id) => [id, 0]));
  const succ = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  for (const e of edges) {
    if (!indeg.has(e.source) || !indeg.has(e.target)) continue;
    succ.get(e.source)?.push(e.target);
    indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1);
  }
  const ready = nodeIds.filter((id) => indeg.get(id) === 0);
  const order: string[] = [];
  while (ready.length) {
    const id = ready.shift() as string;
    order.push(id);
    for (const m of succ.get(id) ?? []) {
      const d = (indeg.get(m) ?? 0) - 1;
      indeg.set(m, d);
      if (d === 0) ready.push(m);
    }
  }
  return order.length === nodeIds.length ? order : null;
}

export function wouldCycle(doc: Pick<FlowDoc, 'nodes' | 'edges'>, source: string, target: string): boolean {
  if (source === target) return true;
  return topoOrder(doc.nodes.map((n) => n.id), [...doc.edges, { source, target }]) === null;
}

/** Resolved type of every output port ("any" follows the input it mirrors). */
export function resolveTypes(doc: Pick<FlowDoc, 'nodes' | 'edges'>, cat: CatalogIndex): Map<string, string | null> {
  const out = new Map<string, string | null>();
  const order = topoOrder(doc.nodes.map((n) => n.id), doc.edges) ?? doc.nodes.map((n) => n.id);
  const byId = new Map(doc.nodes.map((n) => [n.id, n]));
  for (const id of order) {
    const node = byId.get(id);
    const nt = node ? cat.get(node.type) : undefined;
    if (!node || !nt) continue;
    for (const p of nt.outputs) {
      let t: string | null = p.types[0] ?? null;
      if (t === 'any') {
        t = null;
        if (nt.type === 'util.file_input') {
          const kind = node.config.asset_type;
          t = typeof kind === 'string' && kind ? kind : null;
        } else if (p.same_as) {
          for (const e of doc.edges) {
            if (e.target === id && e.target_port === p.same_as) {
              const src = out.get(`${e.source}:${e.source_port}`);
              if (src) { t = src; break; }
            }
          }
        }
      }
      out.set(`${id}:${p.id}`, t);
    }
  }
  return out;
}

export function portOf(nt: NodeTypeSpec | undefined, id: string, dir: 'in' | 'out'): PortSpec | undefined {
  return (dir === 'in' ? nt?.inputs : nt?.outputs)?.find((p) => p.id === id);
}

export interface EdgeCheck {
  ok: boolean;
  reason: string;
  /** The edge already exists: the request is satisfied, so nothing is added and
   *  nothing is reported. Asking twice for the same connection is not an error. */
  duplicate?: boolean;
}

/** A catalogue node that turns `from` into any of `to`, or null when none exists. */
export function conversionNode(cat: CatalogIndex, from: string, to: readonly string[]): string | null {
  for (const node of cat.values()) {
    const takes = node.inputs.some((p) => p.types.includes(from as never));
    const gives = node.outputs.some((p) => p.types.some((t) => to.includes(t)));
    if (takes && gives) return node.label || node.type;
  }
  return null;
}

/** Can source:sourcePort connect to target:targetPort? */
export function checkConnection(doc: Pick<FlowDoc, 'nodes' | 'edges'>, cat: CatalogIndex, source: string,
  sourcePort: string, target: string, targetPort: string, replacing?: string): EdgeCheck {
  const src = doc.nodes.find((n) => n.id === source);
  const dst = doc.nodes.find((n) => n.id === target);
  if (!src || !dst) return { ok: false, reason: 'That node no longer exists.' };
  if (source === target) return { ok: false, reason: 'A node cannot be connected to itself.' };
  const sPort = portOf(cat.get(src.type), sourcePort, 'out');
  const tPort = portOf(cat.get(dst.type), targetPort, 'in');
  if (!sPort || !tPort) return { ok: false, reason: 'Unknown port.' };
  const edges = doc.edges.filter((e) => e.id !== replacing);
  // An exact duplicate is idempotent, not a failure: the end state the user asked
  // for already holds. Rejecting it made isValidConnection() paint the handle
  // invalid and raised a toast on every stray click-to-connect.
  if (edges.some((e) => e.source === source && e.source_port === sourcePort && e.target === target
    && e.target_port === targetPort)) {
    return { ok: true, reason: '', duplicate: true };
  }
  if (!tPort.multiple && edges.some((e) => e.target === target && e.target_port === targetPort)) {
    return { ok: false, reason: `"${tPort.label}" takes a single connection. Remove the existing one first.` };
  }
  const type = resolveTypes({ nodes: doc.nodes, edges }, cat).get(`${source}:${sourcePort}`) ?? null;
  if (type !== null && !tPort.types.includes(type as never)) {
    // Only advise a conversion when one actually exists. Telling someone to "add a
    // conversion node" for image -> text, which nothing in the catalogue can do,
    // sends them looking for a node that was never built.
    const via = conversionNode(cat, type, tPort.types);
    return {
      ok: false,
      reason: `${tPort.label} accepts ${tPort.types.join(' or ')}, not ${type}.`
        + (via ? ` Add a "${via}" node in between.` : ''),
    };
  }
  if (wouldCycle({ nodes: doc.nodes, edges }, source, target)) {
    return { ok: false, reason: 'That connection would create a loop; flows must be acyclic.' };
  }
  return { ok: true, reason: '' };
}

export interface ConnectOption {
  source: string; sourcePort: string; target: string; targetPort: string; label: string; type: string | null;
}

/** Every valid connection from one node's outputs (keyboard "Connect" dialog). */
export function connectOptions(doc: FlowDoc, cat: CatalogIndex, source: string): ConnectOption[] {
  const src = doc.nodes.find((n) => n.id === source);
  const nt = src ? cat.get(src.type) : undefined;
  if (!src || !nt) return [];
  const types = resolveTypes(doc, cat);
  const out: ConnectOption[] = [];
  for (const sp of nt.outputs) {
    for (const dst of doc.nodes) {
      const dnt = cat.get(dst.type);
      for (const tp of dnt?.inputs ?? []) {
        const check = checkConnection(doc, cat, source, sp.id, dst.id, tp.id);
        if (!check.ok || check.duplicate) continue;
        out.push({ source, sourcePort: sp.id, target: dst.id, targetPort: tp.id, type: types.get(`${source}:${sp.id}`) ?? null,
          label: `${sp.label} → ${nodeName(dst, cat)}: ${tp.label}` });
      }
    }
  }
  return out;
}

export function nodeName(node: FlowNode, cat: CatalogIndex): string {
  return node.label || cat.get(node.type)?.label || node.type;
}

function empty(v: unknown): boolean {
  return v === undefined || v === null || (typeof v === 'string' && !v.trim()) || (Array.isArray(v) && !v.length);
}

const NEEDS_TEXT = new Set(['image.generate', 'image.edit', 'video.generate', 'video.t2v', 'voice.tts',
  'voice.dialogue', 'compose.captions', 'compose.subtitles', 'ai.script_writer']);

/** Mirror of schema.readiness(): what stops nodes from running. */
export function readiness(doc: FlowDoc, cat: CatalogIndex, live: Map<string, string> = new Map()): Issue[] {
  const issues: Issue[] = [];
  const connected = new Set(doc.edges.map((e) => `${e.target}:${e.target_port}`));
  for (const node of doc.nodes) {
    if (node.disabled) continue;
    const nt = cat.get(node.type);
    if (!nt) continue;
    const name = nodeName(node, cat);
    const reason = !nt.available ? nt.unavailable_reason : live.get(nt.type);
    if (reason) {
      issues.push({ node_id: node.id, code: 'unavailable', message: `${name} cannot run: ${reason}` });
      continue;
    }
    const fills = new Map<string, FieldSpec>(nt.fields.filter((f) => f.fills).map((f) => [f.fills as string, f]));
    for (const p of nt.inputs) {
      if (connected.has(`${node.id}:${p.id}`)) continue;
      const filler = fills.get(p.id);
      if (filler && !empty(node.config[filler.id])) continue;
      if (p.required || (filler && (filler.required || NEEDS_TEXT.has(nt.type)))) {
        issues.push({ node_id: node.id, code: 'missing_input', port: p.id,
          message: `${name}: connect '${p.label}'${filler ? ` or fill in '${filler.label}'` : ''}` });
      }
    }
    for (const f of nt.fields) {
      if (f.fills || !f.required) continue;
      const v = node.config[f.id] ?? f.default;
      if (empty(v)) {
        issues.push({ node_id: node.id, code: 'missing_field', field: f.id, message: `${name}: '${f.label}' is required` });
      }
    }
  }
  return issues;
}

let counter = 0;

export function newId(prefix: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const base = prefix.replace(/[^A-Za-z0-9_-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24) || 'node';
  for (;;) {
    counter += 1;
    const id = `${base}_${counter.toString(36)}`;
    if (!used.has(id)) return id;
  }
}

export function defaultConfig(nt: NodeTypeSpec): Record<string, unknown> {
  const cfg: Record<string, unknown> = {};
  for (const f of nt.fields) {
    if (f.default !== null && f.default !== undefined && f.default !== '' && !(Array.isArray(f.default) && !f.default.length)) {
      cfg[f.id] = f.default;
    }
  }
  return cfg;
}

export function edgeType(doc: FlowDoc, cat: CatalogIndex, edge: FlowEdge): string | null {
  return resolveTypes(doc, cat).get(`${edge.source}:${edge.source_port}`) ?? null;
}
