// Outline: the whole graph as an accessible list. Every canvas operation has
// an equivalent here (inspect, connect, disconnect, run, bypass, delete), so
// the flow can be built and run without dragging.
import { useApp, useEditor, useEditorState } from '../context';
import { nodeName, readiness, topoOrder } from '../model/graph';
import { Btn, StatusBadge } from '../ui';

export function Outline() {
  const { cat, live } = useApp();
  const { store, actions, run, running } = useEditor();
  const { doc, selected } = useEditorState(store);
  const order = topoOrder(doc.nodes.map((n) => n.id), doc.edges) ?? doc.nodes.map((n) => n.id);
  const issues = readiness(doc, cat, live);
  const byId = new Map(doc.nodes.map((n) => [n.id, n]));
  if (!doc.nodes.length) {
    return <p className="gxf-muted gxf-outline-empty">This flow has no nodes yet. Add one from the node library.</p>;
  }
  return (
    <section className="gxf-outline" aria-label="Flow outline">
      <p className="gxf-muted small">Nodes are listed in execution order. {doc.edges.length} connection(s).</p>
      <ol className="gxf-outline-list">
        {order.map((id, index) => {
          const node = byId.get(id);
          if (!node) return null;
          const spec = cat.get(node.type);
          const name = nodeName(node, cat);
          const nodeIssues = issues.filter((i) => i.node_id === id);
          const incoming = doc.edges.filter((e) => e.target === id);
          const outgoing = doc.edges.filter((e) => e.source === id);
          const status = node.disabled ? 'bypassed' : run?.nodes?.[id]?.status ?? 'idle';
          return (
            <li key={id} className={`gxf-outline-item${selected.includes(id) ? ' is-selected' : ''}`}
              data-outline-node={id} aria-labelledby={`gxf-outline-${id}`}>
              <div className="gxf-outline-head">
                <span className="gxf-outline-index" aria-hidden="true">{index + 1}</span>
                <h3 id={`gxf-outline-${id}`} className="gxf-outline-name">
                  {name} <span className="gxf-node-type">{spec?.label}</span>
                </h3>
                <StatusBadge status={status} />
              </div>
              {nodeIssues.length ? (
                <ul className="gxf-issues">{nodeIssues.map((i) => <li key={i.message}>{i.message}</li>)}</ul>
              ) : null}
              <div className="gxf-outline-links">
                <div>
                  <h4 className="gxf-sub">Inputs</h4>
                  <ul>
                    {spec?.inputs.map((p) => {
                      const links = incoming.filter((e) => e.target_port === p.id);
                      return (
                        <li key={p.id}>
                          <span>{p.label} ({p.types.join('/')}){p.required ? ', required' : ''}: </span>
                          {links.length ? links.map((e) => {
                            const src = byId.get(e.source);
                            const label = `${src ? nodeName(src, cat) : e.source} → ${p.label}`;
                            return (
                              <span key={e.id} className="gxf-chip">
                                {src ? nodeName(src, cat) : e.source}.{e.source_port}
                                <Btn size="sm" variant="ghost" icon="close" label={`Disconnect ${label}`}
                                  onClick={() => { store.disconnect([e.id]); }} />
                              </span>
                            );
                          }) : <span className="gxf-muted">not connected</span>}
                        </li>
                      );
                    })}
                    {!spec?.inputs.length ? <li className="gxf-muted">none</li> : null}
                  </ul>
                </div>
                <div>
                  <h4 className="gxf-sub">Outputs</h4>
                  <ul>
                    {spec?.outputs.map((p) => {
                      const links = outgoing.filter((e) => e.source_port === p.id);
                      return (
                        <li key={p.id}>
                          <span>{p.label}: </span>
                          {links.length ? links.map((e) => {
                            const dst = byId.get(e.target);
                            return <span key={e.id} className="gxf-chip">{dst ? nodeName(dst, cat) : e.target}.{e.target_port}</span>;
                          }) : <span className="gxf-muted">not used</span>}
                        </li>
                      );
                    })}
                    {!spec?.outputs.length ? <li className="gxf-muted">none (final output)</li> : null}
                  </ul>
                </div>
              </div>
              <div className="gxf-row">
                <Btn size="sm" onClick={() => { store.select([id]); actions.inspect(id); }}>Inspect</Btn>
                <Btn size="sm" icon="link" disabled={!spec?.outputs.length} onClick={() => { actions.connectFrom(id); }}>
                  Connect…
                </Btn>
                <Btn size="sm" icon="play" disabled={running} onClick={() => { actions.run('node', id); }}>Run</Btn>
                <Btn size="sm" variant="ghost" disabled={node.locked} onClick={() => { actions.toggleDisabled([id]); }}>
                  {node.disabled ? 'Enable' : 'Bypass'}
                </Btn>
                <Btn size="sm" variant="ghost" onClick={() => { actions.toggleLocked([id]); }}>
                  {node.locked ? 'Unlock' : 'Lock'}
                </Btn>
                <Btn size="sm" variant="danger" icon="trash" disabled={node.locked}
                  onClick={() => { actions.remove([id]); }} aria-label={`Delete ${name}`}>Delete</Btn>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
