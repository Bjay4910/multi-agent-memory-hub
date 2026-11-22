import { useEffect, useRef, useState } from "react";

const easeOut = (t: number) => 1 - (1 - t) ** 3;

/** ``target`` counted up from 0 over ``durationMs`` (ease-out), or shown at once when ``animate`` is false.
 * null stays null (not loaded). The last frame is exactly ``target``; unmounting cancels the animation. */
export function useCountUp(target: number | null, animate: boolean, durationMs = 800): number | null {
  const [shown, setShown] = useState<number | null>(animate ? null : target);
  const frame = useRef(0);
  useEffect(() => {
    if (target === null || !animate) {
      setShown(target);
      return;
    }
    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      setShown(t >= 1 ? target : Math.round(easeOut(t) * target));
      if (t < 1) frame.current = requestAnimationFrame(step);
    };
    frame.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame.current);
  }, [target, animate, durationMs]);
  return shown;
}
