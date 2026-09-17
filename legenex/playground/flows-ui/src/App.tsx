// The Creative Flows application: the flow browser and the editor shell
// (toolbar, node library, canvas/outline, inspector, dialogs, autosave and run
// polling). main.tsx mounts this into the Playground page; every server call
// goes through the host's same-origin request function.
import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';

import { FlowsApi, toHttpError } from './api';
import { AppContext, EditorContext, useApp, useEditorState } from './context';
import type { AppCtx, EditorActions, EditorCtx, InspectorTab, RunMode } from './context';
import { Canvas } from './editor/Canvas';
import {
  AiDialog, ConnectDialog, HistoryDialog, SecretsDialog, ShortcutsDialog, TemplatesDialog, VariablesDialog,
  VersionsDialog,
} from './editor/Dialogs';
import { Inspector } from './editor/Inspector';
import { Outline } from './editor/Outline';
import { Palette } from './editor/Palette';
import { Autosaver, readDraft } from './model/autosave';
import type { Draft, SaveState, SaveStatus } from './model/autosave';
import { indexCatalog, nodeName, readiness } from './model/graph';
import type { CatalogIndex } from './model/graph';
import { EditorStore, LockedError } from './model/store';
import type { Catalog, FlowDoc, FlowListItem, FlowRecord, FlowRun, Host, Issue, Options } from './types';
import { Btn, Icon, StatusBadge, ago, seconds, when } from './ui';

// ------------------------------------------------------------------ helpers

function errorText(err: unknown): string {
  return toHttpError(err).message;
}

/** localStorage, or null where it is blocked (private mode, blocked site data). */
function safeStorage(): Storage | null {
  try {
    const s = window.localStorage;
    const probe = '__gxf_probe__';
    s.setItem(probe, '1');
    s.removeItem(probe);
    return s;
  } catch {
    return null;
  }
}

