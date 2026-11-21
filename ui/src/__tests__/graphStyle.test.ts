import { describe, expect, it } from "vitest";
import { GRAPH_STYLE, fitPadding, selectedVal } from "../graphStyle";
import { nodeSize } from "../graphData";

describe("graph style", () => {
  it("keeps bloom soft enough that big hubs keep their type colour", () => {
    expect(GRAPH_STYLE.bloomStrength).toBeLessThanOrEqual(0.45);
    expect(GRAPH_STYLE.bloomThreshold).toBeGreaterThanOrEqual(0.5);
  });
  it("caps the node size, including the selected node", () => {
    expect(nodeSize(1_000_000)).toBe(GRAPH_STYLE.maxNodeVal);
    expect(selectedVal(nodeSize(1_000_000))).toBe(GRAPH_STYLE.maxSelectedVal);
    expect(selectedVal(1)).toBeGreaterThan(1);
    // radius is cube-root(val) x relSize: the largest hub stays a modest sphere
    expect(Math.cbrt(GRAPH_STYLE.maxSelectedVal) * GRAPH_STYLE.nodeRelSize).toBeLessThan(14);
  });
  it("fits the cluster into about 60-70% of the canvas", () => {
    const padding = fitPadding(1440, 836);
    const fill = ((836 - 2 * padding) / 836) * GRAPH_STYLE.fitDepthFactor; // measured projection factor
    expect(fill).toBeGreaterThanOrEqual(0.6);
    expect(fill).toBeLessThanOrEqual(0.7);
  });
  it("settles and frames the layout within a few seconds", () => {
    expect(GRAPH_STYLE.cooldownTimeMs).toBeLessThanOrEqual(5000);
  });
});
