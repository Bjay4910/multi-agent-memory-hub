// Lists every http(s):// URL in the built dist and fails on any that is not on the reviewed list below.
// The reviewed ones are string constants inside React and three.js that the page never requests: XML
// namespace names (used with createElementNS), links inside library error/warning text, and a credit comment
// inside a three.js shader string. The page's CSP (default-src 'self'; connect-src 'self') blocks any external
// request regardless.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const REVIEWED = new Map([
  ["http://www.w3.org/2000/svg", "XML namespace name (createElementNS in React DOM and three.js)"],
  ["http://www.w3.org/1999/xlink", "XML namespace name (createElementNS in React DOM and three.js)"],
  ["http://www.w3.org/1998/Math/MathML", "XML namespace name (createElementNS in React DOM and three.js)"],
  ["http://www.w3.org/XML/1998/namespace", "XML namespace name (createElementNS in React DOM and three.js)"],
  ["http://www.w3.org/1999/xhtml", "XML namespace name (createElementNS in React DOM and three.js)"],
  ["http://www.w3.org/2000/xmlns/", "XML namespace name (createElementNS in React DOM and three.js)"],
  ["https://react.dev/errors/", "text of React's production error messages"],
  ["https://www.shadertoy.com/view/WtyXRy", "credit comment inside a three.js shader string"],
  ["https://github.com/mrdoob/three.js/issues/32012", "text of a three.js warning"],
  ["https://github.com/styled-components/polished/blob/main/src/internalHelpers/errors.md#", "text of a polished error message (3d-force-graph dependency)"],
]);

// The Privacy page shows the docs' exact test examples as text (src/generated/privacy.json). Their fake URLs
// (example.com and similar) are bundled as strings, never requested and never rendered as links.
const privacyText = (() => {
  try { return readFileSync(new URL("../src/generated/privacy.json", import.meta.url), "utf8"); } catch { return ""; }
})();

function files(dir) {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : [p];
  });
}

const found = new Map();
for (const file of files(new URL("../dist", import.meta.url).pathname)) {
  for (const m of readFileSync(file, "utf8").matchAll(/https?:\/\/[^"'\s)`<>\\]+/g)) {
    const url = m[0];
    if (/^https?:\/\/(127\.0\.0\.1|localhost)([:/]|$)/.test(url)) continue;
    found.set(url, [...(found.get(url) ?? []), file.split("/dist/")[1]]);
  }
}
let unknown = 0;
for (const [url, where] of [...found].sort()) {
  const reason = REVIEWED.get(url)
    ?? (privacyText.includes(url) ? "fake test example from docs/MEMORY.md, shown as text on the Privacy page" : undefined);
  if (!reason) unknown++;
  console.log(`${reason ? "reviewed" : "UNKNOWN "}  ${url}  (${where.length}x in ${[...new Set(where)].join(", ")})${reason ? ` - ${reason}` : ""}`);
}
console.log(`${found.size} distinct external URL strings, ${unknown} not reviewed`);
process.exit(unknown ? 1 : 0);
