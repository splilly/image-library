// Typed client for the imagelib HTTP API.

export interface Summary {
  files: number;
  bytes: number;
  pending: number;
  read_errors: number;
  missing: number;
  unique_contents: number;
  corrupt: number;
  warnings: number;
  unsupported: number;
  duplicate_groups: number;
  redundant_copies: number;
  redundant_bytes: number;
  catalog_dir: string;
  unfinished_scans: { volume_id: string; label: string; subpath: string; state: string; message: string | null }[];
}

export interface VolumeStats {
  files: number;
  bytes: number;
  pending: number;
  done: number;
  read_errors: number;
  missing: number;
  pending_bytes: number;
}

export interface Volume {
  id: string;
  label: string;
  notes: string;
  read_only: boolean;
  total_bytes: number | null;
  online: boolean;
  root: string | null;
  last_seen_at: number | null;
  last_seen_path: string | null;
  fingerprints: string[];
  stats: VolumeStats;
}

export interface TargetProgress {
  volume_id: string;
  label: string;
  scan_id: number;
  state: string;
  message: string;
  discovered: number;
  files_total: number;
  files_done: number;
  bytes_total: number;
  bytes_done: number;
  read_errors: number;
  corrupt: number;
  warnings: number;
  reused: number;
  current: string;
  rate_bps: number;
  eta_s: number | null;
}

export interface ScanState {
  running: boolean;
  pausing: boolean;
  targets: TargetProgress[];
  notes: string[];
  version: number;
}

export interface FileItem {
  id: number;
  volume_id: string;
  path: string;
  size: number;
  kind: string;
  state: string;
  error: string | null;
  hash: string | null;
  status: string | null;
  problems: string | null;
  format: string | null;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  camera: string | null;
  has_preview: boolean;
  copies: number;
}

export interface Location {
  file_id: number;
  volume_id: string;
  volume_label: string;
  path: string;
  state: string;
  read_only: boolean;
  online: boolean;
}

export interface ContentBrief {
  hash: string;
  width: number | null;
  height: number | null;
  status: string;
  taken_at: string | null;
  format: string | null;
  has_preview: boolean;
  size: number;
}

export interface ContentDetail extends ContentBrief {
  kind: string;
  problems: string[];
  camera_make: string | null;
  camera_model: string | null;
  software: string | null;
  duration_s: number | null;
  metadata: Record<string, string>;
  locations: Location[];
  similar: (ContentBrief & { distance: number })[];
}

export interface WhereMatch extends ContentBrief {
  exact: boolean;
  distance: number;
  locations: Location[];
}

export interface Folder {
  name: string;
  path: string;
  files: number;
  bytes: number;
}

export interface Facets {
  cameras: { name: string; count: number }[];
  years: { name: string; count: number }[];
  kinds: { name: string; count: number }[];
}

export interface FsListing {
  path: string | null;
  parent: string | null;
  volume: { id: string; label: string; root: string } | null;
  dirs: { name: string; path: string }[];
}

export interface LibraryQuery {
  volume?: string;
  folder?: string;
  recursive?: boolean;
  status?: string;
  kind?: string;
  camera?: string;
  year?: string;
  q?: string;
  unique?: boolean;
  sort?: string;
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (method !== "GET") {
    // Required by the server on every state-changing request (cross-site protection).
    (init.headers as Record<string, string>)["X-Imagelib"] = "1";
  }
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "" && v !== false) p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  summary: () => request<Summary>("GET", "/api/summary"),
  volumes: (refresh = false) => request<Volume[]>("GET", `/api/volumes${qs({ refresh })}`),
  addVolume: (path: string, label: string, read_only: boolean) =>
    request<{ id: string }>("POST", "/api/volumes", { path, label: label || null, read_only }),
  editVolume: (id: string, changes: { label?: string; notes?: string }) =>
    request("PATCH", `/api/volumes/${id}`, changes),
  scanState: () => request<ScanState>("GET", "/api/scan"),
  startScan: (req: { volume_ids?: string[]; path?: string; all_online?: boolean; resume?: boolean }) =>
    request<ScanState>("POST", "/api/scan", req),
  pauseScan: () => request<ScanState>("POST", "/api/scan/pause"),
  files: (q: LibraryQuery, cursor?: string | null, limit = 300) =>
    request<{ items: FileItem[]; next: string | null }>(
      "GET", `/api/files${qs({ ...q, recursive: q.recursive === false ? "false" : undefined, cursor, limit })}`),
  count: (q: LibraryQuery) =>
    request<{ files: number; bytes: number }>(
      "GET", `/api/files/count${qs({ ...q, recursive: q.recursive === false ? "false" : undefined, sort: undefined })}`),
  folders: (volume: string, folder = "") => request<Folder[]>("GET", `/api/folders${qs({ volume, folder })}`),
  facets: () => request<Facets>("GET", "/api/facets"),
  content: (hash: string) => request<ContentDetail>("GET", `/api/contents/${hash}`),
  where: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ matches: WhereMatch[] }>("POST", "/api/where", form);
  },
  fs: (path?: string) => request<FsListing>("GET", `/api/fs${qs({ path })}`),
};

export const previewUrl = (hash: string, size: "s" | "m" = "s") => `/api/preview/${hash}?size=${size}`;
export const originalUrl = (fileId: number) => `/api/files/${fileId}/original`;
