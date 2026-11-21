// Privacy: what gets filtered and what was tested, generated from docs/MEMORY.md at build time
// (src/generated/privacy.json). Every number comes from that file or the live API. Text-only rendering.

import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { matchesSearch, type HandledForm, type PrivacyData } from "../privacy";

const CARDS = [
  { title: "Local only",
    text: "The dashboard API listens on localhost (the loopback address) and refuses any other address. The page loads nothing from other hosts: its content security policy allows this origin only." },
  { title: "Filtered before storage",
    text: "Before a fact is stored, a pattern filter masks secret-looking values. It is best-effort: the forms below are the ones tests verify, and the known gaps are listed honestly." },
  { title: "Read-only dashboard",
    text: "The API answers GET and HEAD only, opens the memory database read-only, and checks the Host header. Nothing here can change your memory." },
];

function FormName({ row }: { row: HandledForm }) {
  if (!row.formParts?.length) return <>{row.form}</>;
  return <>{row.formParts.map((p, i) => (p.code ? <code key={i}>{p.text}</code> : <span key={i}> {p.text} </span>))}</>;
}

function LiveCounts() {
  const [held, setHeld] = useState<number | null>(null);
  const [mode, setMode] = useState<string | null>(null);
  useEffect(() => {
    const ctrl = new AbortController();
    api.stats(ctrl.signal).then((s) => setHeld(s.nodes_active)).catch(() => setHeld(null));
    api.health(ctrl.signal).then((h) => setMode(h.mode === "demo" ? "demo data" : "your local memory")).catch(() => setMode(null));
    return () => ctrl.abort();
  }, []);
  return (
    <p className="muted live-line">
      This dashboard is showing {mode ?? "—"}, which holds {held === null ? "—" : String(held)} active memories right now.
    </p>
  );
}

export function PrivacyPage({ data }: { data: PrivacyData | null }) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());

  const sources = useMemo(() => [...new Set((data?.handledForms ?? []).map((r) => r.source))].sort(), [data]);
  const rows = useMemo(() => (data?.handledForms ?? [])
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => (source === null || row.source === source) && matchesSearch(row, query)), [data, query, source]);
  const toggle = (i: number) => setOpen((prev) => {
    const next = new Set(prev);
    if (next.has(i)) next.delete(i); else next.add(i);
    return next;
  });

  return (
    <div className="page privacy-page">
      <header className="page-head privacy-hero">
        <p className="eyebrow">Privacy</p>
        <h1>What gets filtered, and what we tested</h1>
        <p className="muted">Generated from the project's own documentation when the page is built, so it says exactly what the docs say.</p>
        {data?.generatedFrom.docsCommit && (
          <span className="badge docs-badge"><span className="badge-dot" />generated from docs {data.generatedFrom.docsCommit}</span>
        )}
      </header>

      <section className="how privacy-cards" aria-label="How your data is handled">
        {CARDS.map((c) => (
          <article key={c.title} className="how-card glass"><h2>{c.title}</h2><p>{c.text}</p></article>
        ))}
      </section>
      <LiveCounts />

      {!data ? (
        <div className="glass state-card static">
          <h2>The privacy data has not been generated</h2>
          <p className="muted">Run <code>npm run privacy</code> in the ui folder to build it from the docs.</p>
        </div>
      ) : (
        <>
          <section className="privacy-section">
            <h2 className="section-title">Handled forms <span className="muted">· {data.handledForms.length} tested forms</span></h2>
            <p className="muted">Each form lists the exact examples a test masks. The examples are fake test values, shown as plain text.</p>
            <div className="privacy-tools">
              <input type="search" placeholder="Search forms, examples or tests…" aria-label="Search handled forms"
                     value={query} onChange={(e) => setQuery(e.target.value)} />
              <div className="chips" aria-label="Filter by test file">
                <button type="button" className={source === null ? "chip on" : "chip"} aria-pressed={source === null}
                        onClick={() => setSource(null)}>All</button>
                {sources.map((s) => (
                  <button key={s} type="button" data-source={s} className={source === s ? "chip on" : "chip"}
                          aria-pressed={source === s} onClick={() => setSource(source === s ? null : s)}>{s}</button>
                ))}
              </div>
            </div>
            {rows.length === 0 && <p className="muted">No form matches.</p>}
            <ul className="forms-list">
              {rows.map(({ row, index }) => {
                const isOpen = open.has(index);
                return (
                  <li key={index} className={`form-row glass${isOpen ? " open" : ""}`}>
                    <button type="button" aria-expanded={isOpen} onClick={() => toggle(index)}>
                      <span className="form-name"><FormName row={row} /></span>
                      <span className="muted form-meta">{row.examples.length} {row.examples.length === 1 ? "example" : "examples"}</span>
                      <span className="chevron" aria-hidden="true">{isOpen ? "−" : "+"}</span>
                    </button>
                    {isOpen && (
                      <div className="form-body">
                        <ul className="examples">
                          {row.examples.map((ex, i) => <li key={i}><code className="example">{ex}</code></li>)}
                        </ul>
                        <dl className="form-facts">
                          <div><dt>Test</dt><dd className="mono">{row.test}</dd></div>
                          {row.minimumLength !== null && <div><dt>Minimum length</dt><dd>{row.minimumLength}</dd></div>}
                        </dl>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>

          <section className="privacy-section">
            <h2 className="section-title">What we do not claim <span className="muted">· known gaps</span></h2>
            <p className="muted">These are documented limits: inputs the filters are known to miss, or rules that cost recall.</p>
            <ul className="gaps-list">
              {data.knownGaps.map((g, i) => (
                <li key={i} className="gap-item glass">
                  <p>{g.text}</p>
                  {g.children.length > 0 && <ul>{g.children.map((c, j) => <li key={j}>{c}</li>)}</ul>}
                </li>
              ))}
            </ul>
          </section>

          <section className="privacy-section">
            <h2 className="section-title">What "verified" means</h2>
            <blockquote className="verified glass">{data.verifiedMeans}</blockquote>
            {data.testCounts && (
              <p className="muted">As stated in {data.generatedFrom.readme}: {data.testCounts.line}</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
