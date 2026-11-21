// App shell: top navigation, mode badge from /api/health, and hash routes (no router dependency).

import { lazy, Suspense, useEffect, useState } from "react";
import { api } from "./api";
import { Placeholder } from "./components/Placeholder";

// three.js is large: load it only when the graph page is opened
const GraphPage = lazy(() => import("./components/GraphPage").then((m) => ({ default: m.GraphPage })));

export const ROUTES = [
  { path: "home", label: "Home" },
  { path: "graph", label: "Memory Graph" },
  { path: "room", label: "The Room" },
  { path: "timeline", label: "Timeline" },
  { path: "explorer", label: "Explorer" },
  { path: "privacy", label: "Privacy" },
] as const;
type Route = (typeof ROUTES)[number]["path"];

function currentRoute(): Route {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return (ROUTES.find((r) => r.path === hash)?.path ?? "graph") as Route;
}

type Mode = { kind: "checking" } | { kind: "demo" } | { kind: "local" } | { kind: "down"; message: string };

function ModeBadge({ mode }: { mode: Mode }) {
  const text = { checking: "Checking…", demo: "Demo data", local: "Local memory", down: "Memory unavailable" }[mode.kind];
  return (
    <span className={`badge badge-${mode.kind}`} title={mode.kind === "down" ? mode.message : undefined}>
      <span className="badge-dot" />{text}
    </span>
  );
}

export function App() {
  const [route, setRoute] = useState<Route>(currentRoute);
  const [mode, setMode] = useState<Mode>({ kind: "checking" });

  useEffect(() => {
    const onHash = () => setRoute(currentRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    let alive = true;
    const check = () => api.health()
      .then((h) => alive && setMode({ kind: h.mode === "demo" ? "demo" : "local" }))
      .catch((err: Error) => alive && setMode({ kind: "down", message: err.message }));
    void check();
    const timer = window.setInterval(check, 30_000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);

  const label = ROUTES.find((r) => r.path === route)!.label;
  return (
    <div className="app">
      <div className="backdrop" aria-hidden="true" />
      <header className="topbar">
        <a className="brand" href="#/graph"><span className="brand-mark" aria-hidden="true" />Agent Room</a>
        <nav aria-label="Main">
          {ROUTES.map((r) => (
            <a key={r.path} href={`#/${r.path}`} className={r.path === route ? "nav-link active" : "nav-link"}
               aria-current={r.path === route ? "page" : undefined}>{r.label}</a>
          ))}
        </nav>
        <ModeBadge mode={mode} />
      </header>
      <main className="main">
        {route === "graph" ? (
          <Suspense fallback={<div className="center-state"><div className="spinner" />Loading the graph…</div>}>
            <GraphPage />
          </Suspense>
        ) : <Placeholder title={label} />}
      </main>
    </div>
  );
}
