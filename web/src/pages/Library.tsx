import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { VirtuosoGrid } from "react-virtuoso";
import { api, previewUrl, type FileItem, type LibraryQuery, type Volume } from "../api";
import { ErrorNote, OnlineDot } from "../components/Bits";
import { DetailPanel } from "../components/DetailPanel";
import { Icon } from "../components/Icon";
import { bytes, count, fileName, navigate, useDebounced, useLoad } from "../util";

const STATUSES: [string, string][] = [
  ["", "Any status"],
  ["problems", "All problems"],
  ["corrupt", "Corrupt"],
  ["warning", "Suspicious"],
  ["unsupported", "Unsupported"],
  ["read_error", "Unreadable (disk error)"],
  ["duplicates", "Has duplicate copies"],
  ["ok", "OK"],
  ["pending", "Not yet scanned"],
  ["missing", "Missing (deleted or moved)"],
];
const SORTS: [string, string][] = [
  ["path", "Folder order"],
  ["date", "Oldest first"],
  ["date_desc", "Newest first"],
  ["size_desc", "Largest first"],
];
const KIND_LABEL: Record<string, string> = { image: "Images", raw: "RAW", psd: "Photoshop", video: "Videos" };
const PARAMS = ["volume", "folder", "status", "kind", "camera", "year", "month", "q", "sort", "unique", "flat"] as const;
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];

