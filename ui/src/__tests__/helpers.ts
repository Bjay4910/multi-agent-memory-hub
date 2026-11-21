import { vi } from "vitest";

export function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

/** fetch stub routing by path prefix; anything unmatched is a network error. */
export function stubApi(routes: Record<string, unknown | ((url: string) => unknown)>) {
  const fetchMock = vi.fn((url: string) => {
    const path = String(url);
    const key = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => path.startsWith(k));
    if (!key) return Promise.reject(new TypeError("Failed to fetch"));
    const value = routes[key];
    return jsonResponse(typeof value === "function" ? (value as (u: string) => unknown)(path) : value);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export const settle = () => new Promise((r) => setTimeout(r, 30));
