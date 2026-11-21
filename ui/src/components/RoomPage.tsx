// The Room: one card per agent with live presence (polled every 5 s while the tab is visible) and the
// memories and sessions it contributed (from provenance). Unknown values are shown as unknown, never guessed.

import { useCallback, useState } from "react";
import { api, ApiError } from "../api";
import { formatAge, mergeAgents, type AgentStats, type PresenceEntry, type RoomStatus } from "../room";
import { usePolling } from "../usePolling";

const STATUS_TEXT: Record<RoomStatus, string> = { working: "Working", idle: "Idle", offline: "Offline", unknown: "Unknown" };
const AGENT_HUE: Record<string, string> = { claude: "#f59e0b", gemini: "#60a5fa", hermes: "#a78bfa" };

type Load = { state: "loading" } | { state: "ready" } | { state: "error"; message: string };

export function RoomPage() {
  const [stats, setStats] = useState<AgentStats[] | null>(null);
  const [presence, setPresence] = useState<PresenceEntry[] | null>(null);
  const [synthetic, setSynthetic] = useState(false);
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [now, setNow] = useState(() => Date.now());

  const refresh = useCallback(() => {
    void Promise.allSettled([api.agents(), api.presence()]).then(([a, p]) => {
      setNow(Date.now());
      if (p.status === "fulfilled") {
        setPresence(p.value.agents);
        setSynthetic(Boolean(p.value.synthetic));
      } else {
        setPresence(null);
      }
      if (a.status === "fulfilled") {
        setStats(a.value.agents);
        setLoad({ state: "ready" });
      } else {
        setStats(null);
        setLoad((prev) => (prev.state === "ready" && p.status === "fulfilled") ? prev
          : { state: "error", message: (a.reason as ApiError).message });
      }
    });
  }, []);
  usePolling(refresh, 5000);

  const cards = mergeAgents(stats, presence, now);
  const nothingSeen = load.state === "ready" && (stats?.length ?? 0) === 0 && (presence?.length ?? 0) === 0;

  return (
    <div className="page">
      <header className="page-head">
        <p className="eyebrow">The Room</p>
        <h1>Who is here</h1>
        <p className="muted">Live presence of each agent, refreshed every few seconds while this tab is open.
          {synthetic && " Presence here is a fixed sample shown with the demo data."}</p>
      </header>

      {load.state === "loading" && <div className="center-state static"><div className="spinner" />Loading agents…</div>}
      {load.state === "error" && (
        <div className="glass state-card static">
          <h2>Cannot read the agents</h2>
          <p>{load.message}</p>
          <p className="muted">Is <code>room dashboard-api</code> running?</p>
        </div>
      )}
      {nothingSeen && <p className="notice-inline glass">No agent has been seen yet. Cards fill in after the first session.</p>}

      {load.state !== "loading" && (
        <section className="agent-grid">
          {cards.map((c) => (
            <article key={c.agent} className={`agent-card glass status-${c.status}`} data-agent={c.agent}>
              <header className="agent-head">
                <span className="agent-avatar" style={{ ["--hue" as string]: AGENT_HUE[c.agent] ?? "#22d3ee" }} aria-hidden="true">
                  {c.label.slice(0, 1).toUpperCase()}
                </span>
                <div className="agent-title">
                  <h2>{c.label}</h2>
                  <span className="muted">{c.status === "unknown" ? "Presence unknown" : `Last seen ${formatAge(c.ageSeconds)}`}</span>
                </div>
                <span className={`status-pill ${c.status}`}><span className="status-dot" />{STATUS_TEXT[c.status]}</span>
              </header>
              <dl className="agent-stats">
                <div><dt>Memories contributed</dt><dd>{c.nodesAdded === null ? "—" : String(c.nodesAdded)}</dd></div>
                <div><dt>Sessions</dt><dd>{c.sessions === null ? "—" : String(c.sessions)}</dd></div>
              </dl>
              {c.lastSessionId
                ? <a className="last-session" href={`#/timeline#${encodeURIComponent(c.lastSessionId)}`}>Last session →</a>
                : <span className="muted last-session-none">No sessions yet</span>}
            </article>
          ))}
        </section>
      )}
    </div>
  );
}
