// Generates ui/src/generated/privacy.json from docs/MEMORY.md (and README.md), so the Privacy page cannot drift
// from the docs. Node only, no dependencies.
//
//   node scripts/build-privacy.mjs          write the JSON (npm run privacy; also runs before npm run build)
//   node scripts/build-privacy.mjs --check  exit 1 if the committed JSON differs from a fresh generation
//
// The docs commit is the last commit that touched docs/MEMORY.md (not HEAD), so the check stays stable across
// commits that do not change the docs. "+uncommitted" is added when docs/MEMORY.md has local changes.

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const UI = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(UI, "src", "generated", "privacy.json");

/** Split a Markdown table row into cells: "|" ends a cell unless escaped ("\|") or inside a code span. */
function cells(line) {
  const out = [];
  let cur = "";
  let tick = 0; // length of the open backtick run, 0 outside code
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === "\\" && line[i + 1] === "|") { cur += "|"; i++; continue; }
    if (ch === "`") {
      let run = 1;
      while (line[i + run] === "`") run++;
      // a run opens a code span only if the same run closes it later on the line (else it is literal, as in GFM)
      if (tick === 0 && new RegExp(`(?<!\`)\`{${run}}(?!\`)`).test(line.slice(i + run))) tick = run;
      else if (run === tick) tick = 0;
      cur += "`".repeat(run);
      i += run - 1;
      continue;
    }
    if (ch === "|" && tick === 0) { out.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out.slice(1, -1); // the row starts and ends with "|"
}

/** The code spans of a cell, in order ("`a`; ``b `c` d``" -> ["a", "b `c` d"]). */
function codeSpans(text) {
  const spans = [];
  const re = /(`+)([\s\S]*?[^`])\1(?!`)/g;
  let m;
  while ((m = re.exec(text))) spans.push(m[1].length > 1 ? m[2].trim() : m[2]);
  return spans;
}

/** A cell as text parts, code spans marked, for display ("label, `is`, value"). */
function parts(md) {
  const out = [];
  const re = /(`+)([\s\S]*?[^`])\1(?!`)/g;
  let last = 0;
  let m;
  while ((m = re.exec(md))) {
    if (m.index > last) out.push({ code: false, text: plain(md.slice(last, m.index)) });
    out.push({ code: true, text: m[1].length > 1 ? m[2].trim() : m[2] });
    last = m.index + m[0].length;
  }
  if (last < md.length) out.push({ code: false, text: plain(md.slice(last)) });
  return out.filter((p) => p.text !== "");
}

/** Markdown inline to plain text: links to their text, emphasis markers and backticks removed, escapes undone. */
function plain(md) {
  return md
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*/g, "")
    .replace(/(`+)([\s\S]*?[^`])\1(?!`)/g, (_m, t, c) => (t.length > 1 ? c.trim() : c)) // code spans only
    .replace(/\\([\\_*`|])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function section(lines, heading, stop) {
  const start = lines.indexOf(heading);
  if (start === -1) throw new Error(`heading not found: ${heading}`);
  let end = lines.findIndex((l, i) => i > start && stop(l));
  if (end === -1) end = lines.length;
  return lines.slice(start + 1, end);
}

function handledForms(lines) {
  const body = section(lines, "#### Handled forms (tested)", (l) => l.startsWith("#### "));
  const rows = body.filter((l) => l.startsWith("|"));
  const [header, , ...data] = rows;
  const names = cells(header).map((h) => h.toLowerCase());
  const col = (want) => names.findIndex((n) => n.startsWith(want));
  const [cForm, cEx, cTest, cMin] = [col("form"), col("verified example"), col("test"), col("minimum")];
  return data.map((row) => {
    const c = cells(row);
    const exCell = c[cEx] ?? "";
    const spans = codeSpans(exCell);
    const test = plain(c[cTest] ?? "");
    return {
      form: plain(c[cForm] ?? ""),
      formParts: parts(c[cForm] ?? ""),
      examples: spans.length ? spans : [plain(exCell)],
      test,
      minimumLength: cMin === -1 ? null : plain(c[cMin] ?? ""),
      source: test.split(" ")[0] || "other",
    };
  });
}

function knownGaps(lines) {
  const body = section(lines, "#### Known gaps", (l) => l.startsWith("## ") || l.startsWith("#### "));
  const items = [];
  let cur = null; // { text, children }
  let child = null; // index into cur.children, or null
  for (const raw of body) {
    if (/^- /.test(raw)) {
      cur = { text: raw.slice(2), children: [] };
      child = null;
      items.push(cur);
    } else if (/^ {2,4}- /.test(raw) && cur) {
      cur.children.push(raw.replace(/^ {2,4}- /, ""));
      child = cur.children.length - 1;
    } else if (raw.trim() && cur) {
      if (child !== null) cur.children[child] += " " + raw.trim();
      else cur.text += " " + raw.trim();
    } else if (!raw.trim()) {
      child = null;
    }
  }
  return items.map((g) => ({ text: plain(g.text), children: g.children.map(plain) }));
}

function verifiedMeans(docs) {
  const flat = docs.replace(/\s+/g, " ");
  const m = flat.match(/Verified means: .*?\)\./);
  if (!m) throw new Error('"Verified means" sentence not found');
  return m[0];
}

function testCounts(readme) {
  const m = readme.match(/The suite has ([\d,]+) tests \(counted with `pytest --collect-only`; ([\d,]+) pass and ([\d,]+) are expected failures/);
  if (!m) return null;
  const n = (s) => Number(s.replace(/,/g, ""));
  return { line: plain(m[0]) + ")", testsText: m[1], tests: n(m[1]), passed: n(m[2]), xfailed: n(m[3]) };
}

function docsCommit(root) {
  try {
    const opts = { cwd: root, stdio: ["ignore", "pipe", "ignore"], encoding: "utf8" };
    const hash = execFileSync("git", ["log", "-1", "--format=%h", "--", "docs/MEMORY.md"], opts).trim();
    if (!hash) return null;
    const dirty = execFileSync("git", ["status", "--porcelain", "--", "docs/MEMORY.md"], opts).trim();
    return dirty ? `${hash}+uncommitted` : hash;
  } catch {
    return null;
  }
}

export function generatePrivacy(root = join(UI, "..")) {
  const docs = readFileSync(join(root, "docs", "MEMORY.md"), "utf8");
  let readme = "";
  try { readme = readFileSync(join(root, "README.md"), "utf8"); } catch { readme = ""; }
  const lines = docs.split("\n");
  return {
    generatedFrom: { docs: "docs/MEMORY.md", readme: "README.md", docsCommit: docsCommit(root) },
    verifiedMeans: verifiedMeans(docs),
    handledForms: handledForms(lines),
    knownGaps: knownGaps(lines),
    testCounts: testCounts(readme),
  };
}

function main() {
  const fresh = JSON.stringify(generatePrivacy(), null, 2) + "\n";
  if (process.argv.includes("--check")) {
    let current = "";
    try { current = readFileSync(OUT, "utf8"); } catch { current = ""; }
    if (current !== fresh) {
      console.error("ui/src/generated/privacy.json is out of date with docs/MEMORY.md; run: npm run privacy");
      process.exit(1);
    }
    console.log("privacy.json matches the docs");
    return;
  }
  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, fresh);
  const data = JSON.parse(fresh);
  console.log(`wrote ${OUT}: ${data.handledForms.length} handled forms, ${data.knownGaps.length} known gaps, docs ${data.generatedFrom.docsCommit ?? "(no git)"}`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main();
