import { describe, expect, it } from "vitest";
import {
  PAGE_SIZE, explorerQuery, initialExplorer, nextRow, pageCount, setPage, setQuery, setStatus, setType, toggleSort,
} from "../explorer";

describe("explorer state", () => {
  it("starts on page 1 of all nodes by name and builds the API query", () => {
    expect(initialExplorer).toEqual({ q: "", type: "", status: "all", sort: "name", order: "asc", page: 0 });
    expect(explorerQuery(initialExplorer)).toBe("/api/nodes?limit=50&offset=0&status=all&sort=name&order=asc");
  });
  it("filters reset to the first page and appear in the query", () => {
    const s = setStatus(setType(setQuery({ ...initialExplorer, page: 3 }, " tern "), "project"), "superseded");
    expect(s.page).toBe(0);
    expect(explorerQuery(s)).toBe("/api/nodes?limit=50&offset=0&status=superseded&sort=name&order=asc&q=tern&type=project");
  });
  it("toggles a column's order, and a new column starts in its natural order", () => {
    const byName = toggleSort(initialExplorer, "name");
    expect([byName.sort, byName.order]).toEqual(["name", "desc"]);
    const byConf = toggleSort(byName, "confidence");
    expect([byConf.sort, byConf.order, byConf.page]).toEqual(["confidence", "desc", 0]);
    expect(toggleSort(byConf, "type").order).toBe("asc");
  });
  it("pages by 50 and clamps to the last page", () => {
    expect(PAGE_SIZE).toBe(50);
    expect(pageCount(0)).toBe(1);
    expect(pageCount(117)).toBe(3);
    expect(setPage(initialExplorer, 9, 117).page).toBe(2);
    expect(setPage(initialExplorer, -4, 117).page).toBe(0);
    expect(explorerQuery(setPage(initialExplorer, 2, 117))).toContain("offset=100");
  });
  it("moves the active row with the arrow keys, Home and End", () => {
    expect(nextRow(-1, "ArrowDown", 5)).toBe(0);
    expect(nextRow(0, "ArrowDown", 5)).toBe(1);
    expect(nextRow(4, "ArrowDown", 5)).toBe(4);
    expect(nextRow(2, "ArrowUp", 5)).toBe(1);
    expect(nextRow(0, "ArrowUp", 5)).toBe(0);
    expect(nextRow(2, "Home", 5)).toBe(0);
    expect(nextRow(2, "End", 5)).toBe(4);
    expect(nextRow(2, "x", 5)).toBe(2);
    expect(nextRow(0, "ArrowDown", 0)).toBe(-1);
  });
});
