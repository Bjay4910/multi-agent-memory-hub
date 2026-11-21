// Hash routes: "#/<route>?<query>#<anchor>", e.g. "#/graph?node=42" and "#/timeline#<session_id>".

export interface Location {
  route: string;
  params: URLSearchParams;
  anchor: string | null;
}

export function parseHash(hash: string): Location {
  const body = hash.replace(/^#\/?/, "");
  const cut = body.indexOf("#");
  const head = cut === -1 ? body : body.slice(0, cut);
  const rawAnchor = cut === -1 ? "" : body.slice(cut + 1);
  const q = head.indexOf("?");
  let anchor: string | null = null;
  if (rawAnchor) {
    try { anchor = decodeURIComponent(rawAnchor); } catch { anchor = rawAnchor; }
  }
  return {
    route: q === -1 ? head : head.slice(0, q),
    params: new URLSearchParams(q === -1 ? "" : head.slice(q + 1)),
    anchor,
  };
}

/** The ?node= id, if it is a positive integer (same rule as the API). */
export function nodeIdFromParams(params: URLSearchParams): number | null {
  const raw = params.get("node") ?? "";
  if (!/^[0-9]{1,15}$/.test(raw)) return null;
  const id = Number(raw);
  return id >= 1 ? id : null;
}
