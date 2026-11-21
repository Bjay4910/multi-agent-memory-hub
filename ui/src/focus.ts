// What to do when a search result is chosen: focus it, add it through its loaded neighbors, or say it is
// outside the loaded graph. Pure.

import { toGNode, type GLink, type GNode, type GraphData, type NodeStatus } from "./graphData";

export interface Neighbor {
  id: number;
  name: string;
  type: string;
  relation: string;
  direction: "in" | "out";
  status?: NodeStatus; // present when asked for with ?neighbor_status=1
}

export interface Provenance {
  agent: string;
  session_id: string;
  source: string;
  ts: string;
}

export interface NodeDetail {
  id: number;
  type: string;
  name: string;
  summary: string;
  aliases: string[];
  status: NodeStatus;
  confidence: number;
  created_at: string;
  updated_at: string;
  provenance: Provenance[];
  neighbors: Neighbor[];
}

export type FocusPlan =
  | { kind: "focus"; id: number }
  | { kind: "need-detail"; id: number }
  | { kind: "add"; node: GNode; links: GLink[] }
  | { kind: "outside"; id: number; name: string };

export function planFocus(id: number, graph: GraphData, detail?: NodeDetail): FocusPlan {
  const loaded = new Set(graph.nodes.map((n) => n.id));
  if (loaded.has(id)) return { kind: "focus", id };
  if (!detail || detail.id !== id) return { kind: "need-detail", id };
  const anchors = detail.neighbors.filter((n) => loaded.has(n.id));
  if (!anchors.length) return { kind: "outside", id, name: detail.name };
  const links = anchors.map((n) => ({
    id: `focus-${id}-${n.id}-${n.relation}`,
    source: n.direction === "out" ? id : n.id,
    target: n.direction === "out" ? n.id : id,
    relation: n.relation,
  }));
  const node = toGNode({ id, type: detail.type, name: detail.name, status: detail.status,
                         confidence: detail.confidence, degree: anchors.length });
  return { kind: "add", node, links };
}

export function applyFocusPlan(graph: GraphData, plan: FocusPlan): GraphData {
  if (plan.kind !== "add") return graph;
  return { nodes: [...graph.nodes, plan.node], links: [...graph.links, ...plan.links] };
}
