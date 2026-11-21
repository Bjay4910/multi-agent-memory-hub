// Side panel for one node. Every string from the API is rendered as React text (escaped), never as HTML.

import { colorForType } from "../graphData";
import type { NodeDetail } from "../focus";

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

interface Props {
  node: NodeDetail;
  onSelect: (id: number) => void;
  onClose: () => void;
}

export function NodePanel({ node, onSelect, onClose }: Props) {
  const color = colorForType(node.type);
  return (
    <aside className="panel glass" aria-label={`Details for ${node.name}`}>
      <header className="panel-head">
        <span className="type-pill" style={{ borderColor: color, color }}>{node.type}</span>
        <button type="button" className="icon-button" onClick={onClose} aria-label="Close details">×</button>
      </header>
      <h2 className="panel-title">{node.name}</h2>
      <p className={node.summary ? "panel-summary" : "panel-summary muted"}>{node.summary || "No summary."}</p>

      <dl className="facts">
        <div><dt>Status</dt><dd className={node.status === "superseded" ? "muted" : ""}>{node.status}</dd></div>
        <div><dt>Confidence</dt><dd>{Math.round(node.confidence * 100)}%</dd></div>
        <div><dt>Created</dt><dd>{formatDate(node.created_at)}</dd></div>
        <div><dt>Updated</dt><dd>{formatDate(node.updated_at)}</dd></div>
      </dl>

      {node.aliases.length > 0 && (
        <section>
          <h3>Also known as</h3>
          <ul className="chips-static">{node.aliases.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </section>
      )}

      <section>
        <h3>Linked to</h3>
        {node.neighbors.length === 0 ? <p className="muted">No links.</p> : (
          <ul className="neighbors">
            {node.neighbors.map((n, i) => (
              <li key={`${n.id}-${n.relation}-${i}`}>
                <span className="relation">{n.direction === "out" ? `${n.relation} →` : `← ${n.relation}`}</span>
                <button type="button" className="link-button" data-neighbor={n.id} onClick={() => onSelect(n.id)}>
                  <span className="dot" style={{ background: colorForType(n.type) }} />
                  {n.name}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3>Where it came from</h3>
        {node.provenance.length === 0 ? <p className="muted">No sources recorded.</p> : (
          <ul className="provenance">
            {node.provenance.map((p, i) => (
              <li key={i}>
                <span>{p.agent}</span>
                <span className="muted">{p.source}</span>
                <span className="muted mono">{p.session_id}</span>
                <span className="muted">{formatDate(p.ts)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}
