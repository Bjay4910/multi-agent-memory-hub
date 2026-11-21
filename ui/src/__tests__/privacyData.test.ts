// The privacy data is generated from docs/MEMORY.md (and README.md); checked here by independent means.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { generatePrivacy } from "../../scripts/build-privacy.mjs";
import committed from "../generated/privacy.json";

const ROOT = join(import.meta.dirname, "..", "..", "..");
const docs = readFileSync(join(ROOT, "docs", "MEMORY.md"), "utf8");
const data = generatePrivacy(ROOT);

describe("generated privacy data", () => {
  it("has one row per table row of Handled forms (counted by a plain line scan)", () => {
    const lines = docs.split("\n");
    const start = lines.indexOf("#### Handled forms (tested)");
    const end = lines.indexOf("#### Known gaps");
    expect(start).toBeGreaterThan(0);
    const tableRows = lines.slice(start, end).filter((l) => l.startsWith("| ") && !l.startsWith("| form "));
    expect(data.handledForms.length).toBe(tableRows.length);
    expect(data.handledForms.length).toBeGreaterThan(40);
  });

  it("gives every row its form, test and non-empty examples", () => {
    for (const row of data.handledForms) {
      expect(row.form.trim(), JSON.stringify(row)).not.toBe("");
      expect(row.test.trim()).not.toBe("");
      expect(row.examples.length).toBeGreaterThan(0);
      for (const ex of row.examples) expect(ex.trim()).not.toBe("");
    }
  });

  it("keeps the exact examples, including escaped pipes", () => {
    const all = data.handledForms.flatMap((r) => r.examples);
    expect(all).toContain("the password is swordfish");
    expect(all).toContain("password: |\\n  spruce99");
    expect(all).toContain("my token YWJjZGU= for Trail Tracker");
  });

  it("lists the known gaps, including the stage 15 one-case single-= base64 gap", () => {
    expect(data.knownGaps.length).toBeGreaterThan(10);
    const gap = data.knownGaps.find((g) => g.text.includes("base64 in one case after a label (stage 15)"));
    expect(gap?.text).toContain("my token enp6eno= for Trail");
  });

  it("has the Verified means sentence verbatim", () => {
    expect(data.verifiedMeans.startsWith("Verified means: these exact examples")).toBe(true);
    expect(data.verifiedMeans.endsWith(".")).toBe(true);
    expect(docs.replace(/\s+/g, " ")).toContain(data.verifiedMeans);
  });

  it("parses the README test counts line only as stated there", () => {
    const readme = readFileSync(join(ROOT, "README.md"), "utf8");
    if (data.testCounts) {
      expect(readme).toContain(`The suite has ${data.testCounts.testsText} tests`);
      expect(data.testCounts.tests).toBe(Number(data.testCounts.testsText.replace(/,/g, "")));
    }
  });

  it("matches the committed privacy.json (npm run check:privacy)", () => {
    expect(committed).toEqual(JSON.parse(JSON.stringify(data)));
  });
});
