export function Placeholder({ title }: { title: string }) {
  return (
    <section className="placeholder">
      <div className="glass placeholder-card">
        <p className="eyebrow">Coming next</p>
        <h1>{title}</h1>
        <p className="muted">This view is not built yet. The Memory Graph is ready to explore.</p>
        <a className="button" href="#/graph">Open the Memory Graph</a>
      </div>
    </section>
  );
}
