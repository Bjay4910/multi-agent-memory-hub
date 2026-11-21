// Visual tuning for the 3D graph, kept in one place so it can be tested.

export const GRAPH_STYLE = {
  // Bloom: a middle value. F3 (0.6 / 0.18) washed hubs out to white when zoomed in; F4 (0.32 / 0.62) left nodes
  // matte. Measured on screenshots with ui/scripts/measure-glow.mjs (npm run check:glow).
  bloomStrength: 0.55,
  bloomRadius: 0.4,
  bloomThreshold: 0.5,
  // Node materials glow faintly in their own colour, so they look luminous without turning white.
  emissiveIntensity: 0.1,
  // 3d-force-graph draws a node as a sphere of radius cbrt(val) * nodeRelSize.
  nodeRelSize: 4,
  maxNodeVal: 24,
  maxSelectedVal: 34,
  // zoomToFit padding as a share of the shorter canvas side. zoomToFit frames the cluster's 3D bounding box,
  // and the projected cluster covered about 75% of the framed height (measured from a screenshot, stage F4),
  // so this padding leaves the cluster filling about 65% of the canvas height.
  fitPaddingRatio: 0.06,
  fitDepthFactor: 0.75,
  // the layout settles (and is framed) after this; 3d-force-graph's default is 15 s
  warmupTicks: 40,
  cooldownTimeMs: 4000,
} as const;

export function selectedVal(val: number): number {
  return Math.min(val * 1.5 + 2, GRAPH_STYLE.maxSelectedVal);
}

export function fitPadding(width: number, height: number): number {
  return Math.round(Math.min(width, height) * GRAPH_STYLE.fitPaddingRatio);
}
