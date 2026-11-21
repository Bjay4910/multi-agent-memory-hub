// The 3D force graph (3d-force-graph + three). Imperative wrapper; React owns everything around it.
// The library's own hover labels insert HTML, so they are turned off: hover text is a React tooltip instead.

import ForceGraph3D, { type ForceGraph3DInstance } from "3d-force-graph";
import { useEffect, useRef, useState } from "react";
import { Vector2 } from "three";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
import { BACKGROUND, type GLink, type GNode, type GraphData } from "../graphData";

type FGNode = GNode & { x?: number; y?: number; z?: number };
type FGLink = Omit<GLink, "source" | "target"> & { source: number | FGNode; target: number | FGNode };

interface OrbitLike {
  autoRotate: boolean;
  autoRotateSpeed: number;
  addEventListener(type: string, fn: () => void): void;
  removeEventListener(type: string, fn: () => void): void;
}

export interface FocusRequest {
  id: number;
  nonce: number;
}

interface Props {
  data: GraphData;
  selectedId: number | null;
  bloom: boolean;
  reducedMotion: boolean;
  focus: FocusRequest | null;
  onNodeClick: (id: number) => void;
  onFailure: () => void;
}

const IDLE_MS = 6000;

// With the bloom composer on, the clear colour reaches the screen with the sRGB curve applied twice (measured:
// #05060d came out as rgb(38,42,64); with glow off it is exact). Passing the colour through the inverse curve
// once makes the composed result land on (or within a few levels of) the intended background.
function srgbToLinearHex(hex: string): string {
  const n = parseInt(hex.slice(1), 16);
  return "#" + [16, 8, 0].map((shift) => {
    const c = ((n >> shift) & 255) / 255;
    const lin = c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    return Math.round(lin * 255).toString(16).padStart(2, "0");
  }).join("");
}
const BLOOM_BACKGROUND = srgbToLinearHex(BACKGROUND);
const endId = (end: number | FGNode) => (typeof end === "object" ? end.id : end);

