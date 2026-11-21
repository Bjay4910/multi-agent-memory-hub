import type { Neighbor } from "./focus";

/** How a neighbor is shown in the node panel: superseded ones are dimmed and tagged, so duplicate names differ. */
export function neighborView(n: Neighbor): { dimmed: boolean; tag: "superseded" | null } {
  return n.status === "superseded" ? { dimmed: true, tag: "superseded" } : { dimmed: false, tag: null };
}
