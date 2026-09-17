// Editors for catalogue field kinds. Used on node cards (compact) and in the
// Inspector. Every control has a programmatic label and an error message
// tied to it with aria-describedby.
import { useEffect, useState } from 'react';

import { useApp, useEditor } from '../context';
import type { Asset, FieldOption, FieldSpec, FlowNode, Options } from '../types';
import { Btn } from '../ui';

export interface FieldProps {
  field: FieldSpec;
  node: FlowNode;
  place: 'card' | 'inspector';
  disabled: boolean;
  connected: Set<string>;
  onChange: (value: unknown) => void;
}

const SIZE_LABELS: Record<string, string> = {
  '1328x1328': 'Square 1:1 (1328)', '1024x1024': 'Square 1:1 (1024)', '1664x928': 'Landscape 16:9',
  '928x1664': 'Portrait 9:16', '1328x800': 'Landscape 5:3', '800x1328': 'Portrait 3:5', '768x768': 'Small 1:1',
  '512x512': 'Small square',
};

export function dynamicOptions(field: FieldSpec, node: FlowNode, options: Options): FieldOption[] | null {
  const source = field.source ?? '';
  if (source.startsWith('image_models')) {
    const op = source.split(':')[1] ?? 'generate';
    const def = op === 'generate' ? options.image_default?.generate : options.image_default?.edit;
    const models = options.image_models.filter((m) => m.operations.includes(op));
    const defLabel = models.find((m) => m.id === def)?.label;
    return [{ value: '', label: defLabel ? `Default (${defLabel})` : 'Default model' },
      ...models.map((m) => ({ value: m.id, label: m.label }))];
  }
  if (source === 'image_sizes') {
    const model = (typeof node.config.image_model === 'string' && node.config.image_model)
      || options.image_default?.generate || '';
    const sizes = options.image_sizes[model] ?? [];
    const def = options.image_models.find((m) => m.id === model)?.default_size;
    return [{ value: '', label: def ? `Model default (${SIZE_LABELS[def] ?? def})` : 'Model default' },
      ...sizes.map((s) => ({ value: s, label: SIZE_LABELS[s] ? `${SIZE_LABELS[s]} · ${s}` : s }))];
  }
  if (source === 'edit_modes') {
    const model = (typeof node.config.image_model === 'string' && node.config.image_model)
      || options.image_default?.edit || '';
    return [{ value: '', label: 'Model default' },
      ...(options.edit_modes[model] ?? []).map((m) => ({ value: m.id, label: m.label }))];
  }
  if (source === 'lora_presets') {
    return [{ value: '', label: field.required ? 'Choose a preset' : 'None' },
      ...options.lora_presets.map((p) => ({ value: p.id, label: `${p.name} (${String(p.loras)} LoRA${p.loras === 1 ? '' : 's'})` }))];
  }
  if (source === 'voices') {
    return [{ value: '', label: 'Choose a voice' },
      ...options.voices.map((v) => ({ value: v.id, label: `${v.name} · ${v.kind}` }))];
  }
  return null;
}

function fieldId(props: FieldProps): string {
  return `gxf-${props.place}-${props.node.id}-${props.field.id}`;
}

function Help({ id, field, error }: { id: string; field: FieldSpec; error?: string }) {
  return (
    <>
      {error ? <p id={`${id}-err`} className="gxf-field-error" role="alert">{error}</p> : null}
      {field.help ? <p id={`${id}-help`} className="field-hint">{field.help}</p> : null}
    </>
  );
}

function describedBy(id: string, field: FieldSpec, error?: string): string | undefined {
  return [error ? `${id}-err` : '', field.help ? `${id}-help` : ''].filter(Boolean).join(' ') || undefined;
}

