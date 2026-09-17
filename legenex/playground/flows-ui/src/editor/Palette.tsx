// The node library. Click (or Enter) adds a node at the centre of the view;
// dragging onto the canvas adds it where it is dropped. Nodes without a real
// backend are listed but disabled, with the reason.
import { useId, useMemo, useState } from 'react';

import { CATEGORY_TONE, useApp } from '../context';
import type { NodeTypeSpec } from '../types';
import { Icon } from '../ui';
import { DRAG_MIME } from './Canvas';

export function Palette({ onAdd, onClose }: { onAdd: (type: string) => void; onClose?: () => void }) {
  const { catalog, live } = useApp();
  const [q, setQ] = useState('');
  const searchId = useId();
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const match = (n: NodeTypeSpec) => !needle || [n.label, n.description, n.type, ...n.keywords]
      .some((s) => s.toLowerCase().includes(needle));
    return catalog.categories.map((c) => ({ ...c, nodes: catalog.nodes.filter((n) => n.category === c.id && match(n)) }))
      .filter((g) => g.nodes.length);
  }, [catalog, q]);
  const count = groups.reduce((n, g) => n + g.nodes.length, 0);
  return (
    <nav className="gxf-palette" aria-label="Node library">
      <div className="gxf-palette-head">
        <h2 className="gxf-panel-title">Add nodes</h2>
        {onClose ? (
          <button type="button" className="icon-btn icon-btn-ghost" aria-label="Close the node library" onClick={onClose}>
            <Icon name="close" />
          </button>
        ) : null}
      </div>
      <label htmlFor={searchId} className="sr-only">Search nodes</label>
      <div className="gxf-search">
        <Icon name="search" />
        <input id={searchId} className="input" type="search" placeholder="Search nodes (/)" value={q}
          data-palette-search="" autoComplete="off" onChange={(ev) => { setQ(ev.target.value); }} />
      </div>
      <p className="sr-only" aria-live="polite">{q ? `${String(count)} node types match` : ''}</p>
      <div className="gxf-palette-groups">
        {groups.map((g) => (
          <section key={g.id} className="gxf-palette-group" aria-labelledby={`gxf-cat-${g.id}`}>
            <h3 id={`gxf-cat-${g.id}`} className="gxf-palette-cat">
              <span className="gxf-cat-dot" style={{ background: CATEGORY_TONE[g.id] }} aria-hidden="true" />
              {g.label}
            </h3>
            <ul>
              {g.nodes.map((n) => {
                const reason = !n.available ? n.unavailable_reason : live.get(n.type);
                const descId = `gxf-pal-${n.type.replace('.', '-')}`;
                return (
                  <li key={n.type}>
                    <button type="button" className="gxf-palette-item" data-node-type={n.type}
                      disabled={Boolean(reason)} draggable={!reason} aria-describedby={descId}
                      onDragStart={(ev) => { ev.dataTransfer.setData(DRAG_MIME, n.type); ev.dataTransfer.effectAllowed = 'copy'; }}
                      onClick={() => { onAdd(n.type); }}>
                      <span className="gxf-palette-name">{n.label}</span>
                      <span id={descId} className="gxf-palette-desc">{reason ? `Unavailable: ${reason}` : n.description}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
        {!count ? <p className="gxf-muted">No node matches “{q}”.</p> : null}
      </div>
    </nav>
  );
}
