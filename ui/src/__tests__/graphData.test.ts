import { describe, expect, it } from "vitest";
import {
  BACKGROUND, TYPE_COLORS, colorForType, dimColor, legendFor, nodeSize, toGraphData, type ApiGraph,
} from "../graphData";

const api: ApiGraph = {
  nodes: [
    { id: 1, type: "person", name: "Sam Rivera", status: "active", confidence: 0.9, degree: 3 },
    { id: 2, type: "project", name: "Lumen Notes", status: "active", confidence: 0.9, degree: 1 },
    { id: 3, type: "decision", name: "Old decision", status: "superseded", confidence: 0.5, degree: 0 },
    { id: 4, type: "made_up_type", name: "Odd", status: "active", confidence: 0.4, degree: 0 },
  ],
  edges: [
    { id: 10, src: 1, dst: 2, relation: "works_on" },
    { id: 11, src: 2, dst: 99, relation: "requires" }, // 99 is not loaded
  ],
};

describe("colours", () => {
  it("gives every known type a distinct colour", () => {
    const colours = Object.values(TYPE_COLORS);
    expect(new Set(colours).size).toBe(colours.length);
    expect(colorForType("person")).toBe(TYPE_COLORS.person);
  });
  it("gives an unknown type a stable fallback colour that is not a known type's", () => {
    const c = colorForType("made_up_type");
    expect(c).toBe(colorForType("made_up_type"));
    expect(c).toMatch(/^#[0-9a-f]{6}$/);
  });
  it("dims a colour toward the background", () => {
    expect(dimColor("#ffffff", 1)).toBe(BACKGROUND);
    expect(dimColor("#ffffff", 0)).toBe("#ffffff");
    expect(dimColor("#ffffff", 0.5)).not.toBe("#ffffff");
  });
});

describe("size", () => {
  it("grows with degree, starts at 1 and is capped", () => {
    expect(nodeSize(0)).toBe(1);
    expect(nodeSize(1)).toBeGreaterThan(nodeSize(0));
    expect(nodeSize(9)).toBeGreaterThan(nodeSize(4));
    expect(nodeSize(10_000)).toBe(nodeSize(100_000));
  });
});

describe("toGraphData", () => {
  it("maps nodes with colour, size and dimming", () => {
    const g = toGraphData(api);
    const byId = Object.fromEntries(g.nodes.map((n) => [n.id, n]));
    expect(byId[1]).toMatchObject({ name: "Sam Rivera", color: TYPE_COLORS.person, val: nodeSize(3), dimmed: false });
    expect(byId[3].dimmed).toBe(true);
    expect(byId[3].color).toBe(dimColor(TYPE_COLORS.decision));
  });
  it("keeps only links between loaded nodes, as source/target", () => {
    expect(toGraphData(api).links).toEqual([{ id: 10, source: 1, target: 2, relation: "works_on" }]);
  });
  it("does not mutate its input", () => {
    const copy = JSON.parse(JSON.stringify(api));
    toGraphData(api);
    expect(api).toEqual(copy);
  });
  it("builds a legend of the loaded types with counts", () => {
    const legend = legendFor(toGraphData(api).nodes);
    expect(legend.map((l) => l.type)).toEqual(["decision", "made_up_type", "person", "project"]);
    expect(legend.find((l) => l.type === "person")).toEqual({ type: "person", color: TYPE_COLORS.person, count: 1 });
  });
});
