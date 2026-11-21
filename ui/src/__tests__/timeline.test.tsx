// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TimelinePage } from "../components/TimelinePage";
import { groupByDay, type SessionSummary } from "../timeline";
import { settle, stubApi } from "./helpers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const S = (id: string, last: string, agent = "claude"): SessionSummary =>
  ({ session_id: id, agent, first_ts: last, last_ts: last, nodes_added: 3, edges_added: 2 });
const SESSIONS = [
  S("s-3", "2026-10-29T09:10:00Z", "hermes"), S("s-2", "2026-10-29T01:00:00Z", "gemini"), S("s-1", "2026-10-22T17:35:00Z"),
];

describe("groupByDay", () => {
  it("groups newest-first sessions by calendar day in the given time zone", () => {
    const groups = groupByDay(SESSIONS, "UTC");
    expect(groups.map((g) => [g.day, g.sessions.map((s) => s.session_id)])).toEqual([
      ["2026-10-29", ["s-3", "s-2"]], ["2026-10-22", ["s-1"]]]);
    expect(groups[0].label).toContain("29");
  });
  it("puts a session in the local day it happened", () => {
    expect(groupByDay(SESSIONS, "America/Los_Angeles").map((g) => g.day)).toEqual(["2026-10-29", "2026-10-28", "2026-10-22"]);
  });
});

let container: HTMLDivElement;
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  stubApi({
    "/api/sessions": { sessions: SESSIONS },
    "/api/session/": (url: string) => ({
      session_id: decodeURIComponent(url.split("/api/session/")[1]), agent: "claude", first_ts: SESSIONS[0].first_ts,
      last_ts: SESSIONS[0].last_ts, edges: [],
      nodes: [{ id: 7, name: "Tern Atlas", type: "project", status: "active" },
              { id: 8, name: "Old choice", type: "decision", status: "superseded" }],
    }),
  });
});
afterEach(() => { container?.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function render(anchor: string | null = null) {
  container = document.createElement("div");
  document.body.appendChild(container);
  await act(async () => { createRoot(container).render(<TimelinePage anchor={anchor} timeZone="UTC" />); });
  await act(settle);
}

describe("TimelinePage", () => {
  it("shows day groups, and expands a session into node chips that link to the graph", async () => {
    await render();
    expect(container.querySelectorAll(".day-group")).toHaveLength(2);
    const item = container.querySelector<HTMLElement>("[data-session='s-2']")!;
    expect(item.querySelector(".chips-session")).toBeNull();
    await act(async () => item.querySelector<HTMLButtonElement>("button.session-toggle")!.click());
    await act(settle);
    const links = [...item.querySelectorAll<HTMLAnchorElement>(".chips-session a")].map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([["Tern Atlas", "#/graph?node=7"], ["Old choice", "#/graph?node=8"]]);
    expect(item.querySelector("button.session-toggle")!.getAttribute("aria-expanded")).toBe("true");
    await act(async () => item.querySelector<HTMLButtonElement>("button.session-toggle")!.click());
    expect(item.querySelector(".chips-session")).toBeNull();
  });

  it("scrolls to, highlights and opens the session named in the anchor", async () => {
    await render("s-1");
    const item = container.querySelector<HTMLElement>("[data-session='s-1']")!;
    expect(item.classList.contains("highlight")).toBe(true);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    expect(item.querySelector(".chips-session")).not.toBeNull();
    expect(container.querySelector("[data-session='s-3']")!.classList.contains("highlight")).toBe(false);
  });

  it("says so when the anchored session is not in the list", async () => {
    await render("missing-session");
    expect(container.textContent).toContain("not in the latest");
  });
});
