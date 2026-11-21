// The Room: merge per-agent stats (/api/agents) with presence (/api/presence). Pure; never invents values.

export type RoomStatus = "working" | "idle" | "offline" | "unknown";

export interface AgentStats {
  agent: string;
  nodes_added: number;
  sessions: number;
  last_ts: string;
  last_session_id: string | null;
}

export interface PresenceEntry {
  agent: string;
  effective_status: string;
  age_seconds: number | null;
}

export interface RoomCard {
  agent: string;
  label: string;
  status: RoomStatus;
  ageSeconds: number | null;
  nodesAdded: number | null;
  sessions: number | null;
  lastSessionId: string | null;
}

export const KNOWN_AGENTS = ["claude", "gemini", "hermes"] as const;
const LABELS: Record<string, string> = { claude: "Claude", gemini: "Gemini", hermes: "Hermes" };

/** Presence available: its effective_status, or "offline" if the agent has no entry. Not available: "unknown". */
export function statusOf(entry: PresenceEntry | undefined, presenceAvailable: boolean): RoomStatus {
  if (!presenceAvailable) return "unknown";
  if (!entry) return "offline";
  return (["working", "idle", "offline"] as const).find((s) => s === entry.effective_status) ?? "unknown";
}

export function formatAge(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "—";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

export function mergeAgents(stats: AgentStats[] | null, presence: PresenceEntry[] | null, nowMs: number): RoomCard[] {
  const statsBy = new Map((stats ?? []).map((a) => [a.agent, a]));
  const presenceBy = new Map((presence ?? []).map((p) => [p.agent, p]));
  const others = [...new Set([...presenceBy.keys(), ...statsBy.keys()])]
    .filter((a) => !(KNOWN_AGENTS as readonly string[]).includes(a)).sort();
  return [...KNOWN_AGENTS, ...others].map((agent) => {
    const s = statsBy.get(agent);
    const p = presenceBy.get(agent);
    let ageSeconds: number | null = p?.age_seconds ?? null;
    if (ageSeconds === null && s?.last_ts) {
      const t = Date.parse(s.last_ts);
      ageSeconds = Number.isNaN(t) ? null : Math.max(0, (nowMs - t) / 1000);
    }
    return {
      agent, label: LABELS[agent] ?? agent, status: statusOf(p, presence !== null), ageSeconds,
      nodesAdded: s ? s.nodes_added : null, sessions: s ? s.sessions : null, lastSessionId: s?.last_session_id ?? null,
    };
  });
}
