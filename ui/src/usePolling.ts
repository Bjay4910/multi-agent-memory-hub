import { useEffect, useRef } from "react";

/** Call ``fn`` now and every ``intervalMs`` while the tab is visible; stop while hidden, resume when shown. */
export function usePolling(fn: () => void, intervalMs: number): void {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    let timer: number | undefined;
    const stop = () => { window.clearInterval(timer); timer = undefined; };
    const start = () => {
      if (document.hidden || timer !== undefined) return;
      latest.current();
      timer = window.setInterval(() => latest.current(), intervalMs);
    };
    const onVisibility = () => (document.hidden ? stop() : start());
    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [intervalMs]);
}