export default function Library({ params }: { params: URLSearchParams }) {
  const query: LibraryQuery = useMemo(() => ({
    volume: params.get("volume") || undefined,
    folder: params.get("folder") || undefined,
    recursive: params.get("flat") !== "1",
    status: params.get("status") || undefined,
    kind: params.get("kind") || undefined,
    camera: params.get("camera") || undefined,
    year: params.get("year") || undefined,
    month: params.get("month") || undefined,
    q: params.get("q") || undefined,
    unique: params.get("unique") === "1",
    sort: params.get("sort") || "path",
  }), [params]);
  const queryKey = JSON.stringify(query);

  const setParam = useCallback((changes: Partial<Record<(typeof PARAMS)[number], string | undefined>>) => {
    const next: Record<string, string | undefined> = {};
    for (const k of PARAMS) next[k] = params.get(k) || undefined;
    navigate("library", { ...next, ...changes });
  }, [params]);

  const vols = useLoad(() => api.volumes(), []);
  const volumes = useMemo(() => new Map((vols.data ?? []).map((v) => [v.id, v])), [vols.data]);
  const facets = useLoad(() => api.facets(query.year), [query.year]);
  const total = useLoad(() => api.count(query), [queryKey]);

  // Search box: type freely, the query follows after a pause. The box also follows the URL
  // (links, back button), and only text the user typed is ever pushed back into it.
  const [text, setText] = useState(query.q ?? "");
  const typed = useRef(false);
  const debounced = useDebounced(text, 350);
  useEffect(() => {
    if (!typed.current) setText(query.q ?? "");
  }, [query.q]);
  useEffect(() => {
    if (!typed.current) return;
    typed.current = false;
    if ((debounced || undefined) !== query.q) setParam({ q: debounced || undefined });
  }, [debounced]); // eslint-disable-line react-hooks/exhaustive-deps

  // Pages of results, appended as the grid scrolls (keyset cursor from the server).
  const [items, setItems] = useState<FileItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loading = useRef(false);
  const generation = useRef(0);

  const loadMore = useCallback((reset = false) => {
    if (loading.current && !reset) return;
    if (!reset && done) return;
    const gen = reset ? ++generation.current : generation.current;
    loading.current = true;
    api.files(query, reset ? null : cursor)
      .then((page) => {
        if (gen !== generation.current) return;
        setItems((prev) => (reset ? page.items : [...prev, ...page.items]));
        setCursor(page.next);
        setDone(!page.next);
        setError(null);
      })
      .catch((e: Error) => gen === generation.current && setError(e.message))
      .finally(() => { if (gen === generation.current) loading.current = false; });
  }, [queryKey, cursor, done]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setItems([]);
    setCursor(null);
    setDone(false);
    setSelected(null);
    loadMore(true);
  }, [queryKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const [selected, setSelected] = useState<FileItem | null>(null);
  const selectedIndex = selected ? items.findIndex((i) => i.id === selected.id) : -1;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!selected || (e.target as HTMLElement)?.tagName === "INPUT") return;
      if (e.key === "Escape") setSelected(null);
      if (e.key === "ArrowRight" && selectedIndex >= 0 && selectedIndex < items.length - 1) {
        setSelected(items[selectedIndex + 1]);
        if (selectedIndex > items.length - 20) loadMore();
      }
      if (e.key === "ArrowLeft" && selectedIndex > 0) setSelected(items[selectedIndex - 1]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, selectedIndex, items, loadMore]);

  const openHash = (hash: string) => {
    api.content(hash).then((c) => {
      const loc = c.locations.find((l) => l.online) ?? c.locations[0];
      if (!loc) return;
      setSelected({
        id: loc.file_id, volume_id: loc.volume_id, path: loc.path, size: c.size, kind: c.kind, state: loc.state,
        error: null, hash: c.hash, status: c.status, problems: c.problems.join("; "), format: c.format,
        width: c.width, height: c.height, taken_at: c.taken_at, camera: c.camera_model,
        has_preview: c.has_preview, copies: c.locations.length,
      });
    }).catch((e: Error) => setError(e.message));
  };

  const currentVolume = query.volume ? volumes.get(query.volume) : undefined;

  return (
    <div className={`library ${selected ? "with-detail" : ""}`}>
      <aside className="sidebar">
        <button className={`side-item ${!query.volume ? "active" : ""}`}
          onClick={() => setParam({ volume: undefined, folder: undefined })}>
          <Icon name="grid" size={16} /> All drives
        </button>
        {vols.data?.map((v) => (
          <div key={v.id}>
            <button className={`side-item ${query.volume === v.id && !query.folder ? "active" : ""}`}
              onClick={() => setParam({ volume: v.id, folder: undefined })}>
              <OnlineDot online={v.online} /> <span className="grow">{v.label}</span>
              <span className="muted small">{count(v.stats.files)}</span>
            </button>
            {query.volume === v.id && (
              <FolderTree volume={v} folder="" selected={query.folder ?? ""} depth={1}
                onSelect={(f) => setParam({ folder: f || undefined })} />
            )}
          </div>
        ))}
        {vols.data?.length === 0 && <p className="muted small">No drives yet. <a href="#/drives">Add one</a>.</p>}
      </aside>

      <section className="results">
        <div className="filters">
          <div className="search">
            <Icon name="search" size={16} />
            <input placeholder="Search file and folder names" value={text}
              onChange={(e) => { typed.current = true; setText(e.target.value); }} />
          </div>
          <select value={query.status ?? ""} onChange={(e) => setParam({ status: e.target.value || undefined })}>
            {STATUSES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <select value={query.kind ?? ""} onChange={(e) => setParam({ kind: e.target.value || undefined })}>
            <option value="">All types</option>
            {facets.data?.kinds.map((k) => (
              <option key={k.name} value={k.name}>{KIND_LABEL[k.name] ?? k.name} ({count(k.count)})</option>
            ))}
          </select>
          <select value={query.camera ?? ""} onChange={(e) => setParam({ camera: e.target.value || undefined })}>
            <option value="">Any camera</option>
            {facets.data?.cameras.map((c) => <option key={c.name} value={c.name}>{c.name} ({count(c.count)})</option>)}
          </select>
          <select value={query.year ?? ""} aria-label="Year"
            onChange={(e) => setParam({ year: e.target.value || undefined,
                                        month: e.target.value === "unknown" ? undefined : query.month })}>
            <option value="">Any year</option>
            {facets.data?.years.map((y) => (
              <option key={y.name} value={y.name}>{y.name === "unknown" ? "Unknown" : y.name} ({count(y.count)})</option>
            ))}
          </select>
          <select value={query.year === "unknown" ? "" : query.month ?? ""} disabled={query.year === "unknown"}
            onChange={(e) => setParam({ month: e.target.value || undefined })}
            aria-label="Month" title={query.year ? `Months of ${query.year}` : "This month in any year"}>
            <option value="">Any month</option>
            {MONTHS.map((name, i) => {
              const value = String(i + 1).padStart(2, "0");
              const n = facets.data?.months.find((m) => m.name === value)?.count;
              return <option key={value} value={value} disabled={facets.data ? !n : false}>{name}{n ? ` (${count(n)})` : ""}</option>;
            })}
          </select>
          <select value={query.sort} onChange={(e) => setParam({ sort: e.target.value === "path" ? undefined : e.target.value })}>
            {SORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <label className="check" title="Show each image once, however many copies exist">
            <input type="checkbox" checked={!!query.unique} onChange={(e) => setParam({ unique: e.target.checked ? "1" : undefined })} />
            One per image
          </label>
          {query.folder && (
            <label className="check">
              <input type="checkbox" checked={query.recursive !== false}
                onChange={(e) => setParam({ flat: e.target.checked ? undefined : "1" })} />
              Include sub-folders
            </label>
          )}
        </div>
        <div className="results-head">
          <span>
            {currentVolume ? currentVolume.label : "All drives"}
            {query.folder ? <> / <code>{query.folder}</code></> : null}
          </span>
          <span className="muted">
            {total.data ? `${count(total.data.files)} files · ${bytes(total.data.bytes)}` : "…"}
          </span>
        </div>
        <ErrorNote error={error} />
        {items.length === 0 && done ? (
          <div className="empty">Nothing matches these filters.</div>
        ) : (
          <VirtuosoGrid
            className="grid"
            listClassName="grid-list"
            itemClassName="grid-item"
            data={items}
            endReached={() => loadMore()}
            overscan={600}
            computeItemKey={(_, item) => item.id}
            itemContent={(_, item) => (
              <Cell item={item} volume={volumes.get(item.volume_id)} selected={selected?.id === item.id}
                onClick={() => setSelected(item)} />
            )}
          />
        )}
      </section>

      {selected && (
        <DetailPanel file={selected} volumes={volumes} onClose={() => setSelected(null)} onOpenHash={openHash} />
      )}
    </div>
  );
}

function Cell({ item, volume, selected, onClick }: {
  item: FileItem; volume?: Volume; selected: boolean; onClick: () => void;
}) {
  const status = item.state === "done" ? item.status : item.state;
  return (
    <button className={`cell ${selected ? "selected" : ""} ${volume && !volume.online ? "cell-offline" : ""}`}
      onClick={onClick} title={`${volume?.label ?? ""}: ${item.path}`}>
      <div className="thumb">
        {item.hash && item.has_preview ? (
          <img src={previewUrl(item.hash)} alt="" loading="lazy" decoding="async" />
        ) : (
          <span className="thumb-placeholder">{item.format ?? item.kind}</span>
        )}
        {status && status !== "ok" && <span className={`corner corner-${status}`} title={status} />}
        {item.copies > 1 && <span className="copies" title={`${item.copies} copies`}>×{item.copies}</span>}
        {item.kind === "video" && <span className="kind-tag">▶</span>}
        {item.kind === "raw" && <span className="kind-tag">RAW</span>}
      </div>
      <div className="caption">{fileName(item.path)}</div>
    </button>
  );
}

function FolderTree({ volume, folder, selected, depth, onSelect }: {
  volume: Volume; folder: string; selected: string; depth: number; onSelect: (f: string) => void;
}) {
  const { data } = useLoad(() => api.folders(volume.id, folder), [volume.id, folder]);
  return (
    <ul className="tree">
      {data?.map((f) => (
        <TreeNode key={f.path} volume={volume} name={f.name} path={f.path} files={f.files} selected={selected}
          depth={depth} onSelect={onSelect} />
      ))}
    </ul>
  );
}

function TreeNode({ volume, name, path, files, selected, depth, onSelect }: {
  volume: Volume; name: string; path: string; files: number; selected: string; depth: number;
  onSelect: (f: string) => void;
}) {
  const onPath = selected === path || selected.startsWith(path + "/");
  const [open, setOpen] = useState(onPath);
  useEffect(() => { if (onPath) setOpen(true); }, [onPath]);
  return (
    <li>
      <div className={`tree-row ${selected === path ? "active" : ""}`} style={{ paddingLeft: depth * 12 }}>
        <button className={`twisty ${open ? "open" : ""}`} onClick={() => setOpen(!open)} aria-label="Expand">
          <Icon name="chevron" size={12} />
        </button>
        <button className="tree-label" onClick={() => onSelect(path)}>
          <span className="grow">{name}</span>
          <span className="muted small">{count(files)}</span>
        </button>
      </div>
      {open && <FolderTree volume={volume} folder={path} selected={selected} depth={depth + 1} onSelect={onSelect} />}
    </li>
  );
}
