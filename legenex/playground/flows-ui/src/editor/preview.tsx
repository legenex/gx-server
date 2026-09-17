// Output previews on node cards and in the Inspector: images, playable video
// and audio (with the Library waveform), text and JSON.
import { useEffect, useRef, useState } from 'react';

import { useApp, useEditor } from '../context';
import type { Asset, PortValue } from '../types';
import { Btn } from '../ui';

const assetCache = new Map<string, Promise<Asset>>();

export function useAsset(id: string | undefined): Asset | null {
  const { api } = useApp();
  const [asset, setAsset] = useState<Asset | null>(null);
  useEffect(() => {
    if (!id) { setAsset(null); return undefined; }
    let alive = true;
    let p = assetCache.get(id);
    if (!p) {
      p = api.asset(id);
      assetCache.set(id, p);
      p.catch(() => assetCache.delete(id));
    }
    p.then((a) => { if (alive) setAsset(a); }, () => { if (alive) setAsset(null); });
    return () => { alive = false; };
  }, [api, id]);
  return asset;
}

export function Waveform({ peaks, progress }: { peaks: [number, number][]; progress: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx || !peaks.length) return;
    const w = canvas.width;
    const h = canvas.height;
    const style = getComputedStyle(canvas);
    ctx.clearRect(0, 0, w, h);
    const step = w / peaks.length;
    peaks.forEach(([lo, hi], i) => {
      ctx.fillStyle = i / peaks.length <= progress ? style.getPropertyValue('--wave-played') || '#9d8bff'
        : style.getPropertyValue('--wave') || '#4d546b';
      const top = (1 - Math.max(0, Math.min(1, (hi + 1) / 2))) * h;
      const bottom = (1 - Math.max(0, Math.min(1, (lo + 1) / 2))) * h;
      ctx.fillRect(i * step, top, Math.max(1, step - 1), Math.max(1, bottom - top));
    });
  }, [peaks, progress]);
  return <canvas ref={ref} className="gxf-wave" width={240} height={40} aria-hidden="true" />;
}

function AudioPreview({ asset, compact }: { asset: Asset; compact: boolean }) {
  const [progress, setProgress] = useState(0);
  return (
    <div className="gxf-audio">
      {asset.waveform?.length ? <Waveform peaks={asset.waveform} progress={progress} /> : null}
      <audio className="nodrag" controls preload="none" src={asset.stream_url ?? asset.url}
        aria-label={`Play ${asset.title ?? 'audio'}`}
        onTimeUpdate={(ev) => {
          const el = ev.currentTarget;
          setProgress(el.duration ? el.currentTime / el.duration : 0);
        }} />
      {!compact && asset.duration ? <span className="gxf-muted small">{asset.duration.toFixed(1)} s</span> : null}
    </div>
  );
}

export function AssetPreview({ id, compact = false }: { id: string; compact?: boolean }) {
  const asset = useAsset(id);
  const { host } = useApp();
  const { actions } = useEditor();
  if (!asset) return <div className="gxf-preview-empty" aria-busy="true">Loading preview…</div>;
  const title = asset.title ?? asset.prompt ?? asset.id;
  return (
    <figure className={`gxf-preview gxf-preview-${asset.type}`}>
      {asset.type === 'image' ? (
        <img src={asset.thumbnail_url} alt={`Result: ${title}`} loading="lazy" />
      ) : asset.type === 'video' ? (
        <video className="nodrag" controls preload="metadata" playsInline poster={asset.thumbnail_url}
          src={asset.url} aria-label={`Video: ${title}`} />
      ) : (
        <AudioPreview asset={asset} compact={compact} />
      )}
      <figcaption className="gxf-row gxf-preview-actions">
        {!compact ? <span className="gxf-preview-title">{title}</span> : null}
        <Btn size="sm" variant="ghost" icon="open" label={`Open ${title} in the Library`}
          className="nodrag" onClick={() => { host.showAsset(asset.id); }} />
        <a className="btn btn-ghost btn-sm gxf-icon-btn nodrag" href={asset.download_url} download
          aria-label={`Download ${title}`} title="Download">
          <svg className="gxf-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14" /></svg>
        </a>
        {!compact ? (
          <Btn size="sm" variant="ghost" className="nodrag" onClick={() => { actions.useOutput(asset.id); }}>
            Use as input
          </Btn>
        ) : null}
      </figcaption>
    </figure>
  );
}

export function ValuePreview({ value, compact = false }: { value: PortValue; compact?: boolean }) {
  if (value.asset_id) return <AssetPreview id={value.asset_id} compact={compact} />;
  if (value.type === 'text') {
    const text = value.text ?? '';
    return <p className={`gxf-text-preview${compact ? ' is-compact' : ''}`}>{compact && text.length > 220 ? `${text.slice(0, 219)}…` : text}</p>;
  }
  if (value.type === 'json') {
    const json = JSON.stringify(value.data, null, 2);
    return <pre className="gxf-json-preview">{compact && json.length > 300 ? `${json.slice(0, 299)}…` : json}</pre>;
  }
  if (value.type === 'voice') return <p className="gxf-text-preview">Voice: {value.name ?? value.voice_id}</p>;
  if (value.type === 'lora') return <p className="gxf-text-preview">LoRA preset: {value.name ?? value.preset_id}</p>;
  return null;
}
