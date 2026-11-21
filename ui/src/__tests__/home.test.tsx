// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HomePage } from "../components/HomePage";
import { heroMode } from "../heroMode";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
beforeEach(() => {
  // jsdom has no WebGL: getContext returns null, as a browser without WebGL would
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
});
afterEach(() => { container?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("heroMode", () => {
  it("is a CSS fallback without WebGL, a static frame with reduced motion, otherwise animated", () => {
    expect(heroMode(false, false)).toBe("fallback");
    expect(heroMode(false, true)).toBe("fallback");
    expect(heroMode(true, true)).toBe("static");
    expect(heroMode(true, false)).toBe("animated");
  });
});

describe("HomePage", () => {
  it("renders without WebGL: the gradient fallback, the pitch, both buttons and no crash", async () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    await act(async () => { createRoot(container).render(<HomePage mode={{ kind: "demo" }} />); });
    expect(container.querySelector(".hero-fallback")).not.toBeNull();
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("h1")?.textContent).toBeTruthy();
    const links = [...container.querySelectorAll<HTMLAnchorElement>(".hero-actions a")].map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([["Explore the memory graph", "#/graph"], ["Privacy", "#/privacy"]]);
    expect(container.querySelectorAll(".how-card")).toHaveLength(3);
    expect(container.querySelector("footer .badge")?.textContent).toContain("Demo data");
  });

  it("says nothing numeric that did not come from the API", async () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    await act(async () => { createRoot(container).render(<HomePage mode={{ kind: "down", message: "x" }} />); });
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    const text = container.textContent ?? "";
    expect(text.replace(/[0-9]/g, "")).toBe(text); // no digits anywhere when the API is down
  });
});
