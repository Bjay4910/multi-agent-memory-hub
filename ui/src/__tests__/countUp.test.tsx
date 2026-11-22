// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Counters } from "../components/Counters";
import { stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const STATS = { nodes_by_type: {}, nodes_active: 103, nodes_superseded: 14, edges: 214, sessions: 12 };
const PRESENCE = { agents: [{ agent: "claude", effective_status: "working", age_seconds: 12 }] };

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval",
                              "requestAnimationFrame", "cancelAnimationFrame", "performance", "Date"] });
});
afterEach(() => { vi.useRealTimers(); container?.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function reducedMotion(on: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: on && q.includes("reduce"), media: q,
    addEventListener() {}, removeEventListener() {} }));
}

async function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(<Counters animate />); });
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}
const values = () => Object.fromEntries([...container.querySelectorAll("[data-counter]")].map(
  (el) => [el.getAttribute("data-counter"), el.querySelector(".counter-value")!.textContent]));

describe("Counters count-up", () => {
  it("shows a dash while loading, counts up, and lands exactly on the API values", async () => {
    reducedMotion(false);
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    // stats are held back by the gate, so the loading state can be seen first
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).startsWith("/api/stats")) await gate;
      const body = String(url).startsWith("/api/stats") ? STATS : PRESENCE;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    await mount();
    expect(values()).toEqual({ memories: "—", connections: "—", agents: "—", sessions: "—" });
    await act(async () => { release(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(400); });
    const mid = Number(values().memories);
    expect(mid).toBeGreaterThan(0);
    expect(mid).toBeLessThan(103);
    await act(async () => { vi.advanceTimersByTime(600); });
    expect(values()).toEqual({ memories: "103", connections: "214", agents: "1", sessions: "12" });
  });

  it("fetches again every time it mounts", async () => {
    reducedMotion(true);
    const fetchMock = stubApi({ "/api/stats": STATS, "/api/presence": PRESENCE });
    await mount();
    act(() => root.unmount());
    container.remove();
    await mount();
    expect(fetchMock.mock.calls.filter((c) => String(c[0]).startsWith("/api/stats"))).toHaveLength(2);
    expect(fetchMock.mock.calls.filter((c) => String(c[0]).startsWith("/api/presence"))).toHaveLength(2);
  });

  it("shows a dash and no digits when the API fails", async () => {
    reducedMotion(false);
    stubApi({});
    await mount();
    await act(async () => { vi.advanceTimersByTime(2000); });
    const text = container.textContent ?? "";
    expect(text).toContain("—");
    expect(text.replace(/[0-9]/g, "")).toBe(text);
  });

  it("skips the animation with reduced motion", async () => {
    reducedMotion(true);
    stubApi({ "/api/stats": STATS, "/api/presence": PRESENCE });
    await mount();
    expect(values()).toEqual({ memories: "103", connections: "214", agents: "1", sessions: "12" });
  });

  it("cancels the animation on unmount", async () => {
    reducedMotion(false);
    stubApi({ "/api/stats": STATS, "/api/presence": PRESENCE });
    await mount();
    await act(async () => { vi.advanceTimersByTime(200); });
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    act(() => root.unmount());
    expect(vi.getTimerCount()).toBe(0);
  });
});
