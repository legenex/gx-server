import { describe, expect, it, vi } from 'vitest';

import { Autosaver, draftKey, readDraft } from '../src/model/autosave';
import { checkConnection, connectOptions, indexCatalog, readiness, resolveTypes, topoOrder, wouldCycle } from '../src/model/graph';
import { EditorStore, HISTORY_LIMIT, LockedError, emptyDoc } from '../src/model/store';
import type { Catalog, FlowDoc, FlowRecord } from '../src/types';
import { HttpError } from '../src/types';
import catalogJson from './fixtures/catalog.json';

const cat = indexCatalog(catalogJson as unknown as Catalog);

function doc(): FlowDoc {
  return {
    ...emptyDoc('Test'),
    nodes: [
      { id: 't', type: 'text.input', label: '', notes: '', position: { x: 0, y: 0 }, config: { text: 'hi' }, disabled: false, locked: false },
      { id: 'g', type: 'image.generate', label: '', notes: '', position: { x: 300, y: 0 }, config: {}, disabled: false, locked: false },
      { id: 'v', type: 'video.i2v', label: '', notes: '', position: { x: 600, y: 0 }, config: {}, disabled: false, locked: false },
      { id: 'd', type: 'util.delay', label: '', notes: '', position: { x: 0, y: 200 }, config: {}, disabled: false, locked: false },
      { id: 'a', type: 'sound.upload', label: '', notes: '', position: { x: 0, y: 400 }, config: {}, disabled: false, locked: false },
    ],
  };
}

describe('graph rules', () => {
  it('orders topologically and detects cycles', () => {
    expect(topoOrder(['a', 'b', 'c'], [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }])).toEqual(['a', 'b', 'c']);
    expect(topoOrder(['a', 'b'], [{ source: 'a', target: 'b' }, { source: 'b', target: 'a' }])).toBeNull();
    const d = doc();
    d.edges.push({ id: 'e1', source: 't', source_port: 'text', target: 'g', target_port: 'prompt' });
    expect(wouldCycle(d, 'g', 't')).toBe(true);
    expect(wouldCycle(d, 't', 'v')).toBe(false);
  });

  it('accepts typed connections and rejects nonsensical ones', () => {
    const d = doc();
    expect(checkConnection(d, cat, 't', 'text', 'g', 'prompt').ok).toBe(true);
    expect(checkConnection(d, cat, 'g', 'image', 'v', 'image').ok).toBe(true);
    const bad = checkConnection(d, cat, 'a', 'audio', 'g', 'prompt');
    expect(bad.ok).toBe(false);
    expect(bad.reason).toContain('accepts text, not audio');
    expect(checkConnection(d, cat, 't', 'text', 't', 'text').ok).toBe(false);
    expect(checkConnection(d, cat, 't', 'nope', 'g', 'prompt').reason).toBe('Unknown port.');
  });

  it('resolves pass-through types through utility nodes', () => {
    const d = doc();
    d.edges.push({ id: 'e1', source: 'a', source_port: 'audio', target: 'd', target_port: 'value' });
    expect(resolveTypes(d, cat).get('d:value')).toBe('audio');
    expect(checkConnection(d, cat, 'd', 'value', 'g', 'prompt').ok).toBe(false);
    const d2 = doc();
    d2.edges.push({ id: 'e1', source: 't', source_port: 'text', target: 'd', target_port: 'value' });
    expect(checkConnection(d2, cat, 'd', 'value', 'g', 'prompt').ok).toBe(true);
  });

  it('enforces single inputs and lists keyboard connection options', () => {
    const d = doc();
    d.edges.push({ id: 'e1', source: 't', source_port: 'text', target: 'g', target_port: 'prompt' });
    d.nodes.push({ ...d.nodes[0]!, id: 't2' });
    expect(checkConnection(d, cat, 't2', 'text', 'g', 'prompt').reason).toContain('single connection');
    const options = connectOptions(d, cat, 'g');
    expect(options.map((o) => `${o.target}:${o.targetPort}`)).toContain('v:image');
    expect(options.every((o) => o.target !== 'a')).toBe(true);
  });

  it('mirrors the server readiness rules', () => {
    const d = doc();
    const issues = readiness(d, cat);
    expect(issues.find((i) => i.node_id === 'g')?.code).toBe('missing_input');
    expect(issues.find((i) => i.node_id === 'v')?.code).toBe('missing_input');
    expect(issues.find((i) => i.node_id === 'a')?.code).toBe('missing_field');
    d.nodes[1]!.config.prompt = 'a lighthouse';
    expect(readiness(d, cat).some((i) => i.node_id === 'g')).toBe(false);
    const live = new Map([['text.input', 'down']]);
    expect(readiness(d, cat, live).find((i) => i.node_id === 't')?.code).toBe('unavailable');
    d.nodes[4]!.disabled = true;
    expect(readiness(d, cat).some((i) => i.node_id === 'a')).toBe(false);
  });
});

