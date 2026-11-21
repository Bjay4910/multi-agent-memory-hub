// Filter state for the graph page, and the /api/graph query it maps to. Pure.

export interface FilterState {
  types: string[]; // empty = every type
  includeSuperseded: boolean;
  limit: number;
}

export const DEFAULT_LIMIT = 500;

export const initialFilters: FilterState = Object.freeze({ types: [], includeSuperseded: false, limit: DEFAULT_LIMIT }) as FilterState;

export function toggleType(state: FilterState, type: string): FilterState {
  const types = state.types.includes(type) ? state.types.filter((t) => t !== type) : [...state.types, type];
  return { ...state, types: types.sort() };
}

export function clearTypes(state: FilterState): FilterState {
  return { ...state, types: [] };
}

export function setIncludeSuperseded(state: FilterState, includeSuperseded: boolean): FilterState {
  return { ...state, includeSuperseded };
}

export function graphQuery(state: FilterState): string {
  const params = new URLSearchParams({ limit: String(state.limit), status: state.includeSuperseded ? "all" : "active" });
  if (state.types.length) params.set("types", [...state.types].sort().join(","));
  return `/api/graph?${params.toString()}`;
}
