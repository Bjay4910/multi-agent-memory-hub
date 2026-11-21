// The Memory Graph page: data loading, filters, search and the side panel around the 3D canvas.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type SearchResult } from "../api";
import { clearTypes, graphQuery, initialFilters, setIncludeSuperseded, toggleType, type FilterState } from "../filters";
import { applyFocusPlan, planFocus, type NodeDetail } from "../focus";
import { colorForType, legendFor, toGraphData, type GraphData } from "../graphData";
import { useReducedMotion } from "../useReducedMotion";
import { hasWebGL } from "../webgl";
import { GraphCanvas, type FocusRequest } from "./GraphCanvas";
import { NodePanel } from "./NodePanel";

type Load = { state: "loading" } | { state: "ready" } | { state: "error"; error: ApiError };

export function GraphPage({ initialNodeId = null }: { initialNodeId?: number | null } = {}) {
  const reducedMotion = useReducedMotion();
  const [webgl, setWebgl] = useState(() => hasWebGL());
  const [filters, setFilters] = useState<FilterState>(initialFilters);
  const [graph, setGraph] = useState<GraphData>({ nodes: [], links: [] });
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [knownTypes, setKnownTypes] = useState<string[]>([]);
  const [bloom, setBloom] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const graphRef = useRef(graph);
  graphRef.current = graph;
  const nonce = useRef(0);

  const requestFocus = (id: number) => setFocus({ id, nonce: ++nonce.current });

  // graph, re-queried whenever the filters change
  useEffect(() => {
    const ctrl = new AbortController();
    setLoad({ state: "loading" });
    api.graph(graphQuery(filters), ctrl.signal)
      .then((g) => { setGraph(toGraphData(g)); setLoad({ state: "ready" }); })
      .catch((err) => { if (err.name !== "AbortError") setLoad({ state: "error", error: err as ApiError }); });
    return () => ctrl.abort();
  }, [filters]);

  // the type chips: every type in memory, plus any type loaded
  useEffect(() => {
    const ctrl = new AbortController();
    api.stats(ctrl.signal).then((s) => setKnownTypes(Object.keys(s.nodes_by_type))).catch(() => {});
    return () => ctrl.abort();
  }, []);
  const chipTypes = useMemo(
    () => [...new Set([...knownTypes, ...graph.nodes.map((n) => n.type), ...filters.types])].sort(),
    [knownTypes, graph.nodes, filters.types]);

  const openNode = useCallback((id: number, fly = true) => {
    setSelectedId(id);
    setDetailError(null);
    api.node(id).then(setDetail).catch((err: ApiError) => { setDetail(null); setDetailError(err.message); });
    if (fly && graphRef.current.nodes.some((n) => n.id === id)) requestFocus(id);
  }, []);

  // search, debounced
  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) { setResults([]); return; }
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      api.search(q, ctrl.signal)
        .then((r) => setResults(r.results))
        .catch(() => setResults([]))
        .finally(() => setSearching(false));
    }, 250);
    return () => { window.clearTimeout(timer); ctrl.abort(); };
  }, [query]);

  // Focus a node by id (search result or /graph?node=<id>): fly to it if loaded, add it through its loaded
  // neighbors, or say it is outside the loaded graph (planFocus / applyFocusPlan).
  const focusById = useCallback(async (id: number) => {
    setNotice(null);
    let plan = planFocus(id, graphRef.current);
    if (plan.kind === "need-detail") {
      try {
        const d = await api.node(id);
        plan = planFocus(id, graphRef.current, d);
        setDetail(d);
      } catch (err) {
        setNotice((err as ApiError).message);
        return;
      }
    }
    if (plan.kind === "add") {
      const added = plan;
      setGraph((g) => applyFocusPlan(g, added));
      setNotice(`"${added.node.name}" was outside the loaded view; it is shown with its ${added.links.length} loaded link${added.links.length === 1 ? "" : "s"}.`);
      setSelectedId(id);
      requestFocus(id);
      return;
    }
    if (plan.kind === "outside") {
      setSelectedId(id);
      setNotice(`"${plan.name}" is not in the loaded graph and none of its links are, so it is not drawn. Its details are in the panel.`);
      return;
    }
    openNode(id);
  }, [openNode]);

  function chooseResult(r: SearchResult) {
    setResults([]);
    setQuery("");
    void focusById(r.id);
  }

  // /graph?node=<id>: focus it once the graph has loaded
  const handledInitial = useRef<number | null>(null);
  useEffect(() => {
    if (initialNodeId === null || load.state !== "ready" || handledInitial.current === initialNodeId) return;
    handledInitial.current = initialNodeId;
    void focusById(initialNodeId);
  }, [initialNodeId, load.state, focusById]);

  const legend = legendFor(graph.nodes);
  const filtered = filters.types.length > 0;

  return (
    <div className="graph-page">
      <div className="graph-stage">
        {load.state === "ready" && graph.nodes.length > 0 && webgl && (
          <GraphCanvas data={graph} selectedId={selectedId} bloom={bloom} reducedMotion={reducedMotion}
                       focus={focus} onNodeClick={(id) => openNode(id)} onFailure={() => setWebgl(false)} />
        )}

        {load.state === "loading" && <div className="center-state"><div className="spinner" />Loading memory…</div>}
        {load.state === "error" && (
          <div className="center-state glass state-card">
            <h2>{load.error.status === 503 ? "Memory is not available" : "Cannot reach the dashboard API"}</h2>
            <p>{load.error.message}</p>
            <p className="muted">Start it with <code>room dashboard-api --demo</code> (or without <code>--demo</code> for your own memory).</p>
            <button type="button" className="button" onClick={() => setFilters({ ...filters })}>Try again</button>
          </div>
        )}
        {load.state === "ready" && graph.nodes.length === 0 && (
          <div className="center-state glass state-card">
            <h2>{filtered ? "Nothing matches these filters" : "No memory yet"}</h2>
            <p className="muted">{filtered ? "Clear the type filter to see everything."
              : "Facts appear here after the room has extracted them from a session."}</p>
            {filtered && <button type="button" className="button" onClick={() => setFilters(clearTypes(filters))}>Clear filters</button>}
          </div>
        )}
        {load.state === "ready" && graph.nodes.length > 0 && !webgl && (
          <div className="fallback glass">
            <h2>3D view unavailable</h2>
            <p className="muted">This browser cannot use WebGL, so here is the same memory as a list.</p>
            <ul className="fallback-list">
              {graph.nodes.map((n) => (
                <li key={n.id}>
                  <button type="button" className={n.dimmed ? "link-button muted" : "link-button"} onClick={() => openNode(n.id, false)}>
                    <span className="dot" style={{ background: colorForType(n.type) }} />{n.name}
                  </button>
                  <span className="muted">{n.type}{n.dimmed ? " · superseded" : ""}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="toolbar glass" role="search">
        <div className="search">
          <input type="search" value={query} placeholder="Search memory…" maxLength={200}
                 aria-label="Search memory" onChange={(e) => setQuery(e.target.value)} />
          {searching && <span className="spinner small" aria-hidden="true" />}
          {results.length > 0 && (
            <ul className="results glass" role="listbox">
              {results.map((r) => (
                <li key={r.id}>
                  <button type="button" onClick={() => chooseResult(r)}>
                    <span className="dot" style={{ background: colorForType(r.type) }} />
                    <span className="result-name">{r.name}</span>
                    <span className="muted">{r.type}{r.status === "superseded" ? " · superseded" : ""}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="chips" aria-label="Filter by type">
          {chipTypes.map((t) => {
            const on = filters.types.includes(t);
            return (
              <button key={t} type="button" aria-pressed={on} className={on ? "chip on" : "chip"}
                      style={{ ["--chip" as string]: colorForType(t) }} onClick={() => setFilters(toggleType(filters, t))}>
                {t}
              </button>
            );
          })}
        </div>
        <div className="toggles">
          <label className="switch">
            <input type="checkbox" checked={filters.includeSuperseded}
                   onChange={(e) => setFilters(setIncludeSuperseded(filters, e.target.checked))} />
            <span>Include superseded</span>
          </label>
          <label className="switch">
            <input type="checkbox" checked={bloom && webgl} disabled={!webgl} onChange={(e) => setBloom(e.target.checked)} />
            <span>Glow</span>
          </label>
        </div>
      </div>

      {notice && (
        <div className="notice glass" role="status">
          <span>{notice}</span>
          <button type="button" className="icon-button" onClick={() => setNotice(null)} aria-label="Dismiss">×</button>
        </div>
      )}

      {load.state === "ready" && graph.nodes.length > 0 && (
        <div className="legend glass" aria-label="Legend">
          <div className="legend-count">{graph.nodes.length} nodes · {graph.links.length} links</div>
          <ul>
            {legend.map((l) => (
              <li key={l.type}><span className="dot" style={{ background: l.color }} />{l.type}<span className="muted">{l.count}</span></li>
            ))}
            {filters.includeSuperseded && <li className="muted"><span className="dot dim" />superseded (dimmed)</li>}
          </ul>
        </div>
      )}

      {selectedId !== null && (detail?.id === selectedId ? (
        <NodePanel node={detail} onSelect={(id) => openNode(id)} onClose={() => { setSelectedId(null); setDetail(null); }} />
      ) : (
        <aside className="panel glass">
          {detailError ? <p>{detailError}</p> : <div className="center-state"><div className="spinner" />Loading…</div>}
        </aside>
      ))}
    </div>
  );
}
