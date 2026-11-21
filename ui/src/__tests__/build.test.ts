// The hero's three.js scene is its own lazy chunk: built for real and checked in Vite's manifest.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

interface Chunk { file: string; src?: string; isEntry?: boolean; isDynamicEntry?: boolean; imports?: string[]; dynamicImports?: string[] }

describe("build", () => {
  it("puts the hero scene and the graph page in lazy chunks, not in the main chunk", () => {
    const out = mkdtempSync(join(tmpdir(), "ui-build-"));
    execFileSync(process.execPath, ["node_modules/vite/bin/vite.js", "build", "--outDir", out, "--emptyOutDir", "--logLevel", "error"],
                 { cwd: join(import.meta.dirname, "..", ".."), stdio: "pipe" });
    const manifest: Record<string, Chunk> = JSON.parse(readFileSync(join(out, ".vite", "manifest.json"), "utf8"));
    const entry = Object.entries(manifest).find(([, c]) => c.isEntry)!;
    const staticClosure = new Set<string>();
    const walk = (key: string) => {
      if (staticClosure.has(key)) return;
      staticClosure.add(key);
      for (const dep of manifest[key]?.imports ?? []) walk(dep);
    };
    walk(entry[0]);
    for (const lazy of ["src/components/Hero3D.tsx", "src/components/GraphPage.tsx"]) {
      expect(manifest[lazy]?.isDynamicEntry, lazy).toBe(true);
      expect(staticClosure.has(lazy), lazy).toBe(false);
    }
    const mainJs = readFileSync(join(out, entry[1].file), "utf8");
    expect(mainJs).not.toContain("WebGLRenderer");
  }, 120_000);
});