function useTheme(): 'dark' | 'light' {
  const [theme, setTheme] = useState<'dark' | 'light'>(
    () => (document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'));
  useEffect(() => {
    const onTheme = () => {
      setTheme(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');
    };
    window.addEventListener('gx-theme', onTheme);
    return () => { window.removeEventListener('gx-theme', onTheme); };
  }, []);
  return theme;
}

function useReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)';
  const read = () => {
    const pref = document.documentElement.dataset.motion;
    if (pref === 'reduce') return true;
    if (pref === 'no-preference') return false;
    return window.matchMedia(query).matches;
  };
  const [reduced, setReduced] = useState(read);
  useEffect(() => {
    const mql = window.matchMedia(query);
    const update = () => { setReduced(read()); };
    mql.addEventListener('change', update);
    window.addEventListener('gx-preferences', update);
    return () => {
      mql.removeEventListener('change', update);
      window.removeEventListener('gx-preferences', update);
    };
  }, []);
  return reduced;
}

/** Node types whose required choices could not be loaded (voices, LoRA presets). */
function liveIssues(catalog: Catalog, options: Options): Map<string, string> {
  const out = new Map<string, string>();
  const errors = options.errors;
  for (const node of catalog.nodes) {
    for (const field of node.fields) {
      if (!field.required || !field.source) continue;
      const reason = errors[field.source.split(':')[0] ?? ''];
      if (reason) {
        out.set(node.type, `${field.label} could not be loaded: ${reason}`);
        break;
      }
    }
  }
  return out;
}

const SAVE_TONE: Record<SaveState, string> = {
  saved: 'ok', unsaved: 'idle', saving: 'info', offline: 'warn', conflict: 'danger', invalid: 'danger',
  error: 'danger',
};

// ------------------------------------------------------------- flow browser

function FlowsList({ onOpen }: { onOpen: (flowId: string) => void }) {
  const { api, host, announce } = useApp();
  const [q, setQ] = useState('');
  const [items, setItems] = useState<FlowListItem[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [dialog, setDialog] = useState<'' | 'templates' | 'ai'>('');
  const [reload, setReload] = useState(0);
  const searchId = useId();
  const loadedOnce = useRef(false);

  useEffect(() => {
    let alive = true;
    const handle = setTimeout(() => {
      api.list(q.trim()).then((list) => {
        if (!alive) return;
        loadedOnce.current = true;
        setItems(list);
        setError('');
      }, (err: unknown) => { if (alive) setError(errorText(err)); });
    }, loadedOnce.current ? 250 : 0);
    return () => { alive = false; clearTimeout(handle); };
  }, [api, q, reload]);

  const create = (key: string, make: () => Promise<FlowRecord>) => {
    setBusy(key);
    make().then((flow) => {
      setBusy('');
      setDialog('');
      onOpen(flow.id);
    }, (err: unknown) => {
      setBusy('');
      host.toast(errorText(err), 'danger');
    });
  };

  return (
    <div className="gxf-browse">
      <header className="gxf-browse-head">
        <div>
          <h1 tabIndex={-1} className="page-title">Creative Flows</h1>
          <p className="gxf-muted">Chain images, video, voice, music and composition into one repeatable graph.
            Every node runs on this cluster.</p>
        </div>
        <div className="gxf-row">
          <Btn variant="primary" icon="plus" disabled={Boolean(busy)}
            onClick={() => { create('new', () => api.create({ name: 'Untitled flow' })); }}>
            {busy === 'new' ? 'Creating…' : 'New flow'}
          </Btn>
          <Btn icon="template" onClick={() => { setDialog('templates'); }}>From template</Btn>
          <Btn icon="sparkles" onClick={() => { setDialog('ai'); }}>Create with AI</Btn>
        </div>
      </header>

      <div className="gxf-search gxf-browse-search">
        <Icon name="search" />
        <label htmlFor={searchId} className="sr-only">Search flows</label>
        <input id={searchId} className="input" type="search" placeholder="Search flows" value={q}
          autoComplete="off" onChange={(ev) => { setQ(ev.target.value); }} />
      </div>

      {error ? (
        <div className="callout callout-danger" role="alert">
          <strong>Flows could not be loaded</strong>
          <p>{error}</p>
          <Btn onClick={() => { setReload((n) => n + 1); }}>Try again</Btn>
        </div>
      ) : null}

      {!items && !error ? <p aria-busy="true">Loading flows…</p> : null}

      {items?.length === 0 ? (
        <div className="gxf-empty">
          <h2>No flows yet</h2>
          <p className="gxf-muted">Start from a template, describe what you want and let a model draft the graph, or
            build one node at a time.</p>
        </div>
      ) : null}

      {items?.length ? (
        <ul className="gxf-flow-grid">
          {items.map((f) => (
            <li key={f.id} className="gxf-flow-card" data-flow={f.id}>
              <h2 className="gxf-flow-name">
                <button type="button" className="gxf-linkish" onClick={() => { onOpen(f.id); }}>{f.name}</button>
              </h2>
              <p className="gxf-muted small">{f.description || 'No description'}</p>
              <p className="gxf-muted xsmall">{f.node_count} node(s) · v{f.version} · edited {ago(f.updated_at)}</p>
              {f.last_run ? (
                <p className="gxf-flow-run"><StatusBadge status={f.last_run.status} compact /> {ago(f.last_run.created_at)}</p>
              ) : <p className="gxf-muted xsmall">Never run</p>}
              <div className="gxf-row">
                <Btn size="sm" variant="primary" onClick={() => { onOpen(f.id); }}>Open</Btn>
                <Btn size="sm" disabled={Boolean(busy)}
                  onClick={() => { create(`dup-${f.id}`, () => api.duplicate(f.id)); }}>Duplicate</Btn>
                <Btn size="sm" variant="danger" disabled={Boolean(busy)} onClick={() => {
                  void (async () => {
                    const ok = await host.confirm({ title: 'Delete flow',
                      message: `Delete “${f.name}”? Its runs and the media it produced stay in the Library.`,
                      okLabel: 'Delete', danger: true });
                    if (!ok) return;
                    try {
                      await api.remove(f.id);
                      announce(`${f.name} deleted`);
                      setReload((n) => n + 1);
                    } catch (err) {
                      host.toast(errorText(err), 'danger');
                    }
                  })();
                }}>Delete</Btn>
              </div>
            </li>
          ))}
        </ul>
      ) : null}

      {dialog === 'templates' ? (
        <TemplatesDialog onClose={() => { setDialog(''); }} canSave={false}
          onUse={(templateId) => new Promise<void>((resolve, reject) => {
            api.create({ template_id: templateId }).then((flow) => { onOpen(flow.id); resolve(); }, reject);
          })} />
      ) : null}
      {dialog === 'ai' ? (
        <AiDialog onClose={() => { setDialog(''); }}
          onCreate={(result) => new Promise<void>((resolve, reject) => {
            api.create({ graph: result.graph }).then((flow) => { onOpen(flow.id); resolve(); }, reject);
          })} />
      ) : null}
    </div>
  );
}

// -------------------------------------------------------------- the editor

type Dialog = '' | 'templates' | 'ai' | 'history' | 'versions' | 'variables' | 'secrets' | 'shortcuts';

interface EditorProps { record: FlowRecord; onClose: () => void; onOpenFlow: (flowId: string) => void }

function EditorShell({ record, onClose, onOpenFlow }: EditorProps) {
  const { api, cat, host, live, announce } = useApp();
  const flow = useReactFlow();
  const theme = useTheme();
  const reducedMotion = useReducedMotion();
  const [storage] = useState(safeStorage);

  const [store] = useState(() => new EditorStore(cat, record.graph));
  const state = useEditorState(store);
  const [saver] = useState(() => new Autosaver({
    store,
    flowId: record.id,
    version: record.version,
    save: (doc: FlowDoc, version: number) => api.save(record.id, doc, version),
    storage,
  }));
  const [status, setStatus] = useState<SaveStatus>(saver.status);
  const [run, setRun] = useState<FlowRun | null>(record.last_run);
  const [inspect, setInspect] = useState<{ nodeId: string; tab: InspectorTab } | null>(null);
  const [connectFrom, setConnectFrom] = useState('');
  const [dialog, setDialog] = useState<Dialog>('');
  const [view, setView] = useState<'canvas' | 'outline'>('canvas');
  const [paletteOpen, setPaletteOpen] = useState(true);
  const [draft, setDraft] = useState<Draft | null>(() => {
    // A newer draft kept in this browser: an earlier save never reached the server.
    const kept = readDraft(safeStorage(), record.id);
    return kept && kept.savedAt > record.updated_at * 1000 + 1000 ? kept : null;
  });
  const [serverIssues, setServerIssues] = useState<Issue[]>(record.readiness);
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const issuesId = useId();

  const doc = state.doc;
  const running = run !== null && (run.status === 'queued' || run.status === 'running');
  const runId = run?.id ?? null;
  const issues = useMemo(() => readiness(doc, cat, live), [doc, cat, live]);

  // --------------------------------------------------------------- autosave
  useEffect(() => saver.subscribe(() => { setStatus({ ...saver.status }); }), [saver]);
  useEffect(() => () => { saver.stop(); }, [saver]);
  useEffect(() => {
    const online = () => { saver.online(); };
    window.addEventListener('online', online);
    return () => { window.removeEventListener('online', online); };
  }, [saver]);
  useEffect(() => {
    const onUnload = (ev: BeforeUnloadEvent) => {
      if (store.dirty) ev.preventDefault();
    };
    window.addEventListener('beforeunload', onUnload);
    return () => { window.removeEventListener('beforeunload', onUnload); };
  }, [store]);

  // ------------------------------------------------------------ run polling
  useEffect(() => {
    if (runId === null || !running) return undefined;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      api.runState(runId).then((next) => {
        if (!alive) return;
        setRun(next);
        if (next.status === 'queued' || next.status === 'running') timer = setTimeout(tick, 1200);
        else announce(`Run ${next.status}`);
      }, () => {
        if (alive) timer = setTimeout(tick, 3000);
      });
    };
    timer = setTimeout(tick, 900);
    return () => {
      alive = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [api, runId, running, announce]);

  // ---------------------------------------------------------------- actions
  const guard = useCallback((fn: () => void) => {
    try {
      fn();
    } catch (err) {
      host.toast(err instanceof LockedError ? err.message : errorText(err),
        err instanceof LockedError ? 'warn' : 'danger');
    }
  }, [host]);

  const doRun = useCallback((mode: RunMode, nodeId?: string) => {
    void (async () => {
      try {
        await saver.flush();
        const blocked = ['conflict', 'invalid', 'error'].includes(saver.status.state);
        if (blocked) {
          host.toast(`The flow was not started: ${saver.status.message}`, 'danger');
          return;
        }
        const body: { mode: string; node_id?: string; run_id?: string } = { mode };
        if (nodeId) body.node_id = nodeId;
        if (mode === 'rerun_failed' && runId !== null) body.run_id = runId;
        const started = await api.run(record.id, body);
        setRun(started);
        setServerIssues([]);
        announce(nodeId ? `Started ${mode} for one node` : 'Run started');
      } catch (err) {
        const e = toHttpError(err);
        if (e.issues.length) setServerIssues(e.issues);
        host.toast(e.message, 'danger');
      }
    })();
  }, [api, saver, host, record.id, runId, announce]);

  const actions = useMemo<EditorActions>(() => ({
    run: doRun,
    cancelRun: () => {
      if (runId === null) return;
      api.cancel(runId).then(setRun, (err: unknown) => { host.toast(errorText(err), 'danger'); });
    },
    cancelNode: (nodeId: string) => {
      if (runId === null) return;
      api.cancelNode(runId, nodeId).then(setRun, (err: unknown) => { host.toast(errorText(err), 'danger'); });
    },
    inspect: (nodeId: string, tab: InspectorTab = 'settings') => {
      store.select([nodeId]);
      setInspect({ nodeId, tab });
    },
    remove: (ids: string[]) => {
      guard(() => {
        const { deleted, locked } = store.deleteNodes(ids);
        if (locked.length) host.toast(`${locked.length} locked node(s) were kept. Unlock them first.`, 'warn');
        if (deleted.length) {
          announce(`${deleted.length} node(s) deleted`);
          setInspect((cur) => (cur && deleted.includes(cur.nodeId) ? null : cur));
        }
      });
    },
    duplicate: (ids: string[]) => {
      guard(() => {
        const made = store.duplicateNodes(ids);
        announce(`${made.length} node(s) duplicated`);
      });
    },
    toggleDisabled: (ids: string[]) => { guard(() => { store.toggleDisabled(ids); }); },
    toggleLocked: (ids: string[]) => { guard(() => { store.toggleLocked(ids); }); },
    connectFrom: (nodeId: string) => { setConnectFrom(nodeId); },
    pickAsset: (nodeId: string, fieldId: string, type?: string) => {
      host.pickAsset({ type, title: 'Choose from Library' }).then((asset) => {
        if (asset) guard(() => { store.updateConfig(nodeId, { [fieldId]: asset.id }); });
      }, (err: unknown) => { host.toast(errorText(err), 'danger'); });
    },
    uploadAsset: (nodeId: string, fieldId: string, file: File) => {
      host.upload(file, { title: file.name.replace(/\.[^.]+$/, '') }).then((asset) => {
        guard(() => { store.updateConfig(nodeId, { [fieldId]: asset.id }); });
        host.toast('Uploaded to the Library');
      }, (err: unknown) => { host.toast(errorText(err), 'danger'); });
    },
    useOutput: (assetId: string) => {
      api.asset(assetId).then((asset) => {
        guard(() => {
          const count = store.doc.nodes.length;
          const id = store.addNode('util.file_input', { x: 40 + (count % 6) * 70, y: 40 + (count % 9) * 60 },
            { asset_type: asset.type, asset_id: asset.id });
          setInspect({ nodeId: id, tab: 'settings' });
          announce('Added a File Input node with that result');
        });
      }, (err: unknown) => { host.toast(errorText(err), 'danger'); });
    },
    guard,
  }), [api, doRun, guard, host, runId, store, announce]);

  const editorCtx = useMemo<EditorCtx>(() => ({ store, actions, run, running, flowId: record.id }),
    [store, actions, run, running, record.id]);

  // --------------------------------------------------------------- add node
  const addAt = useCallback((type: string, position: { x: number; y: number }) => {
    guard(() => {
      const id = store.addNode(type, position);
      const node = store.node(id);
      announce(`${node ? nodeName(node, cat) : 'Node'} added`);
      setInspect({ nodeId: id, tab: 'settings' });
    });
  }, [guard, store, cat, announce]);

  // Adding from the library places the node to the right of the flow so far
  // (nodes never land on top of each other), then brings it into view.
  const addCentre = useCallback((type: string) => {
    const nodes = store.doc.nodes;
    const rightmost = nodes.reduce<{ x: number; y: number } | null>(
      (best, n) => (best === null || n.position.x > best.x ? { x: n.position.x, y: n.position.y } : best), null);
    const box = canvasRef.current?.getBoundingClientRect();
    const position = rightmost
      ? { x: rightmost.x + 320, y: rightmost.y }
      : box && view === 'canvas'
        ? flow.screenToFlowPosition({ x: box.left + box.width / 2, y: box.top + box.height / 3 })
        : { x: 0, y: 0 };
    addAt(type, position);
    if (view === 'canvas') {
      setTimeout(() => { void flow.fitView({ padding: 0.25, duration: reducedMotion ? 0 : 250 }); }, 0);
    }
  }, [addAt, flow, reducedMotion, store, view]);

  // ------------------------------------------------------------- shortcuts
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const target = ev.target as HTMLElement | null;
      const typing = Boolean(target?.closest('input, textarea, select, [contenteditable="true"]'));
      const mod = ev.ctrlKey || ev.metaKey;
      const selection = store.getState().selected.filter((id) => store.node(id));
      if (mod && ev.key.toLowerCase() === 's') {
        ev.preventDefault();
        void saver.flush();
        announce('Saving');
        return;
      }
      if (mod && ev.key === 'Enter') {
        ev.preventDefault();
        if (!running) doRun(ev.shiftKey || !selection[0] ? 'full' : 'node', ev.shiftKey ? undefined : selection[0]);
        return;
      }
      if (mod && ev.key.toLowerCase() === 'z') {
        ev.preventDefault();
        if (ev.shiftKey) store.redo();
        else store.undo();
        return;
      }
      if (mod && ev.key.toLowerCase() === 'y') {
        ev.preventDefault();
        store.redo();
        return;
      }
      if (mod && ev.key.toLowerCase() === 'd' && selection.length) {
        ev.preventDefault();
        actions.duplicate(selection);
        return;
      }
      if (typing || ev.altKey || mod) return;
      if ((ev.key === 'Delete' || ev.key === 'Backspace') && selection.length) {
        ev.preventDefault();
        actions.remove(selection);
      } else if (ev.key === '/') {
        ev.preventDefault();
        setPaletteOpen(true);
        setTimeout(() => { rootRef.current?.querySelector<HTMLInputElement>('[data-palette-search]')?.focus(); }, 0);
      } else if (ev.key === '?') {
        ev.preventDefault();
        setDialog('shortcuts');
      } else if (ev.key.toLowerCase() === 'f') {
        ev.preventDefault();
        if (view === 'canvas') void flow.fitView({ padding: 0.2, duration: reducedMotion ? 0 : 300 });
      } else if (ev.key.toLowerCase() === 'o') {
        ev.preventDefault();
        setView((v) => (v === 'canvas' ? 'outline' : 'canvas'));
      } else if (selection.length === 1 && selection[0]) {
        const id = selection[0];
        if (ev.key.toLowerCase() === 'c') { ev.preventDefault(); setConnectFrom(id); }
        else if (ev.key.toLowerCase() === 'i' || ev.key === 'Enter') { ev.preventDefault(); actions.inspect(id); }
        else if (ev.key.toLowerCase() === 'b') { ev.preventDefault(); actions.toggleDisabled([id]); }
        else if (ev.key.toLowerCase() === 'l') { ev.preventDefault(); actions.toggleLocked([id]); }
      }
    };
    const root = rootRef.current;
    root?.addEventListener('keydown', onKey);
    return () => { root?.removeEventListener('keydown', onKey); };
  }, [actions, announce, doRun, flow, reducedMotion, running, saver, store, view]);

  // ------------------------------------------------------------------ views
  const leave = () => {
    void saver.flush().finally(onClose);
  };

  const blocking = issues.length > 0;
  const summary = run?.summary;

  return (
    <EditorContext.Provider value={editorCtx}>
      <div className={`gxf-app gxf-view-${view}`} ref={rootRef} data-flow-id={record.id}>
        <h1 className="sr-only" tabIndex={-1}>Creative Flows: {doc.name}</h1>
        <header className="gxf-toolbar">
          <Btn icon="back" label="Back to all flows" onClick={leave} />
          <div className="gxf-title-box">
            <label className="sr-only" htmlFor="gxf-flow-name">Flow name</label>
            <input id="gxf-flow-name" className="gxf-title-input" value={doc.name} maxLength={120}
              aria-label="Flow name"
              onChange={(ev) => { store.setName(ev.target.value); }}
              onBlur={() => { void saver.flush(); }} />
            <span className={`gxf-save gxf-tone-${SAVE_TONE[status.state]}`} data-save-state={status.state}
              role="status">{status.message}</span>
          </div>
          <div className="gxf-toolbar-actions">
            <Btn icon="undo" label="Undo" shortcut="Ctrl+Z" disabled={!state.canUndo} onClick={() => { store.undo(); }} />
            <Btn icon="redo" label="Redo" shortcut="Ctrl+Shift+Z" disabled={!state.canRedo} onClick={() => { store.redo(); }} />
            {running ? (
              <Btn icon="stop" variant="danger" onClick={actions.cancelRun}>Stop</Btn>
            ) : (
              <Btn icon="play" variant="primary" disabled={blocking || !doc.nodes.length}
                aria-describedby={blocking ? issuesId : undefined}
                onClick={() => { doRun('full'); }}>Run flow</Btn>
            )}
            {!running && run?.status === 'failed' ? (
              <Btn icon="refresh" onClick={() => { doRun('rerun_failed'); }}>Run failed again</Btn>
            ) : null}
            <Btn icon="list" aria-pressed={view === 'outline'} shortcut="O"
              onClick={() => { setView((v) => (v === 'canvas' ? 'outline' : 'canvas')); }}>
              {view === 'canvas' ? 'Outline' : 'Canvas'}
            </Btn>
            <Btn icon="fit" label="Fit the flow in view" shortcut="F" disabled={view !== 'canvas'}
              onClick={() => { void flow.fitView({ padding: 0.2, duration: reducedMotion ? 0 : 300 }); }} />
            <Btn icon="plus" aria-pressed={paletteOpen} onClick={() => { setPaletteOpen((v) => !v); }}>Nodes</Btn>
            <Btn icon="template" onClick={() => { setDialog('templates'); }}>Templates</Btn>
            <Btn icon="sparkles" onClick={() => { setDialog('ai'); }}>AI</Btn>
            <Btn icon="history" onClick={() => { setDialog('history'); }}>Runs</Btn>
            <Btn icon="copy" onClick={() => { setDialog('versions'); }}>Versions</Btn>
            <Btn icon="variables" onClick={() => { setDialog('variables'); }}>Variables</Btn>
            <Btn icon="key" onClick={() => { setDialog('secrets'); }}>Secrets</Btn>
            <Btn icon="keyboard" label="Keyboard shortcuts" shortcut="?" onClick={() => { setDialog('shortcuts'); }} />
          </div>
        </header>

        {draft ? (
          <div className="callout callout-warn gxf-banner" role="status">
            <strong>Unsaved changes from this browser</strong>
            <p>A version edited {ago(Math.round(draft.savedAt / 1000))} was never saved to the server.</p>
            <div className="gxf-row">
              <Btn variant="primary" onClick={() => {
                store.replaceDoc(draft.doc);
                setDraft(null);
                announce('Local draft restored');
              }}>Restore it</Btn>
              <Btn onClick={() => { setDraft(null); }}>Keep the server version</Btn>
            </div>
          </div>
        ) : null}

        {status.state === 'conflict' ? (
          <div className="callout callout-danger gxf-banner" role="alert">
            <strong>This flow was changed somewhere else</strong>
            <p>{status.message}</p>
            <div className="gxf-row">
              <Btn onClick={() => {
                api.get(record.id).then((fresh) => {
                  store.load(fresh.graph);
                  saver.setVersion(fresh.version);
                  setServerIssues(fresh.readiness);
                  announce('Reloaded the server version');
                }, (err: unknown) => { host.toast(errorText(err), 'danger'); });
              }}>Reload the server version</Btn>
              <Btn variant="danger" onClick={() => {
                api.get(record.id).then((fresh) => { void saver.overwrite(fresh.version); },
                  (err: unknown) => { host.toast(errorText(err), 'danger'); });
              }}>Keep mine and save on top</Btn>
            </div>
          </div>
        ) : null}

        {status.issues.length || serverIssues.length ? (
          <div className="callout callout-danger gxf-banner" role="alert">
            <strong>The server refused this graph</strong>
            <ul>{[...status.issues, ...serverIssues].map((i) => <li key={`${i.code}-${i.message}`}>{i.message}</li>)}</ul>
          </div>
        ) : null}

        <div className="gxf-body">
          {paletteOpen ? <Palette onAdd={addCentre} onClose={() => { setPaletteOpen(false); }} /> : null}
          <main className="gxf-main" ref={canvasRef}>
            {view === 'canvas' ? (
              <Canvas theme={theme} reducedMotion={reducedMotion} onAddAt={addAt}
                onReject={(reason) => {
                  host.toast(reason, 'warn');
                  announce(reason);
                }} />
            ) : <div className="gxf-outline-wrap"><Outline /></div>}
            {!doc.nodes.length ? (
              <div className="gxf-canvas-empty">
                <h2>Empty flow</h2>
                <p className="gxf-muted">Add a node from the library on the left, start from a template, or let a model
                  draft the graph.</p>
              </div>
            ) : null}
          </main>
          {inspect ? (
            <Inspector nodeId={inspect.nodeId} tab={inspect.tab}
              onTab={(tab) => { setInspect((cur) => (cur ? { ...cur, tab } : cur)); }}
              onClose={() => { setInspect(null); }} />
          ) : null}
        </div>

        <footer className="gxf-statusbar">
          <span>{doc.nodes.length} node(s), {doc.edges.length} connection(s)</span>
          <span id={issuesId} className={blocking ? 'gxf-error-text' : 'gxf-muted'}>
            {blocking ? `${issues.length} thing(s) to fix before running: ${issues[0]?.message ?? ''}` : 'Ready to run'}
          </span>
          {run ? (
            <span className="gxf-run-state">
              <StatusBadge status={run.status} compact /> {run.mode}
              {run.duration_s !== null ? ` · ${seconds(run.duration_s)}` : ''}
              {summary?.errors?.length ? ` · ${summary.errors[0]?.message ?? 'failed'}` : ''}
              {run.finished_at !== null ? ` · ${when(run.finished_at)}` : ''}
            </span>
          ) : <span className="gxf-muted">Not run yet</span>}
        </footer>

        {connectFrom ? (
          <ConnectDialog sourceId={connectFrom} onClose={() => { setConnectFrom(''); }}
            onDone={(message) => { announce(message); host.toast(message); }} />
        ) : null}
        {dialog === 'templates' ? (
          <TemplatesDialog onClose={() => { setDialog(''); }} canSave
            onUse={(templateId) => new Promise<void>((resolve, reject) => {
              api.create({ template_id: templateId }).then((created) => {
                setDialog('');
                onOpenFlow(created.id);
                resolve();
              }, reject);
            })}
            onSaveCurrent={async (name, description) => {
              await saver.flush();
              await api.saveTemplate({ name, description, flow_id: record.id });
              host.toast('Saved as a template');
            }} />
        ) : null}
        {dialog === 'ai' ? (
          <AiDialog onClose={() => { setDialog(''); }}
            onCreate={(result) => new Promise<void>((resolve) => {
              store.replaceDoc(result.graph);
              setDialog('');
              announce(`Replaced the graph with ${result.graph.nodes.length} node(s) from the AI draft`);
              resolve();
            })} />
        ) : null}
        {dialog === 'history' ? (
          <HistoryDialog flowId={record.id} currentRunId={runId} onClose={() => { setDialog(''); }}
            onOpen={(picked) => {
              setRun(picked);
              announce(`Showing run ${picked.id}`);
            }} />
        ) : null}
        {dialog === 'versions' ? (
          <VersionsDialog flowId={record.id} onClose={() => { setDialog(''); }}
            onRestore={async (version) => {
              const restored = await api.restore(record.id, version);
              store.load(restored.graph);
              saver.setVersion(restored.version);
              setServerIssues(restored.readiness);
              setDialog('');
              announce(`Restored version ${version}`);
            }} />
        ) : null}
        {dialog === 'variables' ? <VariablesDialog onClose={() => { setDialog(''); }} /> : null}
        {dialog === 'secrets' ? <SecretsDialog onClose={() => { setDialog(''); }} /> : null}
          {dialog === 'shortcuts' ? <ShortcutsDialog onClose={() => { setDialog(''); }} /> : null}
      </div>
    </EditorContext.Provider>
  );
}

