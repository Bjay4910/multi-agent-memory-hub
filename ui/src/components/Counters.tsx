// Live counters, from the API only. A number that could not be read is shown as "—", never guessed.

import { useEffect, useState } from "react";
import { api } from "../api";
import { useCountUp } from "../useCountUp";
import { useReducedMotion } from "../useReducedMotion";

interface Values {
  memories: number | null;
  connections: number | null;
  agents: number | null;
  sessions: number | null;
}

const EMPTY: Values = { memories: null, connections: null, agents: null, sessions: null };
const LABELS: [keyof Values, string][] = [
  ["memories", "Memories"], ["connections", "Connections"], ["agents", "Agents working now"], ["sessions", "Sessions"],
];

function CounterValue({ value, animate }: { value: number | null; animate: boolean }) {
  const shown = useCountUp(value, animate);
  return <span className="counter-value">{shown === null ? "—" : String(shown)}</span>;
}

/** ``animate`` (opt-in; Home uses it): count up to each number on arrival, unless reduced motion is set. */
export function Counters({ animate = false }: { animate?: boolean }) {
  const reduced = useReducedMotion();
  const [values, setValues] = useState<Values>(EMPTY);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    const load = () => Promise.allSettled([api.stats(ctrl.signal), api.presence(ctrl.signal)]).then(([stats, presence]) => {
      if (ctrl.signal.aborted) return;
      setValues({
        memories: stats.status === "fulfilled" ? stats.value.nodes_active : null,
        connections: stats.status === "fulfilled" ? stats.value.edges : null,
        sessions: stats.status === "fulfilled" ? stats.value.sessions : null,
        agents: presence.status === "fulfilled"
          ? presence.value.agents.filter((a) => a.effective_status === "working").length : null,
      });
      setLoaded(true);
    });
    void load();
    const timer = window.setInterval(load, 30_000);
    return () => { ctrl.abort(); window.clearInterval(timer); };
  }, []);

  return (
    <section className="counters glass" aria-label="Live counts from this machine's memory" aria-busy={!loaded}>
      {LABELS.map(([key, label]) => (
        <div key={key} className="counter" data-counter={key}>
          <CounterValue value={values[key]} animate={animate && !reduced} />
          <span className="counter-label">{label}</span>
        </div>
      ))}
    </section>
  );
}
