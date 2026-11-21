// Decorative hero scene (three.js directly, lazy chunk): a slowly turning glowing network. It needs no API.
// Animated: rotation + gentle mouse parallax, paused while the tab is hidden. Static (reduced motion): one frame.

import { useEffect, useRef } from "react";
import {
  AdditiveBlending, BufferGeometry, Color, Float32BufferAttribute, Group, IcosahedronGeometry, InstancedMesh,
  LineBasicMaterial, LineSegments, MeshBasicMaterial, Object3D, PerspectiveCamera, Scene, Vector2, WebGLRenderer,
} from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
import { BACKGROUND } from "../graphData";

const COUNT = 260;
const PALETTE = ["#6366f1", "#8b5cf6", "#a78bfa", "#22d3ee", "#67e8f9", "#818cf8"];

function rng(seed: number) {
  return () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 2 ** 32;
  };
}

/** Points on a slightly flattened, jittered sphere shell plus a sparse core (a galaxy-like cluster). */
function makePoints(): Float32Array {
  const rand = rng(7);
  const pts = new Float32Array(COUNT * 3);
  for (let i = 0; i < COUNT; i++) {
    const core = i % 7 === 0;
    const r = core ? 12 + rand() * 18 : 38 + rand() * 10;
    const y = 1 - (i / (COUNT - 1)) * 2;
    const ring = Math.sqrt(1 - y * y);
    const theta = i * 2.399963 + rand() * 0.3;
    pts.set([Math.cos(theta) * ring * r, y * r * 0.82, Math.sin(theta) * ring * r], i * 3);
  }
  return pts;
}

/** Each point linked to its two nearest neighbours. */
function makeLinks(pts: Float32Array): Float32Array {
  const lines: number[] = [];
  for (let i = 0; i < COUNT; i++) {
    const near: [number, number][] = [];
    for (let j = 0; j < COUNT; j++) {
      if (i === j) continue;
      const d = (pts[i * 3] - pts[j * 3]) ** 2 + (pts[i * 3 + 1] - pts[j * 3 + 1]) ** 2 + (pts[i * 3 + 2] - pts[j * 3 + 2]) ** 2;
      near.push([d, j]);
    }
    near.sort((a, b) => a[0] - b[0]);
    for (const [, j] of near.slice(0, 2)) lines.push(...pts.slice(i * 3, i * 3 + 3), ...pts.slice(j * 3, j * 3 + 3));
  }
  return new Float32Array(lines);
}

export default function Hero3D({ animate, onFailure }: { animate: boolean; onFailure: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const failed = useRef(onFailure);
  failed.current = onFailure;

  useEffect(() => {
    const el = host.current!;
    let renderer: WebGLRenderer;
    try {
      renderer = new WebGLRenderer({ antialias: true, powerPreference: "low-power" });
    } catch {
      failed.current();
      return;
    }
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    renderer.setSize(el.clientWidth, el.clientHeight);
    el.appendChild(renderer.domElement);

    const scene = new Scene();
    scene.background = new Color(BACKGROUND);
    const camera = new PerspectiveCamera(50, el.clientWidth / Math.max(1, el.clientHeight), 1, 600);
    camera.position.set(0, 0, 135);

    const cluster = new Group();
    cluster.rotation.x = 0.35;
    scene.add(cluster);
    const points = makePoints();
    const nodeGeo = new IcosahedronGeometry(0.9, 1);
    const nodeMat = new MeshBasicMaterial({ toneMapped: false });
    const nodes = new InstancedMesh(nodeGeo, nodeMat, COUNT);
    const dummy = new Object3D();
    const rand = rng(11);
    for (let i = 0; i < COUNT; i++) {
      dummy.position.set(points[i * 3], points[i * 3 + 1], points[i * 3 + 2]);
      dummy.scale.setScalar(0.6 + rand() * 1.1);
      dummy.updateMatrix();
      nodes.setMatrixAt(i, dummy.matrix);
      nodes.setColorAt(i, new Color(PALETTE[i % PALETTE.length]));
    }
    cluster.add(nodes);
    const linkGeo = new BufferGeometry();
    linkGeo.setAttribute("position", new Float32BufferAttribute(makeLinks(points), 3));
    const linkMat = new LineBasicMaterial({ color: "#7c83ff", transparent: true, opacity: 0.22, blending: AdditiveBlending, depthWrite: false });
    cluster.add(new LineSegments(linkGeo, linkMat));

    const composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    const bloom = new UnrealBloomPass(new Vector2(el.clientWidth, el.clientHeight), 0.9, 0.6, 0.05);
    composer.addPass(bloom);
    composer.addPass(new OutputPass());

    const target = { x: 0, y: 0 };
    const onPointer = (e: PointerEvent) => {
      target.x = (e.clientX / window.innerWidth - 0.5) * 18;
      target.y = (e.clientY / window.innerHeight - 0.5) * -12;
    };
    let raf = 0;
    const frame = () => {
      cluster.rotation.y += 0.0011;
      camera.position.x += (target.x - camera.position.x) * 0.03;
      camera.position.y += (target.y - camera.position.y) * 0.03;
      camera.lookAt(0, 0, 0);
      composer.render();
      raf = requestAnimationFrame(frame);
    };
    const start = () => { if (animate && !raf && !document.hidden) raf = requestAnimationFrame(frame); };
    const stop = () => { cancelAnimationFrame(raf); raf = 0; };
    const onVisibility = () => (document.hidden ? stop() : start());

    const resize = new ResizeObserver(() => {
      const w = el.clientWidth;
      const h = Math.max(1, el.clientHeight);
      renderer.setSize(w, h);
      composer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      if (!animate) composer.render(); // static: redraw the one frame at the new size
    });
    resize.observe(el);

    if (animate) {
      window.addEventListener("pointermove", onPointer, { passive: true });
      document.addEventListener("visibilitychange", onVisibility);
      start();
    } else {
      composer.render();
    }

    return () => {
      stop();
      resize.disconnect();
      window.removeEventListener("pointermove", onPointer);
      document.removeEventListener("visibilitychange", onVisibility);
      composer.dispose();
      nodeGeo.dispose();
      nodeMat.dispose();
      linkGeo.dispose();
      linkMat.dispose();
      renderer.dispose();
      el.replaceChildren();
    };
  }, [animate]);

  return <div ref={host} className="hero-canvas" aria-hidden="true" />;
}
