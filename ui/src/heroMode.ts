export type HeroMode = "fallback" | "static" | "animated";

/** The home hero: a CSS gradient without WebGL, one still frame with reduced motion, otherwise animated. */
export function heroMode(webgl: boolean, reducedMotion: boolean): HeroMode {
  if (!webgl) return "fallback";
  return reducedMotion ? "static" : "animated";
}
