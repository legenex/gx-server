import { createContext, useContext, useSyncExternalStore } from 'react';

import type { FlowsApi } from './api';
import type { CatalogIndex } from './model/graph';
import type { EditorState, EditorStore } from './model/store';
import type { Catalog, FlowRun, Host, NodeTypeSpec, Options } from './types';

export interface AppCtx {
  api: FlowsApi;
  host: Host;
  catalog: Catalog;
  cat: CatalogIndex;
  options: Options;
  live: Map<string, string>;
  announce: (message: string) => void;
}

export const AppContext = createContext<AppCtx | null>(null);

export function useApp(): AppCtx {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('AppContext missing');
  return ctx;
}

export type RunMode = 'full' | 'node' | 'from' | 'downstream' | 'rerun_failed' | 'regenerate';

export interface EditorActions {
  run: (mode: RunMode, nodeId?: string) => void;
  cancelRun: () => void;
  cancelNode: (nodeId: string) => void;
  inspect: (nodeId: string, tab?: InspectorTab) => void;
  remove: (ids: string[]) => void;
  duplicate: (ids: string[]) => void;
  toggleDisabled: (ids: string[]) => void;
  toggleLocked: (ids: string[]) => void;
  connectFrom: (nodeId: string) => void;
  pickAsset: (nodeId: string, fieldId: string, type?: string) => void;
  uploadAsset: (nodeId: string, fieldId: string, file: File) => void;
  useOutput: (assetId: string) => void;
  guard: (fn: () => void) => void;
}

export type InspectorTab = 'settings' | 'run' | 'logs' | 'payload' | 'outputs';

export interface EditorCtx {
  store: EditorStore;
  actions: EditorActions;
  run: FlowRun | null;
  running: boolean;
  flowId: string;
}

export const EditorContext = createContext<EditorCtx | null>(null);

export function useEditor(): EditorCtx {
  const ctx = useContext(EditorContext);
  if (!ctx) throw new Error('EditorContext missing');
  return ctx;
}

export function useEditorState(store: EditorStore): EditorState {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

export function specOf(cat: CatalogIndex, type: string): NodeTypeSpec | undefined {
  return cat.get(type);
}

export const CATEGORY_TONE: Record<string, string> = {
  text: 'var(--info)', image: 'var(--accent)', video: 'var(--accent-2)', voice: 'var(--ok)', music: 'var(--warn)',
  sound: 'var(--warn)', compose: 'var(--text-2)', utility: 'var(--muted)',
};