function EditorLoader({ flowId, onClose, onOpenFlow }: {
  flowId: string; onClose: () => void; onOpenFlow: (id: string) => void;
}) {
  const { api } = useApp();
  const [record, setRecord] = useState<FlowRecord | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    api.get(flowId).then((r) => { if (alive) setRecord(r); },
      (err: unknown) => { if (alive) setError(errorText(err)); });
    return () => { alive = false; };
  }, [api, flowId, attempt]);

  if (error) {
    return (
      <div className="callout callout-danger" role="alert">
        <strong>This flow could not be opened</strong>
        <p>{error}</p>
        <div className="gxf-row">
          <Btn onClick={() => { setError(''); setAttempt((n) => n + 1); }}>Try again</Btn>
          <Btn onClick={onClose}>Back to all flows</Btn>
        </div>
      </div>
    );
  }
  if (!record) return <p aria-busy="true">Loading the flow…</p>;
  return (
    <ReactFlowProvider>
      <EditorShell key={record.id} record={record} onClose={onClose} onOpenFlow={onOpenFlow} />
    </ReactFlowProvider>
  );
}

// ----------------------------------------------------------------- the app

export function App({ host }: { host: Host }) {
  const api = useMemo(() => new FlowsApi(host), [host]);
  const [loaded, setLoaded] = useState<{ catalog: Catalog; options: Options; cat: CatalogIndex } | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [flowId, setFlowId] = useState(() => host.query.flow ?? '');
  const [message, setMessage] = useState('');

  useEffect(() => {
    let alive = true;
    Promise.all([api.catalog(), api.options()]).then(([catalog, options]) => {
      if (alive) setLoaded({ catalog, options, cat: indexCatalog(catalog) });
    }, (err: unknown) => { if (alive) setError(errorText(err)); });
    return () => { alive = false; };
  }, [api, attempt]);

  const announce = useCallback((text: string) => {
    setMessage(text);
  }, []);

  const open = useCallback((id: string) => {
    setFlowId(id);
    host.setQuery(id ? { flow: id } : {});
  }, [host]);

  const ctx = useMemo<AppCtx | null>(() => (loaded ? {
    api,
    host,
    catalog: loaded.catalog,
    cat: loaded.cat,
    options: loaded.options,
    live: liveIssues(loaded.catalog, loaded.options),
    announce,
  } : null), [api, host, loaded, announce]);

  if (error) {
    return (
      <div className="callout callout-danger" role="alert">
        <strong>Creative Flows could not start</strong>
        <p>{error}</p>
        <Btn onClick={() => { setError(''); setAttempt((n) => n + 1); }}>Try again</Btn>
      </div>
    );
  }
  if (!ctx) return <p aria-busy="true">Loading the node catalogue…</p>;

  return (
    <AppContext.Provider value={ctx}>
      <p className="sr-only" aria-live="polite">{message}</p>
      {flowId
        ? <EditorLoader key={flowId} flowId={flowId} onClose={() => { open(''); }} onOpenFlow={open} />
        : <FlowsList onOpen={open} />}
    </AppContext.Provider>
  );
}
