import { describe, expect, it } from "vitest";
import { formatAge, mergeAgents, statusOf } from "../room";

describe("statusOf", () => {
  it("maps effective_status, missing agents and missing presence", () => {
    expect(statusOf({ agent: "a", effective_status: "working", age_seconds: 3 }, true)).toBe("working");
    expect(statusOf({ agent: "a", effective_status: "idle", age_seconds: 300 }, true)).toBe("idle");
    expect(statusOf({ agent: "a", effective_status: "offline", age_seconds: 9000 }, true)).toBe("offline");
    expect(statusOf(undefined, true)).toBe("offline");
    expect(statusOf(undefined, false)).toBe("unknown");
    expect(statusOf({ agent: "a", effective_status: "weird" as never, age_seconds: 1 }, true)).toBe("unknown");
  });
});

describe("formatAge", () => {
  it("formats seconds, minutes, hours and days, and a dash for unknown", () => {
    expect(formatAge(12)).toBe("12 s ago");
    expect(formatAge(0.4)).toBe("just now");
    expect(formatAge(140)).toBe("2 min ago");
    expect(formatAge(3 * 3600 + 5)).toBe("3 h ago");
    expect(formatAge(9 * 86400)).toBe("9 d ago");
    expect(formatAge(null)).toBe("—");
    expect(formatAge(-5)).toBe("just now");
  });
});

describe("mergeAgents", () => {
  const now = Date.parse("2026-11-01T12:00:00Z");
  it("always lists claude, gemini, hermes first, then any other agent seen", () => {
    const cards = mergeAgents(
      [{ agent: "zeta", nodes_added: 2, sessions: 1, last_ts: "2026-11-01T11:00:00Z", last_session_id: "s-z" },
       { agent: "gemini", nodes_added: 40, sessions: 4, last_ts: "2026-10-31T12:00:00Z", last_session_id: "s-g" }],
      [{ agent: "claude", effective_status: "working", age_seconds: 12 }, { agent: "alpha", effective_status: "idle", age_seconds: 5 }],
      now);
    expect(cards.map((c) => c.agent)).toEqual(["claude", "gemini", "hermes", "alpha", "zeta"]);
    const byName = Object.fromEntries(cards.map((c) => [c.agent, c]));
    expect(byName.claude).toMatchObject({ label: "Claude", status: "working", ageSeconds: 12, nodesAdded: null });
    expect(byName.gemini).toMatchObject({ status: "offline", ageSeconds: 86400, nodesAdded: 40, sessions: 4, lastSessionId: "s-g" });
    expect(byName.hermes).toMatchObject({ status: "offline", ageSeconds: null, nodesAdded: null, sessions: null });
  });
  it("never invents values when presence and stats are unavailable", () => {
    const cards = mergeAgents(null, null, now);
    expect(cards.map((c) => [c.agent, c.status, c.ageSeconds, c.nodesAdded, c.sessions])).toEqual([
      ["claude", "unknown", null, null, null], ["gemini", "unknown", null, null, null], ["hermes", "unknown", null, null, null]]);
  });
});