export function FieldEditor(props: FieldProps) {
  const { field, node, place, disabled, connected } = props;
  const id = fieldId(props);
  const value = node.config[field.id] ?? field.default;
  const filledByInput = Boolean(field.fills && connected.has(field.fills));
  const label = (
    <label htmlFor={id} className="field-label">
      {field.label}{field.required && !field.fills ? <span aria-hidden="true"> *</span> : null}
      {filledByInput ? <span className="gxf-muted"> (from connection)</span> : null}
    </label>
  );
  const common = { id, disabled: disabled || filledByInput, 'aria-required': field.required || undefined };
  const wrap = (control: React.ReactNode, error?: string) => (
    <div className={`field gxf-field gxf-field-${field.kind}`} data-field={field.id}>
      {label}
      {control}
      {place === 'inspector' || error ? <Help id={id} field={field} error={error} /> : null}
    </div>
  );

  switch (field.kind) {
    case 'text':
    case 'textarea':
      return <TextControl {...props} id={id} common={common} wrap={wrap} value={typeof value === 'string' ? value : ''} />;
    case 'number':
    case 'seed':
      return <NumberControl {...props} id={id} common={common} wrap={wrap}
        value={typeof value === 'number' ? value : null} />;
    case 'boolean':
      return (
        <div className="field gxf-field gxf-field-boolean" data-field={field.id}>
          <label className="gxf-check" htmlFor={id}>
            <input type="checkbox" {...common} checked={Boolean(value)}
              aria-describedby={place === 'inspector' ? describedBy(id, field) : undefined}
              onChange={(ev) => { props.onChange(ev.target.checked); }} />
            <span>{field.label}</span>
          </label>
          {place === 'inspector' ? <Help id={id} field={field} /> : null}
        </div>
      );
    case 'select':
      return <SelectControl {...props} id={id} common={common} wrap={wrap} value={typeof value === 'string' ? value : ''} />;
    case 'asset':
      return <AssetControl {...props} id={id} wrap={wrap} value={typeof value === 'string' ? value : ''} />;
    case 'tags':
      return <TagsControl {...props} id={id} common={common} wrap={wrap}
        value={Array.isArray(value) ? (value as string[]) : []} />;
    case 'keyvalue':
    case 'headers':
      return <PairsControl {...props} id={id} wrap={wrap}
        value={Array.isArray(value) ? (value as Record<string, string>[]) : []} />;
    default:
      return null;
  }
}

interface ControlProps<V> extends FieldProps {
  id: string;
  value: V;
  common?: { id: string; disabled: boolean; 'aria-required': boolean | undefined };
  wrap: (control: React.ReactNode, error?: string) => React.ReactNode;
}

function TextControl({ field, place, common, wrap, value, onChange, id }: ControlProps<string>) {
  const [local, setLocal] = useState(value);
  const [synced, setSynced] = useState(value);
  if (synced !== value) {          // the value changed elsewhere (undo, AI draft, template)
    setSynced(value);
    setLocal(value);
  }
  const tooLong = field.max_length !== undefined && local.length > field.max_length;
  const badPattern = Boolean(field.pattern && local && !new RegExp(field.pattern).test(local));
  const error = tooLong ? `At most ${String(field.max_length)} characters.` : badPattern ? 'This value has an invalid format.' : undefined;
  const commit = (v: string) => {
    setLocal(v);
    if ((field.max_length === undefined || v.length <= field.max_length) && !(field.pattern && v && !new RegExp(field.pattern).test(v))) {
      onChange(v);
    }
  };
  const describe = place === 'inspector' || error ? describedBy(id, field, error) : undefined;
  const control = field.kind === 'textarea'
    ? <textarea {...common} className="input textarea nodrag nowheel" value={local} placeholder={field.placeholder}
      rows={place === 'card' ? 3 : 6} aria-invalid={error ? true : undefined} aria-describedby={describe}
      onChange={(ev) => { commit(ev.target.value); }} />
    : <input {...common} className="input nodrag" value={local} placeholder={field.placeholder}
      aria-invalid={error ? true : undefined} aria-describedby={describe} onChange={(ev) => { commit(ev.target.value); }} />;
  return <>{wrap(control, error)}</>;
}

