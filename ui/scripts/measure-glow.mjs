// Measures node glow on graph screenshots and compares it with the F4 screenshots (npm run check:glow).
// Node pixels: inside the canvas, outside the toolbar and legend overlays, with a channel above 90.
// Fails if a glow screenshot has pure-white node pixels (no node type is white; documents are light grey
// #cbd5e1) or if mean node brightness is not above the F4 value for the same view.
import { readFileSync } from "node:fs";
import { inflateSync } from "node:zlib";

function decode(path) {
  const buf = readFileSync(path);
  let pos = 8, width = 0, height = 0, type = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos);
    const kind = buf.toString("latin1", pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (kind === "IHDR") { width = data.readUInt32BE(0); height = data.readUInt32BE(4); type = data[9]; }
    if (kind === "IDAT") idat.push(data);
    pos += 12 + len;
  }
  const bpp = type === 6 ? 4 : 3;
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * bpp;
  const px = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y++) {
    const f = raw[y * (stride + 1)];
    for (let x = 0; x < stride; x++) {
      const v = raw[y * (stride + 1) + 1 + x];
      const a = x >= bpp ? px[y * stride + x - bpp] : 0;
      const b = y > 0 ? px[(y - 1) * stride + x] : 0;
      const c = x >= bpp && y > 0 ? px[(y - 1) * stride + x - bpp] : 0;
      const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
      const pred = [0, a, b, (a + b) >> 1, pa <= pb && pa <= pc ? a : pb <= pc ? b : c][f];
      px[y * stride + x] = (v + pred) & 255;
    }
  }
  return { width, height, bpp, px };
}

export function measure(path) {
  const { width, height, bpp, px } = decode(path);
  let n = 0, sum = 0, white = 0;
  for (let y = 66; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if ((x < 1000 && y < 225) || (x < 215 && y > 585) || x > width - 30) continue; // overlays, scrollbar
      const i = y * width * bpp + x * bpp;
      const r = px[i], g = px[i + 1], b = px[i + 2];
      if (Math.max(r, g, b) <= 90) continue;
      n++;
      sum += (r + g + b) / 3;
      if (r >= 250 && g >= 250 && b >= 250) white++;
    }
  }
  return { nodePixels: n, meanBrightness: n ? sum / n : 0, pureWhitePixels: white };
}

const dir = new URL("../screenshots/", import.meta.url).pathname;
const views = [["default", "graph-glow-default.png", "graph-polish-default.png"],
               ["zoomed", "graph-glow-zoomed.png", "graph-polish-zoomed.png"]];
let ok = true;
for (const [view, now, before] of views) {
  const a = measure(dir + now), b = measure(dir + before);
  const brighter = a.meanBrightness > b.meanBrightness;
  ok &&= brighter && a.pureWhitePixels === 0;
  console.log(`${view}: F5 mean ${a.meanBrightness.toFixed(1)} over ${a.nodePixels} px, ${a.pureWhitePixels} pure white | ` +
              `F4 mean ${b.meanBrightness.toFixed(1)} over ${b.nodePixels} px, ${b.pureWhitePixels} pure white | ` +
              `${brighter ? "brighter" : "NOT brighter"}`);
}
process.exit(ok ? 0 : 1);
