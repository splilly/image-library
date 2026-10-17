import { useCallback, useEffect, useRef, useState } from "react";
import type { ScanState } from "./api";

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  let v = n;
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return i === 0 ? `${v} B` : `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

export function count(n: number | null | undefined): string {
  return n === null || n === undefined ? "–" : n.toLocaleString();
}

export function duration(s: number | null | undefined): string {
  if (s === null || s === undefined || !isFinite(s)) return "–";
  s = Math.round(s);
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

export function date(taken: string | null | undefined): string {
  if (!taken) return "";
  return taken.replace("T", " ").slice(0, 16);
}

export function ago(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "never";
  const s = Date.now() / 1000 - epochSeconds;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString();
}

export function fileName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/** Join a drive root (OS path) with a catalog path ('/' separated) for display and copying. */
export function osPath(root: string, rel: string): string {
  const windows = /^[A-Za-z]:\\/.test(root);
  const sep = windows ? "\\" : "/";
  return root.replace(/[\\/]+$/, "") + sep + (windows ? rel.replace(/\//g, "\\") : rel);
}

/** Fetch on mount and whenever deps change; returns data, error, loading and a reload function. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const reload = useCallback(() => {
    const mine = ++seq.current;
    setLoading(true);
    load()
      .then((d) => mine === seq.current && (setData(d), setError(null)))
      .catch((e: Error) => mine === seq.current && setError(e.message))
      .finally(() => mine === seq.current && setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(reload, [reload]);
  return { data, error, loading, reload, setData };
}

/**
 * Live scan state. Pushed by the server (Server-Sent Events); fetched once up front so
 * the page is right immediately, and polled while the push channel is down, so progress
 * can never silently freeze.
 */
export function useScanState(): ScanState | null {
  const [state, setState] = useState<ScanState | null>(null);
  useEffect(() => {
    let closed = false;
    let healthy = false;
    const fetchOnce = () =>
      fetch("/api/scan").then((r) => r.json()).then((s) => !closed && setState(s)).catch(() => {});
    fetchOnce();
    const es = new EventSource("/api/events");
    es.addEventListener("scan", (e) => {
      healthy = true;
      setState(JSON.parse((e as MessageEvent).data));
    });
    es.onerror = () => { healthy = false; };
    const poll = setInterval(() => { if (!healthy) fetchOnce(); }, 2000);
    return () => { closed = true; es.close(); clearInterval(poll); };
  }, []);
  return state;
}

/** Re-run ``fn`` every ``ms`` milliseconds while ``active``. */
export function useInterval(fn: () => void, ms: number, active = true) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => saved.current(), ms);
    return () => clearInterval(id);
  }, [ms, active]);
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

/** Minimal hash router: "#/library?volume=..." -> { page: "library", params }. */
export function useRoute(): { page: string; params: URLSearchParams } {
  const parse = () => {
    const h = window.location.hash.replace(/^#\/?/, "");
    const [page, query] = h.split("?");
    return { page: page || "dashboard", params: new URLSearchParams(query || "") };
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function navigate(page: string, params: Record<string, string | undefined> = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) p.set(k, v);
  const q = p.toString();
  window.location.hash = `#/${page}${q ? "?" + q : ""}`;
}

export const STATUS_LABEL: Record<string, string> = {
  ok: "OK",
  warning: "Suspicious",
  corrupt: "Corrupt",
  unsupported: "Unsupported",
  read_error: "Read error",
  pending: "Not yet scanned",
  missing: "Missing",
};
