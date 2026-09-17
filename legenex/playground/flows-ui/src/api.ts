// Typed client for the Creative Flows API. Every response is checked at
// runtime before the UI uses it (the backend is trusted, but a proxy error
// page or a version mismatch must not crash the canvas).
import type {
  AiResult, Catalog, FlowDoc, FlowListItem, FlowRecord, FlowRun, Host, Issue, NodeRunDetail, Options, TemplateItem,
} from './types';
import { HttpError } from './types';

type Json = Record<string, unknown>;

function isObj(v: unknown): v is Json {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function expect<T>(value: unknown, check: (v: Json) => boolean, what: string): T {
  if (!isObj(value) || !check(value)) {
    throw new HttpError(0, `The server sent an unexpected ${what}. Reload the page and try again.`, 'bad_response');
  }
  return value as T;
}

const hasArray = (key: string) => (v: Json) => Array.isArray(v[key]);
const isFlow = (v: Json) => typeof v.id === 'string' && typeof v.version === 'number' && isObj(v.graph)
  && Array.isArray((v.graph as Json).nodes) && Array.isArray((v.graph as Json).edges);
const isRun = (v: Json) => typeof v.id === 'string' && typeof v.status === 'string';

export interface ApiErrorLike { status?: number; code?: string; message?: string; detail?: { issues?: Issue[] } }

export function toHttpError(err: unknown): HttpError {
  if (err instanceof HttpError) return err;
  const e = (isObj(err) ? err : {}) as ApiErrorLike;
  const message = typeof e.message === 'string' ? e.message : String(err);
  return new HttpError(e.status ?? 0, message, e.code ?? 'error', e.detail?.issues ?? []);
}

export class FlowsApi {
  private readonly host: Host;

  constructor(host: Host) {
    this.host = host;
  }

  private async call(method: 'GET' | 'POST', path: string, body?: unknown, timeout?: number): Promise<unknown> {
    try {
      return await this.host.request(method, path, body, timeout ? { timeout } : undefined);
    } catch (err) {
      throw toHttpError(err);
    }
  }

  async catalog(): Promise<Catalog> {
    const data = await this.call('GET', '/api/flows/catalog');
    return expect<Catalog>(data, (v) => Array.isArray(v.nodes) && Array.isArray(v.categories), 'node catalogue');
  }

  async options(): Promise<Options> {
    const data = await this.call('GET', '/api/flows/options');
    return expect<Options>(data, (v) => Array.isArray(v.image_models) && Array.isArray(v.voices), 'options list');
  }

  async list(q = ''): Promise<FlowListItem[]> {
    const data = await this.call('GET', `/api/flows${q ? `?q=${encodeURIComponent(q)}` : ''}`);
    return expect<{ flows: FlowListItem[] }>(data, hasArray('flows'), 'flow list').flows;
  }

  async get(id: string): Promise<FlowRecord> {
    return expect<FlowRecord>(await this.call('GET', `/api/flows/${encodeURIComponent(id)}`), isFlow, 'flow');
  }

  async create(body: { graph?: FlowDoc; template_id?: string; asset_id?: string; name?: string }): Promise<FlowRecord> {
    return expect<FlowRecord>(await this.call('POST', '/api/flows', body), isFlow, 'flow');
  }

  async save(id: string, graph: FlowDoc, version: number): Promise<FlowRecord> {
    return expect<FlowRecord>(await this.call('POST', `/api/flows/${encodeURIComponent(id)}`, { graph, version }),
      isFlow, 'flow');
  }

  async remove(id: string): Promise<void> {
    await this.call('POST', `/api/flows/${encodeURIComponent(id)}/delete`, { confirm: true });
  }

  async duplicate(id: string): Promise<FlowRecord> {
    return expect<FlowRecord>(await this.call('POST', `/api/flows/${encodeURIComponent(id)}/duplicate`, {}), isFlow,
      'flow');
  }

  async versions(id: string): Promise<{ version: number; name: string; created_at: number; author: string }[]> {
    const data = await this.call('GET', `/api/flows/${encodeURIComponent(id)}/versions`);
    return expect<{ versions: { version: number; name: string; created_at: number; author: string }[] }>(
      data, hasArray('versions'), 'version list').versions;
  }

  async restore(id: string, version: number): Promise<FlowRecord> {
    return expect<FlowRecord>(
      await this.call('POST', `/api/flows/${encodeURIComponent(id)}/versions/${String(version)}/restore`, {}),
      isFlow, 'flow');
  }

  async run(id: string, body: { mode: string; node_id?: string; run_id?: string; version?: number }): Promise<FlowRun> {
    return expect<FlowRun>(await this.call('POST', `/api/flows/${encodeURIComponent(id)}/run`, body), isRun, 'run');
  }

  async runs(id: string): Promise<FlowRun[]> {
    const data = await this.call('GET', `/api/flows/${encodeURIComponent(id)}/runs?limit=40`);
    return expect<{ runs: FlowRun[] }>(data, hasArray('runs'), 'run history').runs;
  }

  async runState(runId: string): Promise<FlowRun> {
    return expect<FlowRun>(await this.call('GET', `/api/flow-runs/${encodeURIComponent(runId)}`), isRun, 'run');
  }

  async cancel(runId: string): Promise<FlowRun> {
    return expect<FlowRun>(await this.call('POST', `/api/flow-runs/${encodeURIComponent(runId)}/cancel`, {}), isRun,
      'run');
  }

  async cancelNode(runId: string, nodeId: string): Promise<FlowRun> {
    return expect<FlowRun>(await this.call('POST',
      `/api/flow-runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}/cancel`, {}), isRun, 'run');
  }

  async nodeDetail(runId: string, nodeId: string): Promise<NodeRunDetail> {
    const data = await this.call('GET',
      `/api/flow-runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}`);
    return expect<NodeRunDetail>(data, (v) => Array.isArray(v.logs), 'node detail');
  }

  async templates(): Promise<TemplateItem[]> {
    const data = await this.call('GET', '/api/flows/templates');
    return expect<{ templates: TemplateItem[] }>(data, hasArray('templates'), 'template list').templates;
  }

  async saveTemplate(body: { name: string; description?: string; flow_id?: string; graph?: FlowDoc }):
    Promise<TemplateItem> {
    return expect<TemplateItem>(await this.call('POST', '/api/flows/templates', { category: 'custom', ...body }),
      (v) => typeof v.id === 'string', 'template');
  }

  async duplicateTemplate(id: string): Promise<TemplateItem> {
    return expect<TemplateItem>(await this.call('POST', `/api/flows/templates/${encodeURIComponent(id)}/duplicate`,
      {}), (v) => typeof v.id === 'string', 'template');
  }

  async deleteTemplate(id: string): Promise<void> {
    await this.call('POST', `/api/flows/templates/${encodeURIComponent(id)}/delete`, { confirm: true });
  }

  async aiGenerate(prompt: string, model: string): Promise<AiResult> {
    const data = await this.call('POST', '/api/flows/ai/generate', { prompt, model }, 30 * 60_000);
    return expect<AiResult>(data, (v) => isObj(v.graph) && Array.isArray(v.warnings), 'AI result');
  }

  async secrets(): Promise<{ name: string; updated_at: number; length: number }[]> {
    const data = await this.call('GET', '/api/flows/secrets');
    return expect<{ secrets: { name: string; updated_at: number; length: number }[] }>(
      data, hasArray('secrets'), 'secret list').secrets;
  }

  async setSecret(name: string, value: string): Promise<void> {
    await this.call('POST', '/api/flows/secrets', { name, value });
  }

  async deleteSecret(name: string): Promise<void> {
    await this.call('POST', `/api/flows/secrets/${encodeURIComponent(name)}/delete`, {});
  }

  async asset(id: string): Promise<import('./types').Asset> {
    const data = await this.call('GET', `/api/media/assets/${encodeURIComponent(id)}`);
    return expect<import('./types').Asset>(data, (v) => typeof v.id === 'string' && typeof v.type === 'string',
      'asset');
  }
}