describe('editor store', () => {
  it('adds, connects, undoes and redoes', () => {
    const s = new EditorStore(cat);
    const a = s.addNode('text.input', { x: 0, y: 0 });
    const b = s.addNode('image.generate', { x: 300, y: 0 });
    expect(s.getState().selected).toEqual([b]);
    const cfg = s.doc.nodes.find((n) => n.id === b)?.config ?? {};
    expect(cfg.count).toBe(1);
    expect('quality' in cfg).toBe(false);
    expect(s.connect(a, 'text', b, 'prompt').ok).toBe(true);
    expect(s.connect(a, 'text', b, 'negative').ok).toBe(false);
    expect(s.doc.edges).toHaveLength(1);
    s.undo();
    expect(s.doc.edges).toHaveLength(0);
    s.redo();
    expect(s.doc.edges).toHaveLength(1);
    s.deleteNodes([a]);
    expect(s.doc.edges).toHaveLength(0);
    s.undo();
    expect(s.doc.nodes).toHaveLength(2);
    expect(s.doc.edges).toHaveLength(1);
    expect(s.getState().canRedo).toBe(true);
    s.setName('New name');
    expect(s.getState().canRedo).toBe(false);
  });

  it('coalesces typing into one undo step and caps history', () => {
    let now = 1000;
    const s = new EditorStore(cat);
    s.now = () => now;
    const id = s.addNode('text.input', { x: 0, y: 0 });
    for (const text of ['h', 'he', 'hel', 'hello']) {
      now += 100;
      s.updateConfig(id, { text });
    }
    s.undo();
    expect(s.node(id)?.config.text).toBeUndefined();
    for (let i = 0; i < HISTORY_LIMIT + 20; i += 1) {
      now += 5000;
      s.setName(`n${String(i)}`);
    }
    let steps = 0;
    while (s.getState().canUndo) {
      s.undo();
      steps += 1;
    }
    expect(steps).toBe(HISTORY_LIMIT);
  });

  it('records a drag as one step and protects locked nodes', () => {
    const s = new EditorStore(cat);
    const id = s.addNode('text.input', { x: 0, y: 0 });
    s.moveNodes([{ id, x: 10, y: 10 }], { live: true });
    s.moveNodes([{ id, x: 50, y: 60 }], { live: true });
    s.endMove();
    expect(s.node(id)?.position).toEqual({ x: 50, y: 60 });
    s.undo();
    expect(s.node(id)?.position).toEqual({ x: 0, y: 0 });
    s.toggleLocked([id]);
    expect(() => { s.updateConfig(id, { text: 'x' }); }).toThrow(LockedError);
    expect(() => { s.rename(id, 'x'); }).toThrow(LockedError);
    expect(() => { s.toggleDisabled([id]); }).toThrow(LockedError);
    expect(s.deleteNodes([id])).toEqual({ deleted: [], locked: [id] });
    s.toggleLocked([id]);
    s.toggleDisabled([id]);
    expect(s.node(id)?.disabled).toBe(true);
  });

  it('duplicates nodes with their internal connections and serialises', () => {
    const s = new EditorStore(cat);
    const a = s.addNode('text.input', { x: 0, y: 0 }, { text: 'x' });
    const b = s.addNode('image.generate', { x: 300, y: 0 });
    s.connect(a, 'text', b, 'prompt');
    s.toggleLocked([a]);
    const copies = s.duplicateNodes([a, b]);
    expect(copies).toHaveLength(2);
    expect(s.doc.edges).toHaveLength(2);
    expect(s.node(copies[0]!)?.locked).toBe(false);
    const out = s.serialize();
    out.nodes[0]!.label = 'mutated';
    expect(s.doc.nodes[0]!.label).toBe('');
    expect(JSON.parse(JSON.stringify(out))).toEqual(out);
  });

  it('tracks dirty state against saved revisions', () => {
    const s = new EditorStore(cat);
    s.load(doc());
    expect(s.dirty).toBe(false);
    s.setName('x');
    expect(s.dirty).toBe(true);
    s.markSaved(s.getState().revision);
    expect(s.dirty).toBe(false);
    s.setViewport({ x: 10, y: 0, zoom: 1 });
    expect(s.dirty).toBe(true);
    expect(s.getState().canUndo).toBe(true);
  });
});

