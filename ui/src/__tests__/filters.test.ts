import { describe, expect, it } from "vitest";
import { clearTypes, graphQuery, initialFilters, setIncludeSuperseded, toggleType } from "../filters";

describe("filter state", () => {
  it("starts with all types, active only, limit 500", () => {
    expect(initialFilters).toEqual({ types: [], includeSuperseded: false, limit: 500 });
    expect(graphQuery(initialFilters)).toBe("/api/graph?limit=500&status=active");
  });
  it("toggles a type on and off without mutating", () => {
    const a = toggleType(initialFilters, "project");
    const b = toggleType(a, "person");
    expect(initialFilters.types).toEqual([]);
    expect(b.types).toEqual(["person", "project"]);
    expect(toggleType(b, "project").types).toEqual(["person"]);
    expect(clearTypes(b).types).toEqual([]);
  });
  it("re-queries the graph with the chosen types and status", () => {
    const s = setIncludeSuperseded(toggleType(toggleType(initialFilters, "skill"), "project"), true);
    expect(graphQuery(s)).toBe("/api/graph?limit=500&status=all&types=project%2Cskill");
  });
});
