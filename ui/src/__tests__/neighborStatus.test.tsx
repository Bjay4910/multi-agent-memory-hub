// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { NodePanel } from "../components/NodePanel";
import type { NodeDetail } from "../focus";
import { neighborView } from "../neighborView";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("neighborView", () => {
  it("dims and tags superseded neighbors only", () => {
    const base = { id: 1, name: "X", type: "decision", relation: "decided_in", direction: "in" as const };
    expect(neighborView({ ...base, status: "superseded" })).toEqual({ dimmed: true, tag: "superseded" });
    expect(neighborView({ ...base, status: "active" })).toEqual({ dimmed: false, tag: null });
    expect(neighborView(base)).toEqual({ dimmed: false, tag: null }); // older API without status
  });
});

describe("NodePanel neighbors with the same name", () => {
  it("are told apart by a superseded tag and dimming", () => {
    const node: NodeDetail = {
      id: 4, type: "project", name: "Lumen Notes", summary: "", aliases: [], status: "active", confidence: 0.9,
      created_at: "2026-09-01T10:00:00Z", updated_at: "2026-09-01T10:00:00Z", provenance: [],
      neighbors: [
        { id: 10, name: "Lumen Notes sync storage", type: "decision", relation: "decided_in", direction: "in", status: "superseded" },
        { id: 15, name: "Lumen Notes sync storage", type: "decision", relation: "decided_in", direction: "in", status: "active" },
      ],
    };
    const container = document.createElement("div");
    document.body.appendChild(container);
    act(() => createRoot(container).render(<NodePanel node={node} onSelect={() => {}} onClose={() => {}} />));
    const old = container.querySelector("[data-neighbor='10']")!.closest("li")!;
    const current = container.querySelector("[data-neighbor='15']")!.closest("li")!;
    expect(old.classList.contains("dimmed")).toBe(true);
    expect(old.querySelector(".status-tag")?.textContent).toBe("superseded");
    expect(current.classList.contains("dimmed")).toBe(false);
    expect(current.querySelector(".status-tag")).toBeNull();
    container.remove();
  });
});
