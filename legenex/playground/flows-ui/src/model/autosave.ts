// Autosave: debounced saves with optimistic concurrency, an offline draft in
// this browser (localStorage) and bounded retries with exponential backoff.
import type { FlowDoc, FlowRecord, Issue } from '../types';
import { HttpError } from '../types';
import type { EditorStore } from './store';

export type SaveState = 'saved' | 'unsaved' | 'saving' | 'offline' | 'conflict' | 'invalid' | 'error';

export interface SaveStatus {
  state: SaveState;
  message: string;
  issues: Issue[];
  savedAt: number | null;
  version: number;
}

export interface Draft { doc: FlowDoc; baseVersion: number; savedAt: number }

export interface AutosaveOptions {
  store: EditorStore;
  flowId: string;
  version: number;
  save: (doc: FlowDoc, version: number) => Promise<FlowRecord>;
  onSaved?: (flow: FlowRecord) => void;
  delay?: number;
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null;
  timers?: { set: (fn: () => void, ms: number) => unknown; clear: (handle: unknown) => void };
  maxBackoff?: number;
}

export const draftKey = (flowId: string): string => `gxpg.flows.draft.${flowId}`;

export function readDraft(storage: AutosaveOptions['storage'], flowId: string): Draft | null {
  try {
    const raw = storage?.getItem(draftKey(flowId));
    if (!raw) return null;
    const draft = JSON.parse(raw) as Draft;
    return draft.doc && Array.isArray(draft.doc.nodes) ? draft : null;
  } catch {
    return null;
  }
}

export class Autosaver {
  private readonly o: Required<Omit<AutosaveOptions, 'onSaved' | 'storage'>> & Pick<AutosaveOptions, 'onSaved' | 'storage'>;
  private timer: unknown = null;
  private inFlight: Promise<void> | null = null;
  private backoff = 0;
  private listeners = new Set<() => void>();
  private unsubscribe: () => void;
  private stopped = false;
  status: SaveStatus;

  constructor(opts: AutosaveOptions) {
    this.o = {
      delay: 1200,
      maxBackoff: 60_000,
      timers: { set: (fn, ms) => setTimeout(fn, ms), clear: (h) => { clearTimeout(h as ReturnType<typeof setTimeout>); } },
      storage: null,
      ...opts,
    };
    this.status = { state: 'saved', message: 'All changes saved', issues: [], savedAt: null, version: opts.version };
    this.unsubscribe = opts.store.subscribe(() => { this.changed(); });
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private set(next: Partial<SaveStatus>): void {
    this.status = { ...this.status, ...next };
    for (const fn of [...this.listeners]) fn();
  }

  private changed(): void {
    if (this.stopped || !this.o.store.dirty) return;
    if (this.status.state === 'conflict') return;
    this.writeDraft();
    if (this.status.state !== 'offline' && this.status.state !== 'saving') {
      this.set({ state: 'unsaved', message: 'Unsaved changes', issues: [] });
    }
    this.schedule(this.status.state === 'offline' ? Math.max(this.backoff, this.o.delay) : this.o.delay);
  }

  private schedule(ms: number): void {
    if (this.timer !== null) this.o.timers.clear(this.timer);
    this.timer = this.o.timers.set(() => {
      this.timer = null;
      void this.flush();
    }, ms);
  }

  private writeDraft(): void {
    try {
      const draft: Draft = { doc: this.o.store.serialize(), baseVersion: this.status.version, savedAt: Date.now() };
      this.o.storage?.setItem(draftKey(this.o.flowId), JSON.stringify(draft));
    } catch {
      // private mode / quota: the server copy is still authoritative
    }
  }

  private clearDraft(): void {
    try {
      this.o.storage?.removeItem(draftKey(this.o.flowId));
    } catch {
      // ignore
    }
  }

  /** Save now (if dirty). Resolves when the attempt is over. */
  flush(): Promise<void> {
    if (this.inFlight) return this.inFlight.then(() => (this.o.store.dirty ? this.flush() : undefined));
    if (this.stopped || !this.o.store.dirty || this.status.state === 'conflict') return Promise.resolve();
    if (this.timer !== null) {
      this.o.timers.clear(this.timer);
      this.timer = null;
    }
    const revision = this.o.store.getState().revision;
    const doc = this.o.store.serialize();
    this.set({ state: 'saving', message: 'Saving…' });
    this.inFlight = this.o.save(doc, this.status.version).then((flow) => {
      this.backoff = 0;
      this.o.store.markSaved(revision);
      this.set({ state: this.o.store.dirty ? 'unsaved' : 'saved', message: this.o.store.dirty ? 'Unsaved changes'
        : 'All changes saved', savedAt: Date.now(), version: flow.version, issues: [] });
      if (!this.o.store.dirty) this.clearDraft();
      this.o.onSaved?.(flow);
      if (this.o.store.dirty) this.schedule(this.o.delay);
    }, (err: unknown) => {
      const e = err instanceof HttpError ? err : new HttpError(0, String(err), 'error');
      if (e.status === 0 || e.status === 502 || e.status === 503) {
        this.backoff = Math.min(this.o.maxBackoff, this.backoff ? this.backoff * 2 : 2000);
        this.set({ state: 'offline', message: 'Offline: changes are kept in this browser and saved when the '
          + 'connection is back', issues: [] });
        this.schedule(this.backoff);
      } else if (e.status === 409 && e.code === 'version_conflict') {
        this.set({ state: 'conflict', message: 'This flow was changed somewhere else. Reload it, or keep your '
          + 'version and save it on top.', issues: [] });
      } else if (e.status === 422 || e.status === 409) {
        this.set({ state: 'invalid', message: e.message, issues: e.issues });
      } else {
        this.set({ state: 'error', message: e.message, issues: e.issues });
      }
    }).finally(() => {
      this.inFlight = null;
    });
    return this.inFlight;
  }

  /** After a conflict: save this browser's version on top of `version`. */
  overwrite(version: number): Promise<void> {
    this.set({ state: 'unsaved', version, message: 'Saving your version…' });
    return this.flush();
  }

  setVersion(version: number): void {
    this.set({ version });
  }

  online(): void {
    if (this.status.state === 'offline') {
      this.backoff = 0;
      void this.flush();
    }
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) this.o.timers.clear(this.timer);
    this.unsubscribe();
    this.listeners.clear();
  }
}
