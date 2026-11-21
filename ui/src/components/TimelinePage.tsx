// Timeline: sessions grouped by day; each expands to the memories it added, which link to the graph.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { colorForType } from "../graphData";
import { groupByDay, type SessionDetail, type SessionSummary } from "../timeline";

const SESSION_LIMIT = 200;
type Load = { state: "loading" } | { state: "ready" } | { state: "error"; message: string };
type Detail = { state: "loading" } | { state: "ready"; data: SessionDetail } | { state: "error"; message: string };

function time(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function TimelinePage({ anchor = null, timeZone }: { anchor?: string | null; timeZone?: string }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [details, setDetails] = useState<Record<string, Detail>>({});
  const items = useRef(new Map<string, HTMLElement>());

  useEffect(() => {
    const ctrl = new AbortController();
    api.sessions(SESSION_LIMIT, ctrl.signal)
      .then((r) => { setSessions(r.sessions); setLoad({ state: "ready" }); })
      .catch((err) => { if (err.name !== "AbortError") setLoad({ state: "error", message: (err as ApiError).message }); });
    return () => ctrl.abort();
  }, []);

  const loadDetail = (id: string) => {
    setDetails((d) => (d[id] && d[id].state !== "error" ? d : { ...d, [id]: { state: "loading" } }));
    api.session(id)
      .then((data) => setDetails((d) => ({ ...d, [id]: { state: "ready", data } })))
      .catch((err: ApiError) => setDetails((d) => ({ ...d, [id]: { state: "error", message: err.message } })));
  };

  const toggle = (id: string) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else {
        next.add(id);
        if (!details[id] || details[id].state === "error") loadDetail(id);
      }
      return next;
    });
  };

  // #<session_id>: open it, highlight it and scroll to it
  const anchored = load.state === "ready" && anchor !== null && sessions.some((s) => s.session_id === anchor);
  useEffect(() => {
    if (!anchored || anchor === null) return;
    setOpen((prev) => new Set(prev).add(anchor));
    loadDetail(anchor);
    items.current.get(anchor)?.scrollIntoView({ block: "center", behavior: "smooth" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchored, anchor]);

  const groups = useMemo(() => groupByDay(sessions, timeZone), [sessions, timeZone]);

  return (
    <div className="page">
      <header className="page-head">
        <p className="eyebrow">Timeline</p>
        <h1>Sessions, day by day</h1>
        <p className="muted">Each session and the memories it added. Open one to jump into the graph.</p>
      </header>

      {load.state === "loading" && <div className="center-state static"><div className="spinner" />Loading sessions…</div>}
      {load.state === "error" && (
        <div className="glass state-card static"><h2>Cannot read the sessions</h2><p>{load.message}</p></div>
      )}
      {load.state === "ready" && sessions.length === 0 && (
        <div className="glass state-card static"><h2>No sessions yet</h2><p className="muted">Sessions appear here after the room has stored something.</p></div>
      )}
      {load.state === "ready" && anchor !== null && !anchored && (
        <p className="notice-inline glass">Session “{anchor}” is not in the latest {SESSION_LIMIT} sessions.</p>
      )}

      <div className="timeline">
        {groups.map((g) => (
          <section key={g.day} className="day-group">
            <h2 className="day-label">{g.label}</h2>
            <ol className="day-sessions">
              {g.sessions.map((s) => {
                const isOpen = open.has(s.session_id);
                const detail = details[s.session_id];
                return (
                  <li key={s.session_id} data-session={s.session_id}
                      ref={(el) => { if (el) items.current.set(s.session_id, el); else items.current.delete(s.session_id); }}
                      className={`session glass${anchor === s.session_id ? " highlight" : ""}`}>
                    <button type="button" className="session-toggle" aria-expanded={isOpen} onClick={() => toggle(s.session_id)}>
                      <span className="session-time">{time(s.last_ts)}</span>
                      <span className="agent-badge">{s.agent}</span>
                      <span className="session-counts muted">
                        {s.nodes_added} {s.nodes_added === 1 ? "memory" : "memories"} · {s.edges_added} {s.edges_added === 1 ? "link" : "links"}
                      </span>
                      <span className="chevron" aria-hidden="true">{isOpen ? "−" : "+"}</span>
                    </button>
                    {isOpen && (
                      <div className="session-body">
                        <p className="session-id mono muted">{s.session_id}</p>
                        {(!detail || detail.state === "loading") && <div className="spinner small" />}
                        {detail?.state === "error" && <p className="muted">{detail.message}</p>}
                        {detail?.state === "ready" && (detail.data.nodes.length === 0 ? <p className="muted">No memories in this session.</p> : (
                          <ul className="chips-session">
                            {detail.data.nodes.map((n) => (
                              <li key={n.id} className={n.status === "superseded" ? "dimmed" : undefined}>
                                <a href={`#/graph?node=${n.id}`}>
                                  <span className="dot" style={{ background: colorForType(n.type) }} />{n.name}
                                </a>
                              </li>
                            ))}
                          </ul>
                        ))}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          </section>
        ))}
      </div>
    </div>
  );
}
