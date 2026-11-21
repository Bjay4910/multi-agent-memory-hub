// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { NodePanel } from "../components/NodePanel";
import type { NodeDetail } from "../focus";

const EVIL = "<img src=x onerror=alert(1)>";
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const node: NodeDetail = {
  id: 5, type: "project", name: EVIL, summary: `Summary ${EVIL} end`, aliases: [EVIL], status: "active",
  confidence: 0.9, created_at: "2026-09-01T10:00:00Z", updated_at: "2026-11-01T10:00:00Z",
  provenance: [{ agent: EVIL, session_id: EVIL, source: "manual", ts: "2026-09-01T10:00:00Z" }],
  neighbors: [{ id: 6, name: EVIL, type: "skill", relation: "requires", direction: "out" }],
};

let container: HTMLDivElement;
afterEach(() => container?.remove());

describe("NodePanel", () => {
  it("renders hostile strings as text and creates no element from them", () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    act(() => createRoot(container).render(<NodePanel node={node} onSelect={() => {}} onClose={() => {}} />));
    expect(container.textContent).toContain(EVIL);
    expect(container.textContent).toContain(`Summary ${EVIL} end`);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.innerHTML).toContain("&lt;img src=x onerror=alert(1)&gt;");
  });
  it("lists neighbors as buttons that select them", () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    const picked: number[] = [];
    act(() => createRoot(container).render(<NodePanel node={node} onSelect={(id) => picked.push(id)} onClose={() => {}} />));
    const button = container.querySelector<HTMLButtonElement>("button[data-neighbor='6']");
    expect(button).not.toBeNull();
    act(() => button!.click());
    expect(picked).toEqual([6]);
  });
});
