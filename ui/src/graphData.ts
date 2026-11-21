// Pure transforms from /api/graph to what the 3D graph draws. No DOM, no network.

export type NodeStatus = "active" | "superseded";

export interface ApiGraphNode {
  id: number;
  type: string;
  name: string;
  status: NodeStatus;
  confidence: number;
  degree: number;
}

export interface ApiEdge {
  id: number;
  src: number;
  dst: number;
  relation: string;
}

export interface ApiGraph {
  nodes: ApiGraphNode[];
  edges: ApiEdge[];
}

export interface GNode extends ApiGraphNode {
  color: string;
  val: number;
  dimmed: boolean;
}

export interface GLink {
  id: number | string;
  source: number;
  target: number;
  relation: string;
}

export interface GraphData {
  nodes: GNode[];
  links: GLink[];
}

export const BACKGROUND = "#05060d";

// One distinct colour per node type the extractor knows (room.memory.store.NODE_TYPES).
export const TYPE_COLORS: Readonly<Record<string, string>> = {
  person: "#7dd3fc",
  organization: "#f472b6",
  project: "#a78bfa",
  decision: "#22d3ee",
  fact: "#60a5fa",
  task: "#fb923c",
  document: "#cbd5e1",
  preference: "#e879f9",
  application: "#fbbf24",
  skill: "#34d399",
};

const FALLBACK_COLORS = ["#f87171", "#a3e635", "#2dd4bf", "#818cf8", "#facc15"];

export function colorForType(type: string): string {
  const known = TYPE_COLORS[type];
  if (known) return known;
  let h = 0;
  for (const ch of type) h = (h * 31 + ch.codePointAt(0)!) >>> 0;
  return FALLBACK_COLORS[h % FALLBACK_COLORS.length];
}

function channels(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** ``hex`` mixed toward the page background: 0 keeps it, 1 is the background. */
export function dimColor(hex: string, amount = 0.68): string {
  const [r, g, b] = channels(hex);
  const [br, bg, bb] = channels(BACKGROUND);
  const mix = (a: number, z: number) => Math.round(a + (z - a) * amount).toString(16).padStart(2, "0");
  return `#${mix(r, br)}${mix(g, bg)}${mix(b, bb)}`;
}

const MAX_DEGREE_FOR_SIZE = 64;

/** Node volume for 3d-force-graph's ``nodeVal``: 1 for no links, growing with degree, capped. */
export function nodeSize(degree: number): number {
  const d = Math.min(Math.max(degree, 0), MAX_DEGREE_FOR_SIZE);
  return 1 + d * 1.5;
}

export function toGNode(n: ApiGraphNode): GNode {
  const dimmed = n.status === "superseded";
  const base = colorForType(n.type);
  return { ...n, color: dimmed ? dimColor(base) : base, val: nodeSize(n.degree), dimmed };
}

export function toGraphData(api: ApiGraph): GraphData {
  const nodes = api.nodes.map(toGNode);
  const ids = new Set(nodes.map((n) => n.id));
  const links = api.edges
    .filter((e) => ids.has(e.src) && ids.has(e.dst))
    .map((e) => ({ id: e.id, source: e.src, target: e.dst, relation: e.relation }));
  return { nodes, links };
}

export interface LegendEntry {
  type: string;
  color: string;
  count: number;
}

export function legendFor(nodes: readonly GNode[]): LegendEntry[] {
  const counts = new Map<string, number>();
  for (const n of nodes) counts.set(n.type, (counts.get(n.type) ?? 0) + 1);
  return [...counts.keys()].sort().map((type) => ({ type, color: colorForType(type), count: counts.get(type)! }));
}
