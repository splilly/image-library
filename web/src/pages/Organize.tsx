import { useEffect, useRef, useState } from "react";
import { api, type ScanState } from "../api";
import { ErrorNote, ProgressBar } from "../components/Bits";
import { FolderPicker } from "../components/FolderPicker";
import { bytes, count, useLoad } from "../util";

/** Copy one of each picture into a new library sorted by date. */
export default function Organize({ scan }: { scan: ScanState | null }) {
  const exports = useLoad(() => api.exports(), []);
  const [target, setTarget] = useState<string | null>(null);
  const [folderName, setFolderName] = useState("Photo Library");
  const [opts, setOpts] = useState({ include_videos: true, include_raw: true, include_damaged: false });
  const [error, setError] = useState<string | null>(null);
  const progress = scan?.export ?? null;
  const running = !!progress?.running;

  const key = progress ? `${progress.state}:${progress.done}` : "";
  const last = useRef(key);
  useEffect(() => {
    if (key !== last.current && !running) exports.reload();
    last.current = key;
  }, [key, running]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = () => {
    if (!target) return;
    const sep = target.includes("\\") ? "\\" : "/";
    const dest = folderName.trim() ? target.replace(/[\\/]+$/, "") + sep + folderName.trim() : target;
    api.createExport(dest, opts)
      .then((e) => api.runExport(e.id))
      .then(() => { setError(null); exports.reload(); })
      .catch((e: Error) => setError(e.message));
  };

  return (
    <div className="page">
      <h1>Organize</h1>
      <p className="muted">
        Copies one of each picture into a new folder, sorted as <code>YEAR/YEAR-MONTH-DAY/name</code>. Duplicates,
        files you marked to discard, thumbnails of pictures that are being copied and extra versions of similar
        images are left out; thumbnails whose original is lost are copied. Nothing is moved or deleted, and every
        copy is read back and checked.
      </p>
      <ErrorNote error={error} />
      {running && progress && (
        <section className="panel">
          <h2>Copying</h2>
          <ProgressBar value={progress.done} max={progress.total} />
          <div className="scan-numbers">
            <span>{count(progress.done)} / {count(progress.total)}</span>
            <span>{count(progress.copied)} copied ({bytes(progress.copied_bytes)})</span>
            {progress.failed > 0 && <span className="num-bad">{count(progress.failed)} failed</span>}
            {progress.waiting > 0 && <span>{count(progress.waiting)} waiting for a drive</span>}
          </div>
          {progress.current && <div className="scan-current">{progress.current}</div>}
          <button className="btn" onClick={() => api.pauseExport(progress.export_id)}>Pause</button>
        </section>
      )}
      {!running && (
        <section className="panel organize-new">
          <h2>New library</h2>
          <p className="small muted">Where should it go? Pick a drive or folder with enough free space.</p>
          <FolderPicker path={target} onChange={setTarget} />
          <label className="field">
            <span>New folder inside it</span>
            <input value={folderName} onChange={(e) => setFolderName(e.target.value)} />
          </label>
          <div className="row">
            <label className="check"><input type="checkbox" checked={opts.include_raw}
              onChange={(e) => setOpts({ ...opts, include_raw: e.target.checked })} /> RAW files</label>
            <label className="check"><input type="checkbox" checked={opts.include_videos}
              onChange={(e) => setOpts({ ...opts, include_videos: e.target.checked })} /> Videos</label>
            <label className="check"><input type="checkbox" checked={opts.include_damaged}
              onChange={(e) => setOpts({ ...opts, include_damaged: e.target.checked })} /> Damaged files</label>
          </div>
          <button className="btn btn-primary" disabled={!target || !!scan?.running || !!scan?.plan?.running} onClick={start}>
            Copy into {target ? `${target.replace(/[\\/]+$/, "")}/${folderName}` : "…"}
          </button>
        </section>
      )}
      {(exports.data?.length ?? 0) > 0 && (
        <section className="panel">
          <h2>Earlier exports</h2>
          <table className="table">
            <tbody>
              {exports.data!.map((e) => {
                const todo = (e.counts.pending?.files ?? 0) + (e.counts.failed?.files ?? 0);
                return (
                  <tr key={e.id}>
                    <td><code>{e.target}</code><div className="muted small">{new Date(e.created_at * 1000).toLocaleString()}</div></td>
                    <td className="num">{count(e.counts.done?.files ?? 0)} copied
                      <div className="muted small">{bytes(e.counts.done?.bytes ?? 0)}</div></td>
                    <td className="num">{todo > 0 ? `${count(todo)} to go` : "complete"}
                      {(e.counts.failed?.files ?? 0) > 0 && <div className="num-bad small">{count(e.counts.failed!.files)} failed</div>}</td>
                    <td>{todo > 0 && !running && (
                      <button className="btn btn-small" onClick={() => api.runExport(e.id).catch((x: Error) => setError(x.message))}>
                        Continue
                      </button>
                    )}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {progress && !running && progress.waiting_volumes.length > 0 && (
            <div className="note">Waiting for: {progress.waiting_volumes.join(", ")}. Connect them and press Continue.</div>
          )}
        </section>
      )}
    </div>
  );
}
