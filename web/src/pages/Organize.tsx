import { useEffect, useRef, useState } from "react";
import { api, type ExportInfo, type ExportOptions, type ScanState } from "../api";
import { ErrorNote, Modal, ProgressBar } from "../components/Bits";
import { FolderBrowser, Totals } from "../components/OrganizePreview";
import { FolderPicker } from "../components/FolderPicker";
import { bytes, count, navigate, useLoad } from "../util";

/** Gather one of each picture into a new library sorted by date: previewed first, then moved or copied. */
export default function Organize({ scan, params }: { scan: ScanState | null; params: URLSearchParams }) {
  const exports = useLoad(() => api.exports(), []);
  const open = Number(params.get("id")) || null;
  const [target, setTarget] = useState<string | null>(null);
  const [folderName, setFolderName] = useState("Photo Library");
  const [opts, setOpts] = useState<ExportOptions>({ include_videos: true, include_raw: true, include_damaged: false,
    mode: "move" });
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const progress = scan?.export ?? null;
  const running = !!progress?.running;

  const key = progress ? `${progress.state}:${progress.done}` : "";
  const last = useRef(key);
  useEffect(() => {
    if (key !== last.current && !running) exports.reload();
    last.current = key;
  }, [key, running]); // eslint-disable-line react-hooks/exhaustive-deps

  const prepare = () => {
    if (!target) return;
    const sep = target.includes("\\") ? "\\" : "/";
    const dest = folderName.trim() ? target.replace(/[\\/]+$/, "") + sep + folderName.trim() : target;
    setPreparing(true);
    api.createExport(dest, opts)
      .then((e) => { setError(null); exports.reload(); navigate("organize", { id: String(e.id) }); })
      .catch((e: Error) => setError(e.message))
      .finally(() => setPreparing(false));
  };

  if (open) {
    return <LibraryView id={open} scan={scan} onChanged={exports.reload} />;
  }
  return (
    <div className="page">
      <h1>Organize</h1>
      <p className="muted">
        Gathers one of each picture into a new library sorted as <code>YEAR/YEAR-MONTH-DAY/name</code>. Duplicates,
        files you marked to discard, thumbnails of pictures that go in and extra versions of similar images are left
        out; thumbnails whose original is lost go in. You see the proposed library before anything happens.
      </p>
      <ErrorNote error={error} />
      {running && progress && <RunningPanel scan={scan} />}
      {!running && (
        <section className="panel organize-new">
          <h2>New library</h2>
          <p className="small muted">Where should it go?</p>
          <FolderPicker path={target} onChange={setTarget} />
          <label className="field">
            <span>New folder inside it</span>
            <input value={folderName} onChange={(e) => setFolderName(e.target.value)} />
          </label>
          <div className="organize-modes">
            <label className="check">
              <input type="radio" checked={opts.mode === "move"} onChange={() => setOpts({ ...opts, mode: "move" })} />
              <span><strong>Move</strong> pictures into it. Needs no room for a second copy: pictures already on that
                drive are moved instantly; pictures from other drives are copied, checked, and their originals put in
                quarantine there. Their other identical copies are quarantined too. Everything can be undone.</span>
            </label>
            <label className="check">
              <input type="radio" checked={opts.mode === "copy"} onChange={() => setOpts({ ...opts, mode: "copy" })} />
              <span><strong>Copy</strong> pictures into it. Nothing else changes; needs room for every picture.</span>
            </label>
          </div>
          <div className="row">
            <label className="check"><input type="checkbox" checked={opts.include_raw}
              onChange={(e) => setOpts({ ...opts, include_raw: e.target.checked })} /> RAW files</label>
            <label className="check"><input type="checkbox" checked={opts.include_videos}
              onChange={(e) => setOpts({ ...opts, include_videos: e.target.checked })} /> Videos</label>
            <label className="check"><input type="checkbox" checked={opts.include_damaged}
              onChange={(e) => setOpts({ ...opts, include_damaged: e.target.checked })} /> Damaged files</label>
          </div>
          <button className="btn btn-primary" disabled={!target || preparing} onClick={prepare}>
            {preparing ? "Preparing the preview…" : "Prepare a preview"}
          </button>
          <p className="small muted">Nothing is moved or copied until you start it from the preview.</p>
        </section>
      )}
      {(exports.data?.length ?? 0) > 0 && (
        <section className="panel">
          <h2>Libraries</h2>
          <table className="table">
            <tbody>
              {exports.data!.map((e) => (
                <tr key={e.id} className="clickable" onClick={() => navigate("organize", { id: String(e.id) })}>
                  <td><code>{e.target}</code>
                    <div className="muted small">{new Date(e.created_at * 1000).toLocaleString()} · {e.mode === "move" ? "move" : "copy"}</div></td>
                  <td className="num">{count(e.counts.done?.files ?? 0)} in the library
                    <div className="muted small">{bytes(e.counts.done?.bytes ?? 0)}</div></td>
                  <td><span className="badge">{stateLabel(e)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

function stateLabel(e: ExportInfo): string {
  if (e.state === "planned") return "Preview";
  if (e.state === "undone") return "Undone";
  const todo = (e.counts.pending?.files ?? 0) + (e.counts.failed?.files ?? 0);
  return todo ? `${count(todo)} to go` : "Complete";
}

function RunningPanel({ scan }: { scan: ScanState | null }) {
  const p = scan?.export;
  if (!p) return null;
  return (
    <section className="panel">
      <h2>Organizing</h2>
      <ProgressBar value={p.done} max={p.total} />
      <div className="scan-numbers">
        <span>{count(p.done)} / {count(p.total)}</span>
        <span>{count(p.copied)} in the library ({bytes(p.copied_bytes)})</span>
        {p.failed > 0 && <span className="num-bad">{count(p.failed)} failed</span>}
        {p.waiting > 0 && <span>{count(p.waiting)} waiting</span>}
      </div>
      {p.current && <div className="scan-current">{p.current}</div>}
      <button className="btn" onClick={() => api.pauseExport(p.export_id)}>Pause</button>
    </section>
  );
}

function LibraryView({ id, scan, onChanged }: { id: number; scan: ScanState | null; onChanged: () => void }) {
  const summary = useLoad(() => api.exportSummary(id), [id]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"purge" | "undo" | null>(null);
  const [refresh, setRefresh] = useState(0);
  const progress = scan?.export?.export_id === id ? scan.export : null;
  const running = !!progress?.running;
  const busy = !!(scan?.running || scan?.plan?.running || scan?.merge?.running);

  // Refresh whenever this library's run moves on (a short run can finish between two looks).
  const step = progress ? `${progress.state}:${progress.done}:${progress.running}` : "";
  const lastStep = useRef(step);
  useEffect(() => {
    if (step !== lastStep.current) { summary.reload(); setRefresh((n) => n + 1); onChanged(); }
    lastStep.current = step;
  }, [step]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = (promise: Promise<unknown>, done?: (r: never) => string | null) =>
    promise.then((r) => {
      setError(null); setNote(done ? done(r as never) : null);
      summary.reload(); setRefresh((n) => n + 1); onChanged();
    }).catch((e: Error) => setError(e.message));

  const s = summary.data;
  if (!s) return <div className="page"><ErrorNote error={summary.error} /></div>;
  const todo = (s.counts.pending?.files ?? 0) + (s.counts.failed?.files ?? 0);
  const quarantined = s.mode === "move" && s.state === "started";
  return (
    <div className="page wide">
      <button className="link" onClick={() => navigate("organize")}>← Organize</button>
      <h1>{s.state === "planned" ? "Preview: " : ""}<code>{s.target}</code></h1>
      <p className="muted">
        {s.mode === "move" ? "Moving" : "Copying"} {count(Object.values(s.counts).reduce((n, c) => n + c.files, 0))}{" "}
        pictures{s.target_label ? ` into a library on ${s.target_label}` : ""}.
        {s.state === "planned" && " Nothing has changed yet: browse the proposed folders below, then start it or discard it."}
      </p>
      <ErrorNote error={error} />
      {note && <div className="note">{note}</div>}
      {running && <RunningPanel scan={scan} />}
      {!running && (
        <div className="row">
          {(s.state === "planned" || todo > 0) && s.state !== "undone" && (
            <button className="btn btn-primary" disabled={busy}
              title={busy ? "Wait for the running scan, plan or merge" : undefined}
              onClick={() => act(api.runExport(id))}>
              {s.state === "planned" ? (s.mode === "move" ? "Start moving" : "Start copying") : "Continue"}
            </button>
          )}
          {s.state === "planned" && (
            <button className="btn" onClick={() => act(api.discardExport(id).then(() => navigate("organize")))}>
              Discard this preview
            </button>
          )}
          {quarantined && <button className="btn" onClick={() => setConfirm("undo")}>Undo…</button>}
          {quarantined && <button className="btn btn-danger" onClick={() => setConfirm("purge")}>Purge quarantine…</button>}
        </div>
      )}
      <Totals s={s} />
      <FolderBrowser exportId={id} refresh={refresh} />
      {confirm && (
        <Modal title={confirm === "undo" ? "Undo this library" : "Delete the quarantined files"}
          onClose={() => setConfirm(null)} footer={
            <>
              <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
              <button className={`btn ${confirm === "purge" ? "btn-danger" : "btn-primary"}`} onClick={() => {
                setConfirm(null);
                if (confirm === "undo") {
                  act(api.undoExport(id), (r: { problems: string[] }) =>
                    r.problems.length ? `Undone, except: ${r.problems.slice(0, 5).join("; ")}` : "Everything is back where it was.");
                } else {
                  act(api.purgeExport(id), (r: { bytes: number; waiting: string[] }) =>
                    `Freed ${bytes(r.bytes)}.` + (r.waiting.length ? ` Connect ${r.waiting.join(", ")} to purge there too.` : ""));
                }
              }}>{confirm === "undo" ? "Undo" : "Delete permanently"}</button>
            </>
          }>
          {confirm === "undo" ? (
            <p>Every picture moved into the library goes back where it came from, copies made from other drives are
              removed, and the quarantined copies are put back. The drives involved must be connected.</p>
          ) : (
            <p>Permanently delete the originals and extra copies this library put in quarantine, on every connected
              drive. The library itself is not touched. This cannot be undone.</p>
          )}
        </Modal>
      )}
    </div>
  );
}
