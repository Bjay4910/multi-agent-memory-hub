// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "../usePolling";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let hidden = false;
beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

function Probe({ fn }: { fn: () => void }) {
  usePolling(fn, 5000);
  return null;
}

describe("usePolling", () => {
  it("polls every 5 s while visible, stops when hidden, and resumes (with an immediate call) when visible", () => {
    const fn = vi.fn();
    const el = document.createElement("div");
    const root = createRoot(el);
    act(() => root.render(<Probe fn={fn} />));
    expect(fn).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(15_000));
    expect(fn).toHaveBeenCalledTimes(4);

    hidden = true;
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    act(() => vi.advanceTimersByTime(30_000));
    expect(fn).toHaveBeenCalledTimes(4);

    hidden = false;
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(fn).toHaveBeenCalledTimes(5);
    act(() => vi.advanceTimersByTime(5_000));
    expect(fn).toHaveBeenCalledTimes(6);

    act(() => root.unmount());
    act(() => vi.advanceTimersByTime(20_000));
    expect(fn).toHaveBeenCalledTimes(6);
  });

  it("does not start while the tab is hidden", () => {
    hidden = true;
    const fn = vi.fn();
    const root = createRoot(document.createElement("div"));
    act(() => root.render(<Probe fn={fn} />));
    act(() => vi.advanceTimersByTime(20_000));
    expect(fn).not.toHaveBeenCalled();
    act(() => root.unmount());
  });
});