function NumberControl({ field, place, common, wrap, value, onChange, id, disabled }: ControlProps<number | null>) {
  const [local, setLocal] = useState(value === null ? '' : String(value));
  const [synced, setSynced] = useState(value);
  if (synced !== value) {
    setSynced(value);
    setLocal(value === null ? '' : String(value));
  }
  const n = local.trim() === '' ? null : Number(local);
  let error: string | undefined;
  if (n !== null && (!Number.isFinite(n) || (field.min !== undefined && n < field.min) || (field.max !== undefined && n > field.max))) {
    error = `Enter a number between ${String(field.min)} and ${String(field.max)}.`;
  } else if (n !== null && (field.integer || field.kind === 'seed') && !Number.isInteger(n)) {
    error = 'Enter a whole number.';
  }
  const commit = (raw: string) => {
    setLocal(raw);
    const v = raw.trim() === '' ? null : Number(raw);
    if (v === null) { onChange(null); return; }
    if (!Number.isFinite(v) || (field.min !== undefined && v < field.min) || (field.max !== undefined && v > field.max)) return;
    if ((field.integer || field.kind === 'seed') && !Number.isInteger(v)) return;
    onChange(v);
  };
  const control = (
    <div className="gxf-row">
      <input {...common} className="input nodrag" type="number" inputMode="decimal" value={local}
        min={field.min} max={field.max} step={field.kind === 'seed' ? 1 : field.step}
        placeholder={field.kind === 'seed' ? 'Random' : (field.default === null ? 'Default' : undefined)}
        aria-invalid={error ? true : undefined}
        aria-describedby={place === 'inspector' || error ? describedBy(id, field, error) : undefined}
        onChange={(ev) => { commit(ev.target.value); }} />
      {field.kind === 'seed' && place === 'inspector' ? (
        <Btn size="sm" variant="ghost" disabled={disabled} onClick={() => {
          const buf = new Uint32Array(1);
          crypto.getRandomValues(buf);
          commit(String((buf[0] ?? 0) % 2147483647));
        }}>New seed</Btn>
      ) : null}
    </div>
  );
  return <>{wrap(control, error)}</>;
}

