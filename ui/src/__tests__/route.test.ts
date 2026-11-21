import { describe, expect, it } from "vitest";
import { nodeIdFromParams, parseHash } from "../route";

describe("parseHash", () => {
  it("reads the route, query and anchor from a hash URL", () => {
    expect(parseHash("")).toMatchObject({ route: "", anchor: null });
    expect(parseHash("#/")).toMatchObject({ route: "", anchor: null });
    const g = parseHash("#/graph?node=42");
    expect(g.route).toBe("graph");
    expect(g.params.get("node")).toBe("42");
    expect(parseHash("#/timeline#room-2026-10-29-l12")).toMatchObject({ route: "timeline", anchor: "room-2026-10-29-l12" });
    expect(parseHash("#/timeline#a%3Ab")).toMatchObject({ anchor: "a:b" });
  });
  it("accepts only a positive integer node id", () => {
    const id = (q: string) => nodeIdFromParams(new URLSearchParams(q));
    expect(id("node=42")).toBe(42);
    for (const bad of ["", "node=", "node=0", "node=-3", "node=1.5", "node=abc", "node=1e3", "node=99999999999999999999"]) {
      expect(id(bad), bad).toBeNull();
    }
  });
});