function fakeTimers() {
  const pending: { fn: () => void; ms: number }[] = [];
  return {
    pending,
    timers: {
      set: (fn: () => void, ms: number) => { const h = { fn, ms }; pending.push(h); return h; },
      clear: (h: unknown) => { const i = pending.indexOf(h as { fn: () => void; ms: number }); if (i >= 0) pending.splice(i, 1); },
    },
    fire: async () => { const h = pending.shift(); h?.fn(); await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); },
  };
}

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => { data.set(k, v); },
    removeItem: (k: string) => { data.delete(k); },
  };
}

describe('autosave', () => {
  const record = (version: number) => ({ id: 'flow_1', version } as unknown as FlowRecord);

  it('debounces, saves with the version and clears the draft', async () => {
    const s = new EditorStore(cat);
    s.load(doc());
    const save = vi.fn((_doc: FlowDoc, version: number) => Promise.resolve(record(version + 1)));
    const t = fakeTimers();
    const storage = memoryStorage();
    const saver = new Autosaver({ store: s, flowId: 'flow_1', version: 3, save, timers: t.timers, storage });
    s.setName('A');
    s.setName('AB');
    expect(saver.status.state).toBe('unsaved');
    expect(t.pending).toHaveLength(1);
    expect(readDraft(storage, 'flow_1')?.doc.name).toBe('AB');
    await t.fire();
    expect(save).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[0]?.[1]).toBe(3);
    expect(saver.status.state).toBe('saved');
    expect(saver.status.version).toBe(4);
    expect(storage.data.has(draftKey('flow_1'))).toBe(false);
    saver.stop();
  });

  it('keeps a draft and retries with backoff when offline', async () => {
    const s = new EditorStore(cat);
    s.load(doc());
    let fail = true;
    const save = vi.fn(() => (fail ? Promise.reject(new HttpError(0, 'network', 'network'))
      : Promise.resolve(record(2))));
    const t = fakeTimers();
    const storage = memoryStorage();
    const saver = new Autosaver({ store: s, flowId: 'f', version: 1, save, timers: t.timers, storage });
    s.setName('offline edit');
    await t.fire();
    expect(saver.status.state).toBe('offline');
    expect(t.pending[0]?.ms).toBe(2000);
    expect(readDraft(storage, 'f')?.doc.name).toBe('offline edit');
    await t.fire();
    expect(t.pending[0]?.ms).toBe(4000);
    fail = false;
    saver.online();
    await new Promise((r) => setTimeout(r, 0));
    expect(saver.status.state).toBe('saved');
    expect(readDraft(storage, 'f')).toBeNull();
    saver.stop();
  });

  it('stops on a version conflict and reports invalid graphs', async () => {
    const s = new EditorStore(cat);
    s.load(doc());
    const save = vi.fn()
      .mockRejectedValueOnce(new HttpError(409, 'changed elsewhere', 'version_conflict'))
      .mockRejectedValueOnce(new HttpError(422, 'bad edge', 'invalid_flow', [{ message: 'bad edge', code: 'type_mismatch' }]))
      .mockResolvedValue(record(9));
    const t = fakeTimers();
    const saver = new Autosaver({ store: s, flowId: 'f', version: 1, save, timers: t.timers });
    s.setName('x');
    await t.fire();
    expect(saver.status.state).toBe('conflict');
    s.setName('y');
    expect(t.pending).toHaveLength(0);
    await saver.overwrite(7);
    expect(save.mock.calls[1]?.[1]).toBe(7);
    expect(saver.status.state).toBe('invalid');
    expect(saver.status.issues[0]?.code).toBe('type_mismatch');
    s.setName('z');
    await t.fire();
    expect(saver.status.state).toBe('saved');
    saver.stop();
  });
});
