// Timeline: sessions grouped by calendar day (newest first). Pure.

export interface SessionSummary {
  session_id: string;
  agent: string;
  first_ts: string;
  last_ts: string;
  nodes_added: number;
  edges_added: number;
}

export interface SessionNode {
  id: number;
  name: string;
  type: string;
  status: string;
}

export interface SessionDetail extends Omit<SessionSummary, "nodes_added" | "edges_added"> {
  nodes: SessionNode[];
  edges: { src: number; dst: number; relation: string }[];
}

export interface DayGroup {
  day: string; // YYYY-MM-DD in the given time zone
  label: string;
  sessions: SessionSummary[];
}

export function groupByDay(sessions: readonly SessionSummary[], timeZone?: string): DayGroup[] {
  const key = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" });
  const label = new Intl.DateTimeFormat(undefined, { timeZone, weekday: "long", day: "numeric", month: "long", year: "numeric" });
  const groups: DayGroup[] = [];
  for (const s of sessions) {
    const when = new Date(s.last_ts);
    const day = Number.isNaN(when.getTime()) ? "unknown" : key.format(when);
    let g = groups.find((x) => x.day === day);
    if (!g) {
      g = { day, label: day === "unknown" ? "Unknown date" : label.format(when), sessions: [] };
      groups.push(g);
    }
    g.sessions.push(s);
  }
  return groups;
}