function SelectControl({ field, node, place, common, wrap, value, onChange, id }: ControlProps<string>) {
  const { options } = useApp();
  const dynamic = dynamicOptions(field, node, options);
  const list = dynamic ?? field.options;
  const known = list.some((o) => o.value === value);
  return (
    <>
      {wrap(
        <select {...common} className="input select nodrag" value={value}
          aria-describedby={place === 'inspector' ? describedBy(id, field) : undefined}
          onChange={(ev) => { onChange(ev.target.value); }}>
          {!known && value ? <option value={value}>{value} (not available)</option> : null}
          {list.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>,
        !known && value ? 'This choice is no longer available.' : undefined,
      )}
    </>
  );
}

function AssetControl({ field, node, wrap, value, id, disabled }: ControlProps<string>) {
  const { api } = useApp();
  const { actions } = useEditor();
  // Keyed by the chosen id, so a new choice never shows the previous preview.
  const [loaded, setLoaded] = useState<{ id: string; asset: Asset | null } | null>(null);
  const current = loaded?.id === value ? loaded : null;
  const asset = current?.asset ?? null;
  const missing = current !== null && current.asset === null;
  const kind = field.asset_type ?? (typeof node.config.asset_type === 'string' ? node.config.asset_type : undefined);
  useEffect(() => {
    if (!value) return undefined;
    let alive = true;
    api.asset(value).then((a) => { if (alive) setLoaded({ id: value, asset: a }); },
      () => { if (alive) setLoaded({ id: value, asset: null }); });
    return () => { alive = false; };
  }, [api, value]);
  const accept = kind === 'video' ? 'video/mp4,video/webm,video/quicktime'
    : kind === 'audio' ? 'audio/*' : 'image/png,image/jpeg,image/webp';
  const control = (
    <div className="gxf-asset nodrag" id={id} role="group" aria-label={`${field.label}: ${asset ? asset.title ?? asset.id : 'nothing chosen'}`}>
      {asset ? (
        <div className="gxf-asset-chosen">
          {asset.type === 'image' ? <img src={asset.thumbnail_url} alt="" className="gxf-asset-thumb" /> : null}
          <span className="gxf-asset-name">{asset.title ?? asset.prompt ?? asset.id}</span>
        </div>
      ) : <p className="gxf-muted small">{missing ? 'The chosen item was deleted from the Library.' : 'Nothing chosen yet.'}</p>}
      <div className="gxf-row">
        <Btn size="sm" disabled={disabled} onClick={() => { actions.pickAsset(node.id, field.id, kind); }}>Choose from Library</Btn>
        <label className={`btn btn-secondary btn-sm gxf-upload${disabled ? ' is-disabled' : ''}`}>
          Upload
          <input type="file" accept={accept} className="sr-only" disabled={disabled}
            onChange={(ev) => {
              const file = ev.target.files?.[0];
              if (file) actions.uploadAsset(node.id, field.id, file);
              ev.target.value = '';
            }} />
        </label>
      </div>
    </div>
  );
  return <>{wrap(control, missing ? 'Choose another item.' : undefined)}</>;
}

function TagsControl({ field, place, common, wrap, value, onChange, id }: ControlProps<string[]>) {
  const joined = value.join(', ');
  const [local, setLocal] = useState(joined);
  const [synced, setSynced] = useState(joined);
  if (synced !== joined) {
    setSynced(joined);
    setLocal(joined);
  }
  const tags = local.split(',').map((t) => t.trim()).filter(Boolean);
  const error = tags.length > (field.max_length ?? 12) ? `At most ${String(field.max_length ?? 12)} tags.`
    : tags.some((t) => t.length > 40) ? 'Tags are at most 40 characters.' : undefined;
  return (
    <>
      {wrap(
        <input {...common} className="input nodrag" value={local} placeholder="cinematic, piano, ambient"
          aria-invalid={error ? true : undefined}
          aria-describedby={place === 'inspector' || error ? describedBy(id, field, error) : undefined}
          onChange={(ev) => {
            setLocal(ev.target.value);
            const next = ev.target.value.split(',').map((t) => t.trim()).filter(Boolean);
            if (next.length <= (field.max_length ?? 12) && next.every((t) => t.length <= 40)) onChange(next);
          }} />,
        error,
      )}
    </>
  );
}

function PairsControl({ field, node, wrap, value, onChange, id, disabled }: ControlProps<Record<string, string>[]>) {
  const { options } = useApp();
  const headers = field.kind === 'headers';
  const [a, b] = headers ? ['header', 'secret'] : ['key', 'value'];
  const aLabel = headers ? 'Header' : (field.id === 'speakers' ? 'Speaker' : 'Name');
  const bLabel = headers ? 'Secret name' : (field.id === 'schema' ? 'Type' : field.id === 'speakers' ? 'Voice' : 'Value');
  const [rows, setRows] = useState<Record<string, string>[]>(value);
  const [synced, setSynced] = useState(value);
  if (synced !== value) {
    setSynced(value);
    setRows(value);
  }
  const keyOk = (k: string | undefined) => (headers ? /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/ : /^[A-Za-z_][A-Za-z0-9_ -]{0,63}$/)
    .test(k ?? '');
  const valueOk = (v: string | undefined) => (headers ? /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(v ?? '') : true);
  const update = (next: Record<string, string>[]) => {
    setRows(next);
    if (next.every((r) => keyOk(r[a]) && valueOk(r[b]))) onChange(next);
  };
  const set = (i: number, key: string, v: string) => {
    update(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  };
  const voices = dynamicOptions({ ...field, source: 'voices' }, node, options) ?? [];
  const control = (
    <div className="gxf-pairs nodrag" id={id} role="group" aria-label={field.label}>
      {rows.map((row, i) => (
        <div className="gxf-pair" key={String(i)}>
          <input className="input" aria-label={`${aLabel} ${String(i + 1)}`} value={row[a] ?? ''} disabled={disabled}
            onChange={(ev) => { set(i, a, ev.target.value); }} />
          {field.id === 'schema' ? (
            <select className="input select" aria-label={`${bLabel} ${String(i + 1)}`} value={row[b] ?? 'string'}
              disabled={disabled} onChange={(ev) => { set(i, b, ev.target.value); }}>
              {['string', 'number', 'boolean', 'list'].map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          ) : field.id === 'speakers' ? (
            <select className="input select" aria-label={`${bLabel} ${String(i + 1)}`} value={row[b] ?? ''}
              disabled={disabled} onChange={(ev) => { set(i, b, ev.target.value); }}>
              {voices.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          ) : (
            <input className="input" aria-label={`${bLabel} ${String(i + 1)}`} value={row[b] ?? ''} disabled={disabled}
              autoComplete="off" onChange={(ev) => { set(i, b, ev.target.value); }} />
          )}
          <Btn size="sm" variant="ghost" icon="trash" label={`Remove ${aLabel.toLowerCase()} ${String(i + 1)}`}
            disabled={disabled} onClick={() => { update(rows.filter((_, j) => j !== i)); }} />
        </div>
      ))}
      <Btn size="sm" icon="plus" disabled={disabled || rows.length >= (field.max_length ?? 32)}
        onClick={() => { update([...rows, { [a]: '', [b]: field.id === 'schema' ? 'string' : '' }]); }}>
        Add {aLabel.toLowerCase()}
      </Btn>
    </div>
  );
  const invalid = rows.some((r) => !keyOk(r[a]) || !valueOk(r[b]));
  return <>{wrap(control, invalid ? (headers ? 'Header names are letters, digits and -; secret names start with a '
    + 'letter.' : 'Names start with a letter and use letters, digits, spaces, _ or -.') : undefined)}</>;
}
