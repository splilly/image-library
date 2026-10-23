import { useEffect, useState } from "react";
import { api, previewUrl, type ExportFile, type ExportFolder, type ExportMethod, type ExportSummary } from "../api";
import { bytes, count, plural } from "../util";

const METHOD: Record<ExportMethod, { label: string; hint: string }> = {
  copy: { label: "Copied", hint: "copied into the library; the original stays" },
  rename: { label: "Moved", hint: "already on the library's drive: moved into place, instantly" },
  transfer: { label: "Transferred", hint: "copied from another drive and checked; the original goes to quarantine there" },
  wait: { label: "Waiting", hint: "its only copies are on read-only drives: left where it is" },
};

/** What a prepared library will do, and its proposed folders to browse before anything moves. */
export function Totals({ s }: { s: ExportSummary }) {
  const total = (m: ExportMethod) => s.drives.reduce((n, d) => n + (d.methods[m]?.files ?? 0), 0);
  const extras = s.drives.reduce((n, d) => n + (d.extras ?? 0), 0);
  const short = s.free_bytes !== null && s.needed_bytes > s.free_bytes;
  return (
    <div className="organize-totals">
      <div className="tiles">
        {(["rename", "transfer", "copy", "wait"] as ExportMethod[]).filter((m) => total(m) > 0).map((m) => (
          <div key={m} className="tile" title={METHOD[m].hint}>
            <div className="tile-value">{count(total(m))}</div>
            <div className="tile-label">{METHOD[m].label}</div>
            <div className="tile-sub muted small">{METHOD[m].hint}</div>
          </div>
        ))}
        {extras > 0 && (
          <div className="tile" title="Exact copies of pictures that end up in the library">
            <div className="tile-value">{count(extras)}</div>
            <div className="tile-label">Extra copies to quarantine</div>
            <div className="tile-sub muted small">identical to a picture in the library</div>
          </div>
        )}
      </div>
      <p className={short ? "num-bad" : "muted small"}>
        Room needed on the library's drive: {bytes(s.needed_bytes)}
        {s.free_bytes !== null && ` · ${bytes(s.free_bytes)} free`}
        {short && " — not enough: free some space or purge earlier quarantines first."}
      </p>
      {s.drives.length > 0 && (
        <table className="table small">
          <thead>
            <tr><th>Drive</th><th className="num">moved</th><th className="num">transferred</th>
              <th className="num">copied</th><th className="num">waiting</th><th className="num">extra copies</th></tr>
          </thead>
          <tbody>
            {s.drives.map((d) => (
              <tr key={d.volume_id ?? "any"}>
                <td>{d.label}{!d.online && <span className="muted"> (not connected)</span>}</td>
                {(["rename", "transfer", "copy", "wait"] as ExportMethod[]).map((m) => (
                  <td key={m} className="num">{d.methods[m] ? count(d.methods[m]!.files) : ""}</td>
                ))}
                <td className="num">{d.extras ? count(d.extras) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function FolderBrowser({ exportId, refresh }: { exportId: number; refresh: number }) {
  const [folder, setFolder] = useState("");
  const [page, setPage] = useState<ExportFolder | null>(null);
  const [selected, setSelected] = useState<ExportFile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setSelected(null);
    api.exportBrowse(exportId, folder).then((p) => { setPage(p); setError(null); }).catch((e: Error) => setError(e.message));
  }, [exportId, folder, refresh]);

  const more = () => {
    if (!page) return;
    api.exportBrowse(exportId, folder, page.files.length)
      .then((p) => setPage({ ...page, files: [...page.files, ...p.files] }));
  };
  const parts = folder ? folder.split("/") : [];
  return (
    <div className="organize-browser">
      <nav className="crumbs">
        <button className="link" onClick={() => setFolder("")}>Library</button>
        {parts.map((p, i) => (
          <span key={i}> / <button className="link" onClick={() => setFolder(parts.slice(0, i + 1).join("/"))}>{p}</button></span>
        ))}
      </nav>
      {error && <div className="note note-error">{error}</div>}
      {page && page.folders.length > 0 && (
        <div className="organize-folders">
          {page.folders.map((f) => (
            <button key={f.path} className="organize-folder" onClick={() => setFolder(f.path)}>
              <strong>{f.name}</strong>
              <span className="muted small">{plural(f.files, "picture")} · {bytes(f.bytes)}</span>
            </button>
          ))}
        </div>
      )}
      {page && page.files.length > 0 && (
        <div className="grid-list organize-grid">
          {page.files.map((f) => (
            <button key={f.dest} className={`cell ${selected?.dest === f.dest ? "selected" : ""}`}
              onClick={() => setSelected(f)} title={`${f.name}\nfrom ${f.source_label ?? "a connected drive"}: ${f.source_path ?? ""}`}>
              <div className="thumb">
                {f.has_preview ? <img src={previewUrl(f.hash)} alt="" loading="lazy" decoding="async" />
                  : <span className="thumb-placeholder">{f.kind}</span>}
                <span className={`method-tag method-${f.method}`}>{METHOD[f.method].label}</span>
                {f.status === "failed" && <span className="corner corner-corrupt" title={f.error ?? ""} />}
              </div>
              <div className="caption">{f.name}</div>
            </button>
          ))}
        </div>
      )}
      {page && page.files.length < page.total_files && (
        <button className="btn" onClick={more}>Show more ({count(page.total_files - page.files.length)} left)</button>
      )}
      {selected && (
        <div className="panel organize-detail">
          <strong>{selected.name}</strong> <span className="muted small">{bytes(selected.size)}
            {selected.width ? ` · ${selected.width}×${selected.height}` : ""}</span>
          <div className="small">Lands at <code>{selected.dest}</code></div>
          <div className="small">
            From <strong>{selected.source_label ?? "any connected drive"}</strong>
            {selected.source_path && <> <code>{selected.source_path}</code></>}
          </div>
          <div className="small muted">{METHOD[selected.method].label}: {METHOD[selected.method].hint}.</div>
          {selected.status !== "pending" && <div className="small">Status: {selected.status}
            {selected.error && <span className="num-bad"> — {selected.error}</span>}</div>}
        </div>
      )}
    </div>
  );
}
