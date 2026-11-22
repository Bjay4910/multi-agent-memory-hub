// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExplorerPage, drawerLayout } from "../components/ExplorerPage";
import { settle, stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const item = { id: 1, type: "project", name: "Alpha", status: "active", confidence: 0.8, updated_at: "2026-10-01T00:00:00Z" };
const detail = { ...item, summary: "About Alpha", aliases: [], created_at: "2026-10-01T00:00:00Z", provenance: [], neighbors: [] };

let container: HTMLDivElement;
beforeEach(() => {
  stubApi({ "/api/nodes": { total: 1, items: [item] }, "/api/stats": { nodes_by_type: { project: 1 } }, "/api/node/1": detail });
});
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); });

async function renderWidth(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(<ExplorerPage />); });
  await act(settle);
}

describe("Explorer polish", () => {
  it("styles every column header the same way, Status included", async () => {
    await renderWidth(1280);
    const heads = [...container.querySelectorAll("thead th")];
    expect(heads).toHaveLength(5);
    for (const th of heads) expect(th.firstElementChild?.classList.contains("th-label"), th.textContent ?? "").toBe(true);
    expect(heads[4].textContent).toContain("Status");
  });

  it("puts Open in graph inside the drawer, as its footer", async () => {
    await renderWidth(1280);
    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    await act(settle);
    const link = container.querySelector(".drawer > .drawer-footer > a.open-in-graph");
    expect(link?.getAttribute("href")).toBe("#/graph?node=1");
    expect(container.querySelector(".drawer > a.open-in-graph")).toBeNull();
  });

  it("sits beside the table from 1100 px and overlays it below", async () => {
    expect(drawerLayout(1100)).toBe("side");
    expect(drawerLayout(1440)).toBe("side");
    expect(drawerLayout(1099)).toBe("overlay");
    await renderWidth(1280);
    await act(async () => container.querySelector<HTMLTableRowElement>("tbody tr")!.click());
    await act(settle);
    expect(container.querySelector(".explorer-page")!.classList.contains("drawer-side")).toBe(true);
    await act(async () => {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: 900 });
      window.dispatchEvent(new Event("resize"));
    });
    expect(container.querySelector(".explorer-page")!.classList.contains("drawer-overlay")).toBe(true);
    expect(container.querySelector(".explorer-page")!.classList.contains("drawer-side")).toBe(false);
  });
});
