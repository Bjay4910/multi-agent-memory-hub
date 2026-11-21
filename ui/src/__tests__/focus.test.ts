import { describe, expect, it } from "vitest";
import { applyFocusPlan, planFocus, type NodeDetail } from "../focus";
import { toGraphData } from "../graphData";

const graph = toGraphData({
  nodes: [
    { id: 1, type: "person", name: "Sam Rivera", status: "active", confidence: 0.9, degree: 1 },
    { id: 2, type: "project", name: "Lumen Notes", status: "active", confidence: 0.9, degree: 1 },
  ],
  edges: [{ id: 10, src: 1, dst: 2, relation: "works_on" }],
});

function detail(id: number, neighbors: NodeDetail["neighbors"]): NodeDetail {
  return {
    id, type: "skill", name: "TypeScript", summary: "", aliases: [], status: "active", confidence: 0.8,
    created_at: "2026-09-01T10:00:00Z", updated_at: "2026-09-01T10:00:00Z", provenance: [], neighbors,
  };
}

describe("search-result focus", () => {
  it("focuses a node that is already loaded", () => {
    expect(planFocus(2, graph)).toEqual({ kind: "focus", id: 2 });
  });
  it("needs the node detail for a node that is not loaded", () => {
    expect(planFocus(3, graph)).toEqual({ kind: "need-detail", id: 3 });
  });
  it("adds an outside node through its loaded neighbors, with correct link directions", () => {
    const plan = planFocus(3, graph, detail(3, [
      { id: 2, name: "Lumen Notes", type: "project", relation: "requires", direction: "in" },
      { id: 7, name: "Not loaded", type: "skill", relation: "mentions", direction: "out" },
    ]));
    expect(plan.kind).toBe("add");
    if (plan.kind !== "add") return;
    expect(plan.node).toMatchObject({ id: 3, name: "TypeScript", degree: 1 });
    expect(plan.links).toEqual([{ id: "focus-3-2-requires", source: 2, target: 3, relation: "requires" }]);
    const merged = applyFocusPlan(graph, plan);
    expect(merged.nodes.map((n) => n.id)).toEqual([1, 2, 3]);
    expect(merged.links).toHaveLength(2);
    expect(graph.nodes).toHaveLength(2); // pure
  });
  it("says so when no neighbor is loaded", () => {
    const plan = planFocus(3, graph, detail(3, [
      { id: 7, name: "Not loaded", type: "skill", relation: "mentions", direction: "out" },
    ]));
    expect(plan).toEqual({ kind: "outside", id: 3, name: "TypeScript" });
    expect(applyFocusPlan(graph, plan)).toBe(graph);
  });
});
