// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { settle, stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const ROUTES = ["#/", "#/graph", "#/room", "#/timeline", "#/explorer", "#/privacy"];
const EMPTY = {
  "/api/stats": { nodes_by_type: {}, nodes_active: 0, nodes_superseded: 0, edges: 0, sessions: 0 },
  "/api/presence": { agents: [] }, "/api/agents": { agents: [] }, "/api/sessions": { sessions: [] },
  "/api/graph": { nodes: [], edges: [] }, "/api/nodes": { total: 0, items: [] },
};

let container: HTMLDivElement;
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); window.location.hash = ""; });

async function renderAt(hash: string, mode: "demo" | "local") {
  window.location.hash = hash;
  stubApi({ ...EMPTY, "/api/health": { ok: true, mode, schema_version: 2 } });
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(<App />); });
  await act(settle);
  await act(settle);
}

describe("demo banner", () => {
  it.each(ROUTES)("is shown on %s in demo mode", async (hash) => {
    await renderAt(hash, "demo");
    const banner = container.querySelector("[role='note'].demo-banner");
    expect(banner?.textContent).toContain("Fictional sample data. Every name, project and number here is invented for this demo.");
    expect(banner?.textContent).toContain("room dashboard-api");
    expect(banner?.querySelector("a, img")).toBeNull();
    expect(container.querySelector(".topbar .badge-demo")).not.toBeNull(); // the badge stays
  });

  it("is absent in local mode", async () => {
    await renderAt("#/", "local");
    expect(container.querySelector(".demo-banner")).toBeNull();
  });

  it("is absent when the API is unreachable", async () => {
    window.location.hash = "#/";
    stubApi({});
    container = document.createElement("div");
    document.body.appendChild(container);
    await act(async () => { createRoot(container).render(<App />); });
    await act(settle);
    expect(container.querySelector(".demo-banner")).toBeNull();
  });
});
