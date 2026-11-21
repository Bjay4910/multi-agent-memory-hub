// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExplorerPage } from "../components/ExplorerPage";
import { settle, stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const EVIL = "<img src=x onerror=alert(1)>";
const item = (id: number, name: string, status = "active") =>
  ({ id, type: "project", name, status, confidence: 0.8, updated_at: "2026-10-01T00:00:00Z" });
const detail = (id: number, name: string) => ({
  id, type: "project", name, summary: `About ${name}`, aliases: [], status: "active", confidence: 0.8,
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", provenance: [], neighbors: [],
});

let container: HTMLDivElement;
let fetchMock: ReturnType<typeof stubApi>;
beforeEach(() => {
  fetchMock = stubApi({
    "/api/nodes": { total: 3, items: [item(1, "Alpha"), item(2, EVIL), item(3, "Gamma", "superseded")] },
    "/api/stats": { nodes_by_type: { project: 3 }, nodes_active: 2, nodes_superseded: 1, edges: 0, sessions: 1 },
    "/api/node/1": detail(1, "Alpha"),
    "/api/node/2": detail(2, EVIL),
  });
});
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); });

async function render() {
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(<ExplorerPage />); });
  await act(settle);
}
const rows = () => [...container.querySelectorAll<HTMLTableRowElement>("tbody tr")];
const key = async (el: Element | Document, k: string) => act(async () => {
  el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true }));
});

describe("ExplorerPage", () => {
  it("lists rows as text, with a dimmed superseded row and a confidence bar", async () => {
    await render();
    expect(rows()).toHaveLength(3);
    expect(rows()[1].textContent).toContain(EVIL);
    expect(container.querySelector("img")).toBeNull();
    expect(rows()[2].classList.contains("dimmed")).toBe(true);
    expect(rows()[0].querySelector(".conf-bar")).not.toBeNull();
    expect(container.textContent).toContain("3 nodes");
  });

  it("sorts by a column when its header is clicked", async () => {
    await render();
    const header = container.querySelector<HTMLButtonElement>("th button[data-sort='confidence']")!;
    await act(async () => header.click());
    await act(settle);
    const last = String(fetchMock.mock.calls.at(-1)![0]);
    expect(last).toContain("sort=confidence") ;
    expect(last).toContain("order=desc");
    expect(header.closest("th")!.getAttribute("aria-sort")).toBe("descending");
  });

  it("moves through rows with the arrow keys, opens the drawer with Enter and closes it with Escape", async () => {
    await render();
    const table = container.querySelector("tbody")!;
    await key(table, "ArrowDown");
    await key(table, "ArrowDown");
    expect(rows()[1].getAttribute("aria-selected")).toBe("true");
    await key(table, "Enter");
    await act(settle);
    const drawer = container.querySelector(".drawer");
    expect(drawer).not.toBeNull();
    expect(drawer!.querySelector(".panel-title")?.textContent).toBe(EVIL);
    expect(drawer!.querySelector("img")).toBeNull();
    expect(drawer!.querySelector<HTMLAnchorElement>("a.open-in-graph")?.getAttribute("href")).toBe("#/graph?node=2");
    await key(document, "Escape");
    expect(container.querySelector(".drawer")).toBeNull();
  });

  it("opens the drawer when a row is clicked", async () => {
    await render();
    await act(async () => rows()[0].click());
    await act(settle);
    expect(container.querySelector(".drawer .panel-title")?.textContent).toBe("Alpha");
    expect(String(fetchMock.mock.calls.find((c) => String(c[0]).startsWith("/api/node/1"))![0])).toContain("neighbor_status=1");
  });

  it("shows an empty state and an error state", async () => {
    stubApi({ "/api/nodes": { total: 0, items: [] }, "/api/stats": { nodes_by_type: {} } });
    await render();
    expect(container.textContent).toContain("No nodes match");
    container.remove();
    stubApi({});
    await render();
    expect(container.querySelector(".state-card")?.textContent).toContain("Cannot");
  });
});
