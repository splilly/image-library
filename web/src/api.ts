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

export interface RelateState {
  state: string;
  phase: string;
  done: number;
  total: number;
  pairs: number;
  message: string;
  running: boolean;
  current: boolean;
}

export interface ScanState {
  running: boolean;
  pausing: boolean;
  targets: TargetProgress[];
  notes: string[];
  version: number;
  relate?: RelateState;
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
  decision?: Decision | null;
}

export type Decision = "keep" | "discard";

export interface ProblemItem {
  file_id: number;
  volume_id: string;
  volume_label: string;
  path: string;
  size: number;
  hash: string | null;
  status: string;
  problems: string | null;
  has_preview: boolean;
  width: number | null;
  height: number | null;
  format: string | null;
  decision: Decision | null;
}

export interface IntactVersion extends ContentBrief {
  distance: number | null;
  top_difference: number | null;
  why: string[];
  locations: { file_id: number; volume_label: string; path: string }[];
}

export interface DupGroupBrief extends ContentBrief {
  copies: number;
  drives: number;
  same_drive: boolean;
  wasted: number;
  decided: number;
}

export interface DupCopy {
  file_id: number;
  volume_id: string;
  volume_label: string;
  path: string;
  online: boolean;
  read_only: boolean;
  mtime: number;
  decision: Decision | null;
}

export interface DupGroup extends ContentBrief {
  suggested_keep: number;
  suggestion_reason: string;
  copies: DupCopy[];
}

export interface FolderSide {
  volume_id: string;
  volume_label: string;
  folder: string;
  files: number;
}

export interface FolderPair {
  a: FolderSide;
  b: FolderSide;
  shared: number;
  bytes: number;
}

export interface DecisionSummary {
  keep: { files: number; bytes: number };
  discard: { files: number; bytes: number };
  by_reason: { decision: Decision; reason: string; files: number; bytes: number }[];
  stale: number;
}

export interface DecisionItem {
  file_id: number;
  volume_id: string;
  volume_label: string;
  path: string;
  size: number;
  hash: string | null;
  decision: Decision;
  reason: string;
  decided_at: number;
  status: string | null;
  has_preview: boolean;
}

export type ThumbClass = "original_ok" | "original_damaged" | "original_uncertain" | "orphan";

export interface ThumbItem {
  hash: string;
  width: number;
  height: number;
  size: number;
  has_preview: boolean;
  class: ThumbClass;
  kind: "thumbnail" | "reduced";
  reason: string | null;
  decided: boolean;
  original: ContentBrief | null;
}

export interface CopyLocation {
  file_id: number;
  volume_id: string;
  volume_label: string;
  path: string;
  online: boolean;
  decision: Decision | null;
}

export interface ThumbDetail {
  hash: string;
  width: number;
  height: number;
  size: number;
  has_preview: boolean;
  class: ThumbClass;
  kind: "thumbnail" | "reduced";
  reason: string | null;
  locations: CopyLocation[];
  originals: (ContentBrief & { certain: boolean; locations: CopyLocation[] })[];
}

export interface ClusterBrief {
  cluster: number;
  members: number;
  extra_bytes: number;
  kind: "same_pixels" | "near";
  sample: ContentBrief | null;
}

export interface ClusterMember extends ContentBrief {
  has_exif: boolean;
  camera: string | null;
  software: string | null;
  metadata_size: number;
  locations: CopyLocation[];
}

export interface Cluster {
  cluster: number;
  suggested_keep: string;
  suggestion_reason: string;
  members: ClusterMember[];
}

/** Error from the API; ``refused`` lists images a decision would have left without a copy. */
export class ApiError extends Error {
  refused?: { hash: string; status: string; copies: number; paths: string[] }[];
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
    let body: { detail?: unknown; refused?: ApiError["refused"] } = {};
    try {
      body = await res.json();
    } catch {
      /* not JSON */
    }
    const detail = body.detail ?? res.statusText;
    const err = new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.refused = body.refused;
    throw err;
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

  decide: (file_ids: number[], decision: Decision | null, reason = "manual") =>
    request<{ changed: number }>("POST", "/api/decisions", { file_ids, decision, reason }),
  decisionSummary: () => request<DecisionSummary>("GET", "/api/decisions/summary"),
  decisions: (decision?: Decision, after = 0) =>
    request<{ items: DecisionItem[]; next: number | null }>("GET", `/api/decisions${qs({ decision, after })}`),
  problems: (q: { status?: string; volume?: string; undecided?: boolean }, after = 0) =>
    request<{ items: ProblemItem[]; next: number | null }>("GET", `/api/review/problems${qs({ ...q, after })}`),
  intact: (hash: string) => request<IntactVersion[]>("GET", `/api/review/intact/${hash}`),
  dupGroups: (q: { scope?: string; volume?: string; undecided?: boolean }, offset = 0, limit = 100) =>
    request<{ items: DupGroupBrief[]; total: number; total_wasted: number }>(
      "GET", `/api/review/duplicates${qs({ ...q, offset, limit })}`),
  dupGroup: (hash: string) => request<DupGroup>("GET", `/api/review/duplicates/${hash}`),
  applySuggestions: (hashes: string[]) =>
    request<{ groups: number; discarded: number }>("POST", "/api/review/duplicates/apply-suggestions", { hashes }),
  startRelate: () => request<RelateState>("POST", "/api/relate"),
  thumbnails: (q: { cls?: string; kind?: string; undecided?: boolean }, offset = 0, limit = 100) =>
    request<{ items: ThumbItem[]; counts: Record<string, number> }>(
      "GET", `/api/review/thumbnails${qs({ ...q, offset, limit })}`),
  thumbnail: (hash: string) => request<ThumbDetail>("GET", `/api/review/thumbnails/${hash}`),
  discardVerifiedThumbnails: (kind: string) =>
    request<{ images: number; files: number }>("POST", `/api/review/thumbnails/discard-verified${qs({ kind })}`),
  similar: (undecided: boolean, offset = 0, limit = 100) =>
    request<{ items: ClusterBrief[]; total: number; total_extra: number }>(
      "GET", `/api/review/similar${qs({ undecided, offset, limit })}`),
  cluster: (id: number) => request<Cluster>("GET", `/api/review/similar/${id}`),
  keepClusterMember: (id: number, hash: string) =>
    request<{ kept: number; discarded: number }>("POST", `/api/review/similar/${id}/keep/${hash}`),
  folderPairs: (volume?: string, offset = 0) =>
    request<{ items: FolderPair[]; total: number }>("GET", `/api/review/folder-pairs${qs({ volume, offset })}`),
  discardFolderCopies: (keep: FolderSide, discard: FolderSide) =>
    request<{ discarded: number; kept: number }>("POST", "/api/review/folder-pairs/discard", {
      keep_volume: keep.volume_id, keep_folder: keep.folder,
      discard_volume: discard.volume_id, discard_folder: discard.folder,
    }),
};

export const previewUrl = (hash: string, size: "s" | "m" = "s") => `/api/preview/${hash}?size=${size}`;
export const originalUrl = (fileId: number) => `/api/files/${fileId}/original`;
