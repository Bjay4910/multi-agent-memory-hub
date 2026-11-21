// Read-only client for `room dashboard-api` (same origin; the dev server proxies /api).

import type { ApiGraph } from "./graphData";
import type { NodeDetail } from "./focus";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { method: "GET", headers: { Accept: "application/json" }, signal, credentials: "omit" });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError("The dashboard API is not reachable. Is `room dashboard-api` running?", 0);
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const message = typeof (body as { error?: unknown })?.error === "string" ? (body as { error: string }).error
      : `The dashboard API answered ${res.status}.`;
    throw new ApiError(message, res.status);
  }
  return body as T;
}

export interface Health {
  ok: boolean;
  mode: "demo" | "local";
  schema_version?: number;
  error?: string;
}

export interface SearchResult {
  id: number;
  type: string;
  name: string;
  status: string;
  confidence: number;
  relevance: number;
}

export interface Stats {
  nodes_by_type: Record<string, number>;
  nodes_active: number;
  nodes_superseded: number;
  edges: number;
  sessions: number;
}

export const api = {
  health: (signal?: AbortSignal) => getJson<Health>("/api/health", signal),
  graph: (query: string, signal?: AbortSignal) => getJson<ApiGraph>(query, signal),
  node: (id: number, signal?: AbortSignal) => getJson<NodeDetail>(`/api/node/${Math.trunc(id)}`, signal),
  search: (q: string, signal?: AbortSignal) =>
    getJson<{ results: SearchResult[] }>(`/api/search?q=${encodeURIComponent(q.slice(0, 200))}`, signal),
  stats: (signal?: AbortSignal) => getJson<Stats>("/api/stats", signal),
};
