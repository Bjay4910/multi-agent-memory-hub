import { describe, expect, it } from "vitest";
import { GRAPH_STYLE, fitPadding, selectedVal } from "../graphStyle";
import { nodeSize } from "../graphData";

describe("graph style", () => {
  it("uses a middle glow: luminous nodes that keep their type colour (F5 values; replaced the F4 bounds)", () => {
    expect(GRAPH_STYLE.bloomStrength).toBeGreaterThanOrEqual(0.5);
    expect(GRAPH_STYLE.bloomStrength).toBeLessThanOrEqual(0.6);
    expect(GRAPH_STYLE.bloomThreshold).toBeGreaterThanOrEqual(0.4);
    expect(GRAPH_STYLE.bloomThreshold).toBeLessThanOrEqual(0.5);
  });
  it("tints node materials with a mild emissive glow of their own colour", () => {
    expect(GRAPH_STYLE.emissiveIntensity).toBeGreaterThan(0);
    expect(GRAPH_STYLE.emissiveIntensity).toBeLessThanOrEqual(0.5);
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
