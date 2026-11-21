export type Mode = { kind: "checking" } | { kind: "demo" } | { kind: "local" } | { kind: "down"; message: string };

const TEXT = { checking: "Checking…", demo: "Demo data", local: "Local memory", down: "Memory unavailable" } as const;

export function ModeBadge({ mode }: { mode: Mode }) {
  return (
    <span className={`badge badge-${mode.kind}`} title={mode.kind === "down" ? mode.message : undefined}>
      <span className="badge-dot" />{TEXT[mode.kind]}
    </span>
  );
}
