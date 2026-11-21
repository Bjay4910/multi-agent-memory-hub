// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GraphPage } from "../components/GraphPage";
import { settle, stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const detail = (id: number, name: string, neighbors: unknown[] = []) => ({
  id, type: "project", name, summary: `About ${name}.`, aliases: [], status: "active", confidence: 0.9,
  created_at: "2026-09-01T10:00:00Z", updated_at: "2026-09-01T10:00:00Z", provenance: [], neighbors,
});

let container: HTMLDivElement;
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null); // no WebGL: the list view
  stubApi({
    "/api/graph": { nodes: [{ id: 5, type: "project", name: "Tern Atlas", status: "active", confidence: 0.9, degree: 0 }], edges: [] },
    "/api/stats": { nodes_by_type: { project: 1 }, nodes_active: 1, nodes_superseded: 0, edges: 0, sessions: 1 },
    "/api/node/5": detail(5, "Tern Atlas"),
    "/api/node/9": detail(9, "Far Away Node"),
  });
});
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function render(id: number | null) {
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(<GraphPage initialNodeId={id} />); });
  await act(settle);
  await act(settle);
}

describe("/graph?node=<id>", () => {
  it("opens a loaded node's panel", async () => {
    await render(5);
    expect(container.querySelector(".panel-title")?.textContent).toBe("Tern Atlas");
  });
  it("says so for a node outside the loaded graph with no loaded neighbors, and still shows its details", async () => {
    await render(9);
    expect(container.querySelector(".panel-title")?.textContent).toBe("Far Away Node");
    expect(container.querySelector(".notice")?.textContent).toContain("not in the loaded graph");
  });
  it("does nothing without a node id", async () => {
    await render(null);
    expect(container.querySelector(".panel")).toBeNull();
  });
});
