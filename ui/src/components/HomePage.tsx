// Home: hero, live counters from the API, how it works, and the mode badge. No numbers are written in.

import { Counters } from "./Counters";
import { HeroBackdrop } from "./Hero";
import { ModeBadge, type Mode } from "./ModeBadge";

const STEPS = [
  { title: "Agents work", text: "Your AI agents answer in the room as usual. Their sessions are the only input." },
  { title: "Memory is filtered and stored locally",
    text: "Facts are extracted, secret-looking text is masked (a best-effort filter), and the result is saved on this machine." },
  { title: "You explore it", text: "Browse the graph, search it, and see which session and agent each fact came from." },
];

export function HomePage({ mode }: { mode: Mode }) {
  return (
    <div className="home">
      <section className="hero">
        <HeroBackdrop />
        <div className="hero-shade" aria-hidden="true" />
        <div className="hero-copy">
          <p className="eyebrow">Local · private · shared</p>
          <h1>One memory for all your AI agents, kept on your machine.</h1>
          <p className="hero-pitch">
            What your agents learn in each session becomes a private graph on this computer that every agent can
            recall and you can explore.
          </p>
          <div className="hero-actions">
            <a className="button" href="#/graph">Explore the memory graph</a>
            <a className="button ghost" href="#/privacy">Privacy</a>
          </div>
        </div>
      </section>

      <div className="home-body">
        <Counters />
        <section className="how" aria-label="How it works">
          {STEPS.map((s) => (
            <article key={s.title} className="how-card glass">
              <h2>{s.title}</h2>
              <p>{s.text}</p>
            </article>
          ))}
        </section>
        <footer className="home-footer">
          <ModeBadge mode={mode} />
          <span className="muted">Read-only view of the room's memory.</span>
        </footer>
      </div>
    </div>
  );
}
