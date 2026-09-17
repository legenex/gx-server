// Shared types for the Creative Flows island. The node catalogue itself is
// served by the backend (GET /api/flows/catalog); nothing here hard-codes
// node types.

export type PortType = 'text' | 'json' | 'image' | 'video' | 'audio' | 'voice' | 'lora' | 'any';

export interface PortSpec {
  id: string;
  label: string;
  types: PortType[];
  required: boolean;
  multiple: boolean;
  same_as: string | null;
}

export type FieldKind =
  | 'text' | 'textarea' | 'select' | 'number' | 'boolean' | 'asset' | 'seed' | 'keyvalue' | 'tags' | 'headers';

export interface FieldOption { value: string; label: string }

export interface FieldSpec {
  id: string;
  label: string;
  kind: FieldKind;
  default: unknown;
  required: boolean;
  help?: string;
  options: FieldOption[];
  source?: string;
  min?: number;
  max?: number;
  step?: number;
  integer?: boolean;
  max_length?: number;
  asset_type?: 'image' | 'video' | 'audio';
  pattern?: string;
  placeholder?: string;
  card?: boolean;
  fills?: string;
}

export interface NodeTypeSpec {
  type: string;
  label: string;
  category: string;
  description: string;
  service: string;
  version: number;
  aliases: string[];
  inputs: PortSpec[];
  outputs: PortSpec[];
  fields: FieldSpec[];
  cacheable: boolean;
  available: boolean;
  unavailable_reason: string;
  output_node: boolean;
  backend: string;
  keywords: string[];
}

export interface Catalog {
  version: number;
  port_types: PortType[];
  categories: { id: string; label: string }[];
  nodes: NodeTypeSpec[];
}

export type ConfigValue = unknown;

export interface FlowNode {
  id: string;
  type: string;
  label: string;
  notes: string;
  position: { x: number; y: number };
  config: Record<string, ConfigValue>;
  disabled: boolean;
  locked: boolean;
}

export interface FlowEdge {
  id: string;
  source: string;
  source_port: string;
  target: string;
  target_port: string;
}

export interface FlowDoc {
  schema: number;
  name: string;
  description: string;
  nodes: FlowNode[];
  edges: FlowEdge[];
  variables: Record<string, string>;
  viewport: { x: number; y: number; zoom: number };
}

export interface Issue {
  message: string;
  code: string;
  node_id?: string;
  edge_id?: string;
  field?: string;
  port?: string;
}

export type NodeStatus =
  | 'pending' | 'queued' | 'waiting' | 'running' | 'succeeded' | 'cached' | 'reused' | 'failed' | 'cancelled'
  | 'skipped' | 'bypassed' | 'blocked' | 'interrupted';

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';

export interface PortValue {
  type: string;
  text?: string;
  data?: unknown;
  asset_id?: string;
  voice_id?: string;
  preset_id?: string;
  name?: string;
}

export interface NodeRun {
  node_id: string;
  node_type: string;
  status: NodeStatus;
  detail: string;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  model: string | null;
  progress: number | null;
  cached: boolean;
  outputs: Record<string, PortValue[]>;
  jobs: { kind: string; id: string; at: number }[];
  resource: { code?: string; reason?: string; [key: string]: unknown } | null;
  duration_s: number | null;
  log_count: number;
}

export interface NodeRunDetail extends NodeRun {
  logs: { ts: number; msg: string }[];
  payload: Record<string, unknown>;
}

export interface RunSummary {
  counts?: Record<string, number>;
  models?: string[];
  assets?: string[];
  final_assets?: string[];
  errors?: { node_id: string | null; message: string | null; code?: string | null }[];
  resource_waits?: { node_id: string; code?: string; reason?: string; at: number }[];
  nodes?: number;
  cached?: number;
  executed?: number;
}

export interface FlowRun {
  id: string;
  flow_id: string;
  flow_version: number;
  mode: string;
  target_node: string | null;
  status: RunStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  parent_run: string | null;
  duration_s: number | null;
  summary: RunSummary;
  nodes?: Record<string, NodeRun>;
  node_counts?: Record<string, number>;
  active?: boolean;
  user?: string;
}

export interface FlowRecord {
  id: string;
  name: string;
  description: string;
  version: number;
  created_at: number;
  updated_at: number;
  template_id: string | null;
  graph: FlowDoc;
  readiness: Issue[];
  last_run: FlowRun | null;
}

export interface FlowListItem {
  id: string;
  name: string;
  description: string;
  version: number;
  updated_at: number;
  node_count: number;
  node_types: string[];
  last_run: { id: string; status: RunStatus; created_at: number } | null;
}

export interface TemplateItem {
  id: string;
  name: string;
  description: string;
  category: string;
  builtin: boolean;
  node_count: number;
  node_types: string[];
  updated_at: number;
}

export interface ImageModelOption {
  id: string;
  label: string;
  family: string;
  operations: string[];
  description: string;
  qualities: string[];
  default_size: string | null;
}

export interface Options {
  image_models: ImageModelOption[];
  image_sizes: Record<string, string[]>;
  edit_modes: Record<string, { id: string; label: string; description: string; strength_applies: boolean }[]>;
  image_default?: { generate: string; edit: string };
  lora_presets: { id: string; name: string; description: string; builtin: boolean; loras: number }[];
  voices: { id: string; name: string; kind: string; description: string }[];
  llm_models: string[];
  errors: Record<string, string>;
}

export interface Asset {
  id: string;
  type: 'image' | 'video' | 'audio';
  title: string | null;
  prompt: string | null;
  url: string;
  thumbnail_url: string;
  download_url: string;
  stream_url?: string;
  duration: number | null;
  width: number | null;
  height: number | null;
  waveform: [number, number][] | null;
  model_alias: string | null;
  flow_id?: string | null;
}

export interface AiResult {
  graph: FlowDoc;
  warnings: string[];
  model: string;
  model_used: string | null;
  attempts: number;
  readiness: Issue[];
  seconds: number;
}

/** What the vanilla Playground shell hands to the island (web/js/pages/flows.js). */
export interface HostRequestOptions { signal?: AbortSignal; timeout?: number }

export interface Host {
  request: (method: 'GET' | 'POST', path: string, body?: unknown, opts?: HostRequestOptions) => Promise<unknown>;
  user: string;
  query: Record<string, string>;
  setQuery: (query: Record<string, string>) => void;
  toast: (message: string, tone?: 'ok' | 'danger' | 'warn' | 'info') => void;
  pickAsset: (opts: { type?: string; title?: string }) => Promise<Asset | null>;
  upload: (file: File, opts: { title?: string }) => Promise<Asset>;
  showAsset: (assetId: string) => void;
  confirm: (opts: { title: string; message: string; okLabel?: string; danger?: boolean }) => Promise<boolean>;
}

export class HttpError extends Error {
  status: number;
  code: string;
  issues: Issue[];

  constructor(status: number, message: string, code: string, issues: Issue[] = []) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.code = code;
    this.issues = issues;
  }
}
