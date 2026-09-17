// Small accessible UI primitives in the Playground's design language
// (the app's CSS variables are inherited; classes are prefixed gxf-).
import { useEffect, useId, useRef } from 'react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';

const PATHS: Record<string, string> = {
  play: 'M8 5v14l11-7z',
  stop: 'M6 6h12v12H6z',
  plus: 'M12 5v14M5 12h14',
  undo: 'M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3',
  redo: 'M15 14l5-5-5-5M20 9H10a6 6 0 000 12h3',
  fit: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  sparkles: 'M12 3l1.8 4.2L18 9l-4.2 1.8L12 15l-1.8-4.2L6 9l4.2-1.8zM19 14l.9 2.1L22 17l-2.1.9L19 20l-.9-2.1L16 17l2.1-.9z',
  template: 'M4 4h16v6H4zM4 14h7v6H4zM15 14h5v6h-5z',
  history: 'M3 12a9 9 0 109-9 9 9 0 00-7 3.4M3 4v4h4M12 7v5l3 3',
  close: 'M6 6l12 12M18 6L6 18',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  copy: 'M8 8h12v12H8zM4 16V4h12',
  lock: 'M6 11h12v9H6zM8 11V7a4 4 0 018 0v4',
  unlock: 'M6 11h12v9H6zM8 11V7a4 4 0 017.5-2',
  bypass: 'M4 12h16M14 6l6 6-6 6M4 6v12',
  link: 'M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1',
  refresh: 'M20 11a8 8 0 10-2.3 5.7M20 4v7h-7',
  key: 'M14 10a4 4 0 10-3.9 5L8 17v3h3v-2h2v-2l1-1a4 4 0 001-5zM16 8h.01',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  search: 'M11 18a7 7 0 110-14 7 7 0 010 14zM21 21l-4.3-4.3',
  back: 'M15 18l-6-6 6-6',
  download: 'M12 4v11M7 10l5 5 5-5M5 20h14',
  open: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
  keyboard: 'M3 6h18v12H3zM7 10h.01M11 10h.01M15 10h.01M7 14h10',
  variables: 'M8 4c-2 0-3 1-3 3v2c0 1.5-1 3-2 3 1 0 2 1.5 2 3v2c0 2 1 3 3 3M16 4c2 0 3 1 3 3v2c0 1.5 1 3 2 3-1 0-2 1.5-2 3v2c0 2-1 3-3 3',
  inspector: 'M4 4h16v16H4zM14 4v16',
  warn: 'M12 3l10 18H2zM12 10v4M12 17h.01',
};

export function Icon({ name, size = 16, label }: { name: string; size?: number; label?: string }) {
  const d = PATHS[name] ?? PATHS.more;
  return (
    <svg className="gxf-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden={label ? undefined : true}
      role={label ? 'img' : undefined} aria-label={label} focusable="false">
      <path d={d} />
    </svg>
  );
}

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

export interface BtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon?: string;
  variant?: Variant;
  size?: 'sm' | 'md';
  label?: string;
  shortcut?: string;
}

export function Btn({ icon, variant = 'secondary', size = 'md', label, shortcut, children, className, ...rest }: BtnProps) {
  const iconOnly = !children && Boolean(label);
  return (
    <button type="button" className={['btn', `btn-${variant}`, size === 'sm' ? 'btn-sm' : '', iconOnly ? 'gxf-icon-btn' : '',
      className ?? ''].filter(Boolean).join(' ')} aria-label={iconOnly ? label : undefined}
      title={label ? `${label}${shortcut ? ` (${shortcut})` : ''}` : undefined}
      aria-keyshortcuts={shortcut ? shortcut.replace(/Ctrl/g, 'Control') : undefined} {...rest}>
      {icon ? <Icon name={icon} /> : null}
      {children ? <span>{children}</span> : null}
    </button>
  );
}

export function Modal({ title, onClose, children, footer, wide = false, labelledBy }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean; labelledBy?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return undefined;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (typeof dlg.showModal === 'function' && !dlg.open) dlg.showModal();
    else dlg.setAttribute('open', '');
    const onCancel = (ev: Event) => { ev.preventDefault(); closeRef.current(); };
    dlg.addEventListener('cancel', onCancel);
    const first = dlg.querySelector<HTMLElement>('[data-autofocus], input, textarea, select, button');
    first?.focus();
    return () => {
      dlg.removeEventListener('cancel', onCancel);
      if (dlg.open && typeof dlg.close === 'function') dlg.close();
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return (
    <dialog ref={ref} className={`dialog gxf-dialog${wide ? ' gxf-dialog-wide' : ''}`}
      aria-labelledby={labelledBy ?? titleId}
      onClick={(ev) => { if (ev.target === ref.current) onClose(); }}>
      <header className="dialog-head">
        <h2 id={titleId} className="dialog-title">{title}</h2>
        <button type="button" className="icon-btn dialog-x" aria-label="Close dialog" onClick={onClose}>
          <Icon name="close" />
        </button>
      </header>
      <div className="dialog-body">{children}</div>
      {footer ? <footer className="dialog-foot">{footer}</footer> : null}
    </dialog>
  );
}

export const STATUS_LABEL: Record<string, string> = {
  pending: 'Pending', queued: 'Queued', waiting: 'Waiting', running: 'Running', succeeded: 'Done', cached: 'Cached',
  reused: 'Reused', failed: 'Failed', cancelled: 'Cancelled', skipped: 'Skipped', bypassed: 'Bypassed',
  blocked: 'Blocked', interrupted: 'Interrupted', idle: 'Not run',
};

const STATUS_TONE: Record<string, string> = {
  pending: 'idle', queued: 'warn', waiting: 'warn', running: 'info', succeeded: 'ok', cached: 'ok', reused: 'ok',
  failed: 'danger', cancelled: 'idle', skipped: 'idle', bypassed: 'idle', blocked: 'danger', interrupted: 'danger',
  idle: 'idle',
};

export function StatusBadge({ status, compact = false }: { status: string; compact?: boolean }) {
  return (
    <span className={`gxf-status gxf-tone-${STATUS_TONE[status] ?? 'idle'}${compact ? ' gxf-status-compact' : ''}`}
      data-status={status}>
      <span className="gxf-status-dot" aria-hidden="true" />
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (value < 1) return `${String(Math.round(value * 1000))} ms`;
  if (value < 90) return `${value.toFixed(1)} s`;
  const m = Math.floor(value / 60);
  return `${String(m)} min ${String(Math.round(value - m * 60))} s`;
}

export function when(epoch: number | null | undefined): string {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString();
}

export function ago(epoch: number | null | undefined): string {
  if (!epoch) return '';
  const d = Math.max(0, Date.now() / 1000 - epoch);
  if (d < 45) return 'just now';
  if (d < 3600) return `${String(Math.round(d / 60))} min ago`;
  if (d < 86400) return `${String(Math.round(d / 3600))} h ago`;
  return new Date(epoch * 1000).toLocaleDateString();
}
