// The home hero's backdrop: a lazily loaded three.js scene, or a CSS gradient when WebGL is missing or fails.

import { lazy, Suspense, useState } from "react";
import { heroMode } from "../heroMode";
import { useReducedMotion } from "../useReducedMotion";
import { hasWebGL } from "../webgl";

const Hero3D = lazy(() => import("./Hero3D"));

export function HeroBackdrop() {
  const reduced = useReducedMotion();
  const [webgl, setWebgl] = useState(() => hasWebGL());
  const mode = heroMode(webgl, reduced);
  const fallback = <div className="hero-fallback" aria-hidden="true" />;
  if (mode === "fallback") return fallback;
  return (
    <Suspense fallback={fallback}>
      <Hero3D animate={mode === "animated"} onFailure={() => setWebgl(false)} />
    </Suspense>
  );
}
