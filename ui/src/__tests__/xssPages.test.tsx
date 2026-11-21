// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RoomPage } from "../components/RoomPage";
import { TimelinePage } from "../components/TimelinePage";
import { settle, stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const EVIL = "<img src=x onerror=alert(1)>";
let container: HTMLDivElement;
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); });

async function mount(node: React.ReactNode) {
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(node); });
  await act(settle);
}

describe("hostile names from the API render as text", () => {
  it("in The Room", async () => {
    stubApi({
      "/api/agents": { agents: [{ agent: EVIL, nodes_added: 1, sessions: 1, last_ts: "2026-10-01T00:00:00Z", last_session_id: EVIL }] },
      "/api/presence": { agents: [{ agent: EVIL, effective_status: "working", age_seconds: 3 }] },
    });
    await mount(<RoomPage />);
    expect(container.textContent).toContain(EVIL);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    const link = container.querySelector<HTMLAnchorElement>("a.last-session");
    expect(link?.getAttribute("href")).toBe(`#/timeline#${encodeURIComponent(EVIL)}`);
  });

  it("in the Timeline, including a session's node chips", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    const s = { session_id: "s-1", agent: EVIL, first_ts: "2026-10-01T00:00:00Z", last_ts: "2026-10-01T00:00:00Z", nodes_added: 1, edges_added: 0 };
    stubApi({
      "/api/sessions": { sessions: [s] },
      "/api/session/": { ...s, nodes: [{ id: 3, name: EVIL, type: "fact", status: "active" }], edges: [] },
    });
    await mount(<TimelinePage anchor="s-1" />);
    expect(container.textContent).toContain(EVIL);
    expect(container.querySelector(".chips-session a")?.textContent).toBe(EVIL);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
  });
});
