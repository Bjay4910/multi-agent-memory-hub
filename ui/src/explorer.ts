// Explorer state: search, filters, sort and page, and the /api/nodes query they map to. Pure.

export type SortKey = "name" | "type" | "confidence" | "updated_at";
export type StatusFilter = "all" | "active" | "superseded";

export interface ExplorerState {
  q: string;
  type: string; // "" = every type
  status: StatusFilter;
  sort: SortKey;
  order: "asc" | "desc";
  page: number; // 0-based
}

export const PAGE_SIZE = 50;
export const initialExplorer: ExplorerState = Object.freeze({
  q: "", type: "", status: "all", sort: "name", order: "asc", page: 0,
}) as ExplorerState;

const NATURAL_ORDER: Record<SortKey, "asc" | "desc"> = { name: "asc", type: "asc", confidence: "desc", updated_at: "desc" };

export const setQuery = (s: ExplorerState, q: string): ExplorerState => ({ ...s, q: q.trim(), page: 0 });
export const setType = (s: ExplorerState, type: string): ExplorerState => ({ ...s, type, page: 0 });
export const setStatus = (s: ExplorerState, status: StatusFilter): ExplorerState => ({ ...s, status, page: 0 });

export function toggleSort(s: ExplorerState, key: SortKey): ExplorerState {
  if (s.sort === key) return { ...s, order: s.order === "asc" ? "desc" : "asc", page: 0 };
  return { ...s, sort: key, order: NATURAL_ORDER[key], page: 0 };
}

export function pageCount(total: number): number {
  return Math.max(1, Math.ceil(total / PAGE_SIZE));
}

export function setPage(s: ExplorerState, page: number, total: number): ExplorerState {
  return { ...s, page: Math.min(Math.max(0, Math.trunc(page)), pageCount(total) - 1) };
}

export function explorerQuery(s: ExplorerState): string {
  const p = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(s.page * PAGE_SIZE), status: s.status,
                                  sort: s.sort, order: s.order });
  if (s.q) p.set("q", s.q.slice(0, 200));
  if (s.type) p.set("type", s.type);
  return `/api/nodes?${p.toString()}`;
}

/** The active row after a key press: arrows move by one, Home/End jump; other keys leave it. */
export function nextRow(index: number, key: string, count: number): number {
  if (count <= 0) return -1;
  switch (key) {
    case "ArrowDown": return Math.min(index + 1, count - 1);
    case "ArrowUp": return Math.max(index - 1, 0);
    case "Home": return 0;
    case "End": return count - 1;
    default: return index;
  }
}
