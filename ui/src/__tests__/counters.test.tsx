// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Counters } from "../components/Counters";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); });

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

async function render() {
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(<Counters />); });
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  return Object.fromEntries([...container.querySelectorAll("[data-counter]")].map(
    (el) => [el.getAttribute("data-counter"), el.querySelector(".counter-value")!.textContent]));
}

describe("Counters", () => {
  it("shows the exact numbers from the API", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => url.startsWith("/api/stats")
      ? json({ nodes_by_type: {}, nodes_active: 103, nodes_superseded: 14, edges: 213, sessions: 12 })
      : json({ agents: [{ agent: "claude", effective_status: "working", age_seconds: 3 },
                        { agent: "gemini", effective_status: "idle", age_seconds: 900 }] })));
    expect(await render()).toEqual({ memories: "103", connections: "213", agents: "1", sessions: "12" });
  });

  it("shows a dash for every counter when the API is unreachable, never a made-up number", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    expect(await render()).toEqual({ memories: "—", connections: "—", agents: "—", sessions: "—" });
  });

  it("shows a dash only for the part that failed", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => url.startsWith("/api/stats")
      ? json({ error: "No memory yet" }, 503)
      : json({ agents: [] })));
    expect(await render()).toEqual({ memories: "—", connections: "—", agents: "0", sessions: "—" });
  });
});
