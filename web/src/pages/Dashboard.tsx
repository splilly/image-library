import { useEffect, useRef, useState } from "react";
import { api, type ScanState, type Storage } from "../api";
import { Empty, ErrorNote, OnlineDot } from "../components/Bits";
import { Icon } from "../components/Icon";
import { ScanProgress } from "../components/ScanProgress";
import { bytes, count, navigate, useInterval, useLoad } from "../util";

export default function Dashboard({ scan }: { scan: ScanState | null }) {
  const summary = useLoad(() => api.summary(), []);
  const vols = useLoad(() => api.volumes(), []);
  const storage = useLoad(() => api.storage(), []);
  const [actionError, setActionError] = useState<string | null>(null);
  const running = !!scan?.running;

  // Totals move while scanning: refresh them every few seconds, and once when a scan ends.
  useInterval(() => { summary.reload(); vols.reload(); }, 4000, running);
  useInterval(() => storage.reload(), 15000, running);
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) { summary.reload(); vols.reload(); storage.reload(); }
    wasRunning.current = running;
  }, [running]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = (p: Promise<unknown>) => p.then(() => setActionError(null)).catch((e: Error) => setActionError(e.message));
  const s = summary.data;
  const online = vols.data?.filter((v) => v.online) ?? [];

  return (
    <div className="page">
      <h1>Overview</h1>
      <ErrorNote error={summary.error ?? actionError} />

      {s && (
        <div className="tiles">
          <Tile label="Pictures & videos" value={count(s.files)} sub={bytes(s.bytes)} />
          {s.other_files > 0 && (
            <Tile label="Other files" value={count(s.other_files)}
              sub={`${bytes(s.other_bytes)} · compared when matching folders`} />
          )}
          <Tile label="Unique images & videos" value={count(s.unique_contents)} />
          <Tile label="Duplicate copies" value={count(s.redundant_copies)} sub={`${bytes(s.redundant_bytes)} reclaimable`}
            onClick={() => navigate("library", { status: "duplicates" })} />
          <Tile label="Corrupt" value={count(s.corrupt)} tone={s.corrupt ? "bad" : undefined}
            onClick={() => navigate("library", { status: "corrupt" })} />
          <Tile label="Suspicious" value={count(s.warnings)} tone={s.warnings ? "warn" : undefined}
            onClick={() => navigate("library", { status: "warning" })} />
          <Tile label="Unreadable" value={count(s.read_errors + s.unsupported)}
            tone={s.read_errors ? "bad" : undefined} onClick={() => navigate("library", { status: "problems" })} />
          <Tile label="Not yet scanned" value={count(s.pending)} />
          {s.sidecar_files > 0 && (
            <Tile label="macOS metadata files" value={count(s.sidecar_files)}
              sub={`${bytes(s.sidecar_bytes)} · not pictures, left out`}
              onClick={() => navigate("library", { status: "sidecar" })} />
          )}
        </div>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>Scanning</h2>
          {running ? (
            <button className="btn" disabled={scan?.pausing} onClick={() => act(api.pauseScan())}>
              <Icon name="pause" size={15} /> {scan?.pausing ? "Pausing…" : "Pause"}
            </button>
          ) : (
            <div className="row">
              {!!s?.unfinished_scans.length && (
                <button className="btn btn-primary" onClick={() => act(api.startScan({ resume: true }))}>
                  <Icon name="play" size={15} /> Resume unfinished
                </button>
              )}
              <button className="btn" disabled={!online.length} onClick={() => act(api.startScan({ all_online: true }))}>
                Scan all connected drives
              </button>
            </div>
          )}
        </div>
        {scan?.notes.map((n) => <div key={n} className="note">{n}</div>)}
        {scan && scan.targets.length > 0 ? (
          <div className="scan-list">{scan.targets.map((t) => <ScanProgress key={t.scan_id} t={t} />)}</div>
        ) : (
          <p className="muted">No scan has run since the app started.</p>
        )}
        {!running && !!s?.unfinished_scans.length && (
          <div className="unfinished">
            <h3>Unfinished</h3>
            <ul>
              {s.unfinished_scans.map((u) => {
                const v = vols.data?.find((x) => x.id === u.volume_id);
                return (
                  <li key={`${u.volume_id}/${u.subpath}`}>
                    <OnlineDot online={!!v?.online} /> {u.label}{u.subpath ? ` / ${u.subpath}` : ""}
                    <span className="muted"> — {u.state}{v && !v.online ? ", waiting for the drive" : ""}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        <p className="muted small">
          Scans are saved continuously. Pausing, closing the app or a crash loses nothing; resume any time.
        </p>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Drives</h2>
          <a className="btn" href="#/drives">Manage drives</a>
        </div>
        {vols.data?.length === 0 && (
          <Empty>
            No drives yet. <a href="#/drives">Add your first drive</a> to start cataloguing.
          </Empty>
        )}
        <table className="table">
          <tbody>
            {vols.data?.map((v) => (
              <tr key={v.id} onClick={() => navigate("library", { volume: v.id })} className="clickable">
                <td><OnlineDot online={v.online || !!v.agent} /> {v.label} {v.read_only && <span className="tag">read-only</span>}
                  {v.agent && !v.online && <span className="tag">on {v.agent.machine ?? "agent"}</span>}</td>
                <td className="num">{count(v.stats.files)} files</td>
                <td className="num">{bytes(v.stats.bytes)}</td>
                <td className="num muted">{v.stats.pending ? `${count(v.stats.pending)} not scanned` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {storage.data && <StoragePanel st={storage.data} images={s?.unique_contents ?? 0} />}
    </div>
  );
}

/** How much room the catalog takes, so it can be watched as the library grows. */
function StoragePanel({ st, images }: { st: Storage; images: number }) {
  const small = st.preview_counts?.s ?? 0;
  const large = st.preview_counts?.m ?? 0;
  const perImage = images ? (st.database + st.previews) / images : 0;
  const low = st.disk_free !== null && st.disk_total !== null &&
    (st.disk_free < 10 * 1024 ** 3 || st.disk_free < 0.05 * st.disk_total);
  return (
    <section className={`panel storage ${low ? "storage-low" : ""}`}>
      <div className="panel-head"><h2>Storage</h2><span className="muted small">{bytes(st.total)} in total</span></div>
      <div className="storage-grid">
        <div><div className="tile-value">{bytes(st.database)}</div><div className="tile-label">Database</div></div>
        <div>
          <div className="tile-value">{bytes(st.previews)}</div>
          <div className="tile-label">Previews</div>
          <div className="tile-sub">{count(small)} small · {count(large)} large
            {small ? ` · ${bytes(st.previews / small)} per picture` : ""}</div>
        </div>
        {st.disk_free !== null && (
          <div>
            <div className="tile-value">{bytes(st.disk_free)}</div>
            <div className="tile-label">Free on this disk</div>
            <div className="tile-sub">of {bytes(st.disk_total)}</div>
          </div>
        )}
      </div>
      {low && <div className="note note-warn">The disk holding the catalog is nearly full. Scans stop working when it fills up;
        free some space or move the catalog folder to a larger disk.</div>}
      <p className="muted small">
        Kept in <code>{st.dir}</code>{images ? ` · about ${bytes(perImage)} per picture so far` : ""}.
        Back up <code>catalog.db</code> (the database) now and then; previews can be rebuilt by scanning again.
      </p>
    </section>
  );
}

function Tile({ label, value, sub, tone, onClick }: {
  label: string; value: string; sub?: string; tone?: "bad" | "warn"; onClick?: () => void;
}) {
  return (
    <div className={`tile ${tone ? `tile-${tone}` : ""} ${onClick ? "clickable" : ""}`} onClick={onClick}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  );
}
