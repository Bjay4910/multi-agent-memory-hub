// App shell: top navigation, mode badge from /api/health, and hash routes (no router dependency).

import { lazy, Suspense, useEffect, useState } from "react";
import { api } from "./api";
import { HomePage } from "./components/HomePage";
import { ModeBadge, type Mode } from "./components/ModeBadge";
import { ExplorerPage } from "./components/ExplorerPage";
import { Placeholder } from "./components/Placeholder";
import { PrivacyPage } from "./components/PrivacyPage";
import privacyJson from "./generated/privacy.json";
import type { PrivacyData } from "./privacy";
import { RoomPage } from "./components/RoomPage";
import { TimelinePage } from "./components/TimelinePage";
import { nodeIdFromParams, parseHash, type Location } from "./route";

// three.js is large: load it only when the graph page is opened
const GraphPage = lazy(() => import("./components/GraphPage").then((m) => ({ default: m.GraphPage })));

const PRIVACY = privacyJson as PrivacyData;

export const ROUTES = [
  { path: "", label: "Home" },
  { path: "graph", label: "Memory Graph" },
  { path: "room", label: "The Room" },
  { path: "timeline", label: "Timeline" },
  { path: "explorer", label: "Explorer" },
  { path: "privacy", label: "Privacy" },
] as const;
type Route = (typeof ROUTES)[number]["path"];

function currentLocation(): Location & { route: Route } {
  const loc = parseHash(window.location.hash);
  const name = loc.route === "home" ? "" : loc.route;
  return { ...loc, route: (ROUTES.find((r) => r.path === name)?.path ?? "") as Route };
}

export function App() {
  const [loc, setLoc] = useState(currentLocation);
  const route = loc.route;
  const [mode, setMode] = useState<Mode>({ kind: "checking" });

  useEffect(() => {
    const onHash = () => setLoc(currentLocation());
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
    <div className={mode.kind === "demo" ? "app with-banner" : "app"}>
      <div className="backdrop" aria-hidden="true" />
      <header className="topbar">
        <a className="brand" href="#/"><span className="brand-mark" aria-hidden="true" />Agent Room</a>
        <nav aria-label="Main">
          {ROUTES.map((r) => (
            <a key={r.path} href={`#/${r.path}`} className={r.path === route ? "nav-link active" : "nav-link"}
               aria-current={r.path === route ? "page" : undefined}>{r.label}</a>
          ))}
        </nav>
        {route === "privacy" && PRIVACY.generatedFrom.docsCommit && (
          <span className="badge docs-badge" title="The Privacy page is generated from docs/MEMORY.md at this commit">
            generated from docs {PRIVACY.generatedFrom.docsCommit}
          </span>
        )}
        <ModeBadge mode={mode} />
      </header>
      {mode.kind === "demo" && (
        <p className="demo-banner" role="note">
          <span className="demo-banner-dot" aria-hidden="true" />
          Fictional sample data. Every name, project and number here is invented for this demo. Run{" "}
          <code>room dashboard-api</code> without <code>--demo</code> to see your own memory.
        </p>
      )}
      <main className="main">
        {route === "" ? <HomePage mode={mode} /> : route === "graph" ? (
          <Suspense fallback={<div className="center-state"><div className="spinner" />Loading the graph…</div>}>
            <GraphPage initialNodeId={nodeIdFromParams(loc.params)} />
          </Suspense>
        ) : route === "room" ? <RoomPage />
          : route === "timeline" ? <TimelinePage anchor={loc.anchor} />
          : route === "explorer" ? <ExplorerPage />
          : route === "privacy" ? <PrivacyPage data={PRIVACY} />
          : <Placeholder title={label} />}
      </main>
    </div>
  );
}
