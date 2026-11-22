// Explorer: every node in a sortable, filterable, paged table; a row opens the node panel in a drawer.

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, ApiError, type NodeRow } from "../api";
import {
  explorerQuery, initialExplorer, nextRow, pageCount, setPage, setQuery, setStatus, setType, toggleSort,
  type ExplorerState, type SortKey, type StatusFilter,
} from "../explorer";
import type { NodeDetail } from "../focus";
import { colorForType } from "../graphData";
import { NodePanel } from "./NodePanel";

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "confidence", label: "Confidence" },
  { key: "updated_at", label: "Updated" },
];
type Load = { state: "loading" } | { state: "ready" } | { state: "error"; message: string };

/** Where the detail drawer goes: beside the table on wide screens, over it on narrow ones. */
export function drawerLayout(width: number): "side" | "overlay" {
  return width >= 1100 ? "side" : "overlay";
}

function day(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function ExplorerPage() {
  const [state, setState] = useState<ExplorerState>(initialExplorer);
  const [draft, setDraft] = useState("");
  const [rows, setRows] = useState<NodeRow[]>([]);
  const [total, setTotal] = useState(0);
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [types, setTypes] = useState<string[]>([]);
  const [active, setActive] = useState(-1);
  const [drawer, setDrawer] = useState<{ id: number; detail: NodeDetail | null; error: string | null } | null>(null);
  const body = useRef<HTMLTableSectionElement>(null);
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    api.stats(ctrl.signal).then((s) => setTypes(Object.keys(s.nodes_by_type ?? {}).sort())).catch(() => {});
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => setState((s) => (s.q === draft.trim() ? s : setQuery(s, draft))), 250);
    return () => window.clearTimeout(timer);
  }, [draft]);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoad((l) => (l.state === "ready" ? l : { state: "loading" }));
    api.nodes(explorerQuery(state), ctrl.signal)
      .then((r) => { setRows(r.items); setTotal(r.total); setLoad({ state: "ready" }); setActive(-1); })
      .catch((err) => { if (err.name !== "AbortError") setLoad({ state: "error", message: (err as ApiError).message }); });
    return () => ctrl.abort();
  }, [state]);

  const open = (id: number) => {
    setDrawer({ id, detail: null, error: null });
    api.node(id).then((detail) => setDrawer((d) => (d && d.id === id ? { ...d, detail } : d)))
      .catch((err: ApiError) => setDrawer((d) => (d && d.id === id ? { ...d, error: err.message } : d)));
  };

  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") setDrawer(null); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawer]);

  const onTableKey = (e: KeyboardEvent<HTMLTableSectionElement>) => {
    if (e.key === "Enter" && active >= 0 && rows[active]) {
      e.preventDefault();
      open(rows[active].id);
      return;
    }
    const next = nextRow(active, e.key, rows.length);
    if (next !== active) {
      e.preventDefault();
      setActive(next);
      body.current?.querySelectorAll("tr")[next]?.scrollIntoView?.({ block: "nearest" });
    }
  };

  const pages = pageCount(total);
  return (
    <div className={`page explorer-page${drawer ? ` drawer-${drawerLayout(width)}` : ""}`}>
      <header className="page-head">
        <p className="eyebrow">Explorer</p>
        <h1>Every memory, in a table</h1>
        <p className="muted">Search, filter and sort everything the room remembers. Open a row for its details.</p>
      </header>

      <div className="explorer-tools glass">
        <input type="search" placeholder="Search memory…" aria-label="Search memory" maxLength={200} value={draft}
               onChange={(e) => setDraft(e.target.value)} />
        <select aria-label="Type" value={state.type} onChange={(e) => setState(setType(state, e.target.value))}>
          <option value="">All types</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <select aria-label="Status" value={state.status} onChange={(e) => setState(setStatus(state, e.target.value as StatusFilter))}>
          <option value="all">Any status</option>
          <option value="active">Active</option>
          <option value="superseded">Superseded</option>
        </select>
        <span className="muted result-count">{load.state === "ready" ? `${total} ${total === 1 ? "node" : "nodes"}` : ""}</span>
      </div>

      {load.state === "error" && (
        <div className="glass state-card static"><h2>Cannot load the nodes</h2><p>{load.message}</p></div>
      )}
      {load.state === "loading" && <div className="center-state static"><div className="spinner" />Loading…</div>}
      {load.state === "ready" && rows.length === 0 && (
        <div className="glass state-card static"><h2>No nodes match</h2><p className="muted">Change the search or filters.</p></div>
      )}

      {load.state === "ready" && rows.length > 0 && (
        <div className="table-wrap glass">
          <table className="explorer-table">
            <thead>
              <tr>
                {COLUMNS.map((c) => (
                  <th key={c.key} scope="col"
                      aria-sort={state.sort === c.key ? (state.order === "asc" ? "ascending" : "descending") : "none"}>
                    <button type="button" className="th-label" data-sort={c.key} onClick={() => setState(toggleSort(state, c.key))}>
                      {c.label}
                      <span className="sort-mark" aria-hidden="true">{state.sort === c.key ? (state.order === "asc" ? "↑" : "↓") : ""}</span>
                    </button>
                  </th>
                ))}
                <th scope="col"><span className="th-label">Status</span></th>
              </tr>
            </thead>
            <tbody ref={body} tabIndex={0} onKeyDown={onTableKey} aria-label="Nodes; arrow keys move, Enter opens">
              {rows.map((r, i) => (
                <tr key={r.id} aria-selected={i === active} className={[r.status === "superseded" ? "dimmed" : "", i === active ? "active" : ""].join(" ").trim() || undefined}
                    onClick={() => { setActive(i); open(r.id); }}>
                  <td className="cell-name"><span className="name-wrap"><span className="dot" style={{ background: colorForType(r.type) }} />{r.name}</span></td>
                  <td className="cell-type">{r.type}</td>
                  <td className="cell-conf">
                    <span className="conf-bar" aria-hidden="true"><span style={{ width: `${Math.round(r.confidence * 100)}%` }} /></span>
                    <span className="conf-num">{Math.round(r.confidence * 100)}%</span>
                  </td>
                  <td className="cell-date muted">{day(r.updated_at)}</td>
                  <td><span className={`status-chip ${r.status}`}>{r.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          <nav className="pager" aria-label="Pages">
            <button type="button" className="button ghost" disabled={state.page === 0}
                    onClick={() => setState(setPage(state, state.page - 1, total))}>Previous</button>
            <span className="muted">Page {state.page + 1} of {pages}</span>
            <button type="button" className="button ghost" disabled={state.page >= pages - 1}
                    onClick={() => setState(setPage(state, state.page + 1, total))}>Next</button>
          </nav>
        </div>
      )}

      {drawer && (
        <div className="drawer" role="dialog" aria-label="Node details">
          <div className="drawer-body">
            {drawer.detail ? (
              <NodePanel node={drawer.detail} onSelect={(id) => open(id)} onClose={() => setDrawer(null)} />
            ) : (
              <aside className="panel glass">
                {drawer.error ? <p>{drawer.error}</p> : <div className="center-state"><div className="spinner" />Loading…</div>}
              </aside>
            )}
          </div>
          {drawer.detail && (
            <footer className="drawer-footer">
              <a className="open-in-graph button" href={`#/graph?node=${drawer.detail.id}`}>Open in graph</a>
            </footer>
          )}
        </div>
      )}
    </div>
  );
}