export function GraphCanvas({ data, selectedId, bloom, reducedMotion, focus, onNodeClick, onFailure }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const graph = useRef<ForceGraph3DInstance | null>(null);
  const bloomPass = useRef<UnrealBloomPass | null>(null);
  const hoverLinks = useRef<Set<FGLink>>(new Set());
  const selected = useRef<number | null>(selectedId);
  const motion = useRef({ reduced: reducedMotion, idleTimer: 0 as number | undefined });
  const handlers = useRef({ onNodeClick, onFailure });
  handlers.current = { onNodeClick, onFailure };
  const [hover, setHover] = useState<{ name: string; type: string } | null>(null);
  const tip = useRef<HTMLDivElement>(null);

  // create once
  useEffect(() => {
    const el = host.current!;
    let g: ForceGraph3DInstance;
    try {
      g = new ForceGraph3D(el, { controlType: "orbit" });
    } catch {
      handlers.current.onFailure();
      return;
    }
    graph.current = g;
    g.backgroundColor(BACKGROUND)
      .showNavInfo(false)
      .width(el.clientWidth)
      .height(el.clientHeight)
      .nodeLabel(() => "")
      .linkLabel(() => "")
      .nodeColor((n) => (n as FGNode).color)
      .nodeVal((n) => ((n as FGNode).id === selected.current ? (n as FGNode).val * 1.5 + 2 : (n as FGNode).val))
      .nodeOpacity(0.95)
      .nodeResolution(18)
      .linkColor(() => "#8b93ff")
      .linkOpacity(0.18)
      .linkWidth(0)
      .linkDirectionalParticles((l) => (hoverLinks.current.has(l as FGLink) ? 3 : 0))
      .linkDirectionalParticleWidth(1.4)
      .linkDirectionalParticleSpeed(0.007)
      .linkDirectionalParticleColor(() => "#67e8f9")
      .onNodeHover((n) => {
        const node = n as FGNode | null;
        el.style.cursor = node ? "pointer" : "";
        setHover(node ? { name: node.name, type: node.type } : null);
        const links = (g.graphData().links as FGLink[]).filter(
          (l) => node && (endId(l.source) === node.id || endId(l.target) === node.id));
        hoverLinks.current = new Set(links);
        g.linkDirectionalParticles(g.linkDirectionalParticles());
        pause();
      })
      .onLinkHover((l) => {
        hoverLinks.current = new Set(l ? [l as FGLink] : []);
        g.linkDirectionalParticles(g.linkDirectionalParticles());
      })
      .onNodeClick((n) => handlers.current.onNodeClick((n as FGNode).id));

    const size = new Vector2(el.clientWidth, el.clientHeight);
    const pass = new UnrealBloomPass(size, 0.6, 0.45, 0.18);
    g.postProcessingComposer().addPass(pass);
    bloomPass.current = pass;

    const controls = g.controls() as unknown as OrbitLike;
    controls.autoRotateSpeed = 0.35;
    function pause() {
      controls.autoRotate = false;
      window.clearTimeout(motion.current.idleTimer);
      motion.current.idleTimer = window.setTimeout(() => {
        controls.autoRotate = !motion.current.reduced;
      }, IDLE_MS);
    }
    controls.addEventListener("start", pause);
    el.addEventListener("pointerdown", pause);
    el.addEventListener("wheel", pause, { passive: true });
    const onMove = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      tip.current?.style.setProperty("transform", `translate(${e.clientX - r.left + 14}px, ${e.clientY - r.top + 14}px)`);
    };
    el.addEventListener("pointermove", onMove);
    controls.autoRotate = !motion.current.reduced;

    const resize = new ResizeObserver(() => {
      g.width(el.clientWidth).height(el.clientHeight);
      pass.setSize(el.clientWidth, el.clientHeight);
    });
    resize.observe(el);

    return () => {
      resize.disconnect();
      window.clearTimeout(motion.current.idleTimer);
      controls.removeEventListener("start", pause);
      el.removeEventListener("pointerdown", pause);
      el.removeEventListener("wheel", pause);
      el.removeEventListener("pointermove", onMove);
      g._destructor();
      graph.current = null;
      el.replaceChildren();
    };
  }, []);

  // data: keep the positions of nodes that stay, so a re-query does not scramble the layout
  useEffect(() => {
    const g = graph.current;
    if (!g) return;
    const old = new Map((g.graphData().nodes as FGNode[]).map((n) => [n.id, n]));
    const nodes = data.nodes.map((n) => {
      const prev = old.get(n.id);
      return prev ? { ...n, x: prev.x, y: prev.y, z: prev.z } : { ...n };
    });
    hoverLinks.current = new Set();
    g.graphData({ nodes, links: data.links.map((l) => ({ ...l })) });
  }, [data]);

  useEffect(() => {
    selected.current = selectedId;
    const g = graph.current;
    if (g) g.nodeVal(g.nodeVal());
  }, [selectedId]);

  useEffect(() => {
    if (bloomPass.current) bloomPass.current.enabled = bloom;
    graph.current?.backgroundColor(bloom ? BLOOM_BACKGROUND : BACKGROUND);
  }, [bloom]);

  useEffect(() => {
    motion.current.reduced = reducedMotion;
    const g = graph.current;
    if (g) (g.controls() as unknown as OrbitLike).autoRotate = !reducedMotion;
  }, [reducedMotion]);

  // fly the camera to a node (waits briefly for a just-added node to get a position)
  useEffect(() => {
    const g = graph.current;
    if (!g || !focus) return;
    let tries = 0;
    let timer = 0;
    const fly = () => {
      const node = (g.graphData().nodes as FGNode[]).find((n) => n.id === focus.id);
      if (!node) return;
      if (node.x === undefined && tries++ < 30) {
        timer = window.setTimeout(fly, 50);
        return;
      }
      const { x = 0, y = 0, z = 0 } = node;
      const ratio = 1 + 160 / (Math.hypot(x, y, z) || 1);
      (g.controls() as unknown as OrbitLike).autoRotate = false;
      g.cameraPosition({ x: x * ratio, y: y * ratio, z: (z || 1) * ratio }, { x, y, z }, reducedMotion ? 0 : 1200);
    };
    fly();
    return () => window.clearTimeout(timer);
  }, [focus, reducedMotion]);

  return (
    <div className="canvas-wrap">
      <div ref={host} className="canvas-host" />
      <div ref={tip} className={hover ? "tooltip visible" : "tooltip"} aria-hidden="true">
        {hover && (<><strong>{hover.name}</strong><span>{hover.type}</span></>)}
      </div>
    </div>
  );
}
