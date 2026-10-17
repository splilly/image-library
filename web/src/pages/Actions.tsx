import { useEffect, useRef, useState } from "react";
import { api, type Counts, type PlanItem, type ScanState } from "../api";
import { Empty, ErrorNote, Modal, OnlineDot, ProgressBar } from "../components/Bits";
import { bytes, count, navigate, plural, useLoad } from "../util";

const STATUS_LABEL: Record<string, string> = {
  pending: "To move",
  moving: "Moving",
  quarantined: "In quarantine",
  blocked: "Blocked (safety check)",
  skipped: "Skipped",
  failed: "Failed",
  restored: "Restored",
  purged: "Deleted",
};
const ORDER = ["pending", "quarantined", "blocked", "skipped", "failed", "restored", "purged", "moving"];

function n(c: Counts, ...keys: string[]) {
  return keys.reduce((sum, k) => sum + (c[k]?.files ?? 0), 0);
}
function b(c: Counts, ...keys: string[]) {
  return keys.reduce((sum, k) => sum + (c[k]?.bytes ?? 0), 0);
}

export default function Actions({ params, scan }: { params: URLSearchParams; scan: ScanState | null }) {
  const plans = useLoad(() => api.plans(), []);
  const summary = useLoad(() => api.decisionSummary(), []);
  const [error, setError] = useState<Error | null>(null);
  const selected = Number(params.get("plan")) || plans.data?.[0]?.id || null;

  const create = () =>
    api.createPlan()
      .then((p) => {
        setError(p.id ? null : new Error("Every discard decision is already in a plan."));
        plans.reload();
        summary.reload();
        if (p.id) navigate("actions", { plan: String(p.id) });
      })
      .catch((e: Error) => setError(e));

  return (
    <div className="page wide actions">
      <div className="page-head">
        <h1>Actions</h1>
        <button className="btn btn-primary" onClick={create} disabled={!summary.data?.unplanned_discard}
          title={summary.data?.unplanned_discard ? "" : "Every file marked to discard is already in a plan"}>
          {summary.data?.unplanned_discard
            ? `Create plan for ${plural(summary.data.unplanned_discard, "new decision")}`
            : "No new decisions to plan"}
        </button>
      </div>
      <p className="muted">
        A plan moves the files you marked to discard into a <code>_imagelib_quarantine</code> folder on their own
        drive. That is instant and can be undone. Deleting them for good is a separate, last step. Drives registered
        read-only are never touched; you get a list instead.
      </p>
      <ErrorNote error={error?.message} />
      {plans.data?.length === 0 && <Empty>No plans yet. Mark files to discard on the <a href="#/review">Review</a> page first.</Empty>}
      <div className="plans-layout">
        <ul className="plan-list">
          {plans.data?.map((p) => (
            <li key={p.id}>
              <button className={p.id === selected ? "active" : ""} onClick={() => navigate("actions", { plan: String(p.id) })}>
                <strong>Plan {p.id}</strong>
                <span className="muted small">{new Date(p.created_at * 1000).toLocaleString()}</span>
                <span className="small">
                  {n(p.counts, "pending", "blocked", "failed") > 0 && <>{count(n(p.counts, "pending", "blocked", "failed"))} to do · </>}
                  {count(n(p.counts, "quarantined"))} in quarantine
                  {n(p.counts, "purged") > 0 && <> · {count(n(p.counts, "purged"))} deleted</>}
                </span>
              </button>
            </li>
          ))}
        </ul>
        {selected && <PlanView key={selected} id={selected} scan={scan} onChanged={plans.reload} />}
      </div>
    </div>
  );
}

function PlanView({ id, scan, onChanged }: { id: number; scan: ScanState | null; onChanged: () => void }) {
  const detail = useLoad(() => api.plan(id), [id]);
  const [verify, setVerify] = useState(true);
  const [requireConnected, setRequireConnected] = useState(true);
  const [status, setStatus] = useState("");
  const [error, setError] = useState<Error | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [purging, setPurging] = useState(false);
  const progress = scan?.plan && scan.plan.plan_id === id ? scan.plan : null;
  const running = !!progress?.running;

  // Refresh the counts whenever a run reports new results. (A short run can finish
  // between two progress updates, so watch the results, not the running flag.)
  const resultKey = progress ? `${progress.state}:${progress.done}:${progress.moved}:${progress.blocked}` : "";
  const lastKey = useRef(resultKey);
  useEffect(() => {
    if (resultKey !== lastKey.current && !running) { detail.reload(); onChanged(); }
    lastKey.current = resultKey;
  }, [resultKey, running]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = (p: Promise<unknown>, after?: (r: any) => void) =>
    p.then((r) => { setError(null); after?.(r); detail.reload(); onChanged(); }).catch((e: Error) => setError(e));

  const totals: Counts = {};
  for (const v of detail.data?.volumes ?? []) {
    for (const [k, c] of Object.entries(v.counts)) {
      totals[k] = { files: (totals[k]?.files ?? 0) + c.files, bytes: (totals[k]?.bytes ?? 0) + c.bytes };
    }
  }
  const runnable = (detail.data?.volumes ?? []).filter((v) => !v.read_only && v.online)
    .reduce((s, v) => s + n(v.counts, "pending", "blocked", "failed"), 0);

  return (
    <div className="plan-view">
      <ErrorNote error={error?.message} />
      {note && <div className="note">{note}</div>}
      <table className="table plan-drives">
        <thead>
          <tr><th>Drive</th><th className="num">To move</th><th className="num">In quarantine</th>
            <th className="num">Blocked / failed</th><th className="num">Skipped</th><th className="num">Restored / deleted</th><th /></tr>
        </thead>
        <tbody>
          {detail.data?.volumes.map((v) => (
            <tr key={v.volume_id}>
              <td>
                <OnlineDot online={v.online} /> {v.label}
                {v.read_only && <span className="tag">read-only</span>}
                {!v.online && !v.read_only && <div className="muted small">connect it to carry out its part</div>}
              </td>
              <td className="num">{count(n(v.counts, "pending"))} <span className="muted small">{bytes(b(v.counts, "pending"))}</span></td>
              <td className="num">{count(n(v.counts, "quarantined"))}</td>
              <td className="num">{count(n(v.counts, "blocked", "failed"))}</td>
              <td className="num">{count(n(v.counts, "skipped"))}</td>
              <td className="num">{count(n(v.counts, "restored"))} / {count(n(v.counts, "purged"))}</td>
              <td>
                {v.read_only && n(v.counts, "pending", "blocked", "failed") > 0 && (
                  <a className="btn btn-small" href={api.manualListUrl(id, v.volume_id)}>Download list to delete yourself</a>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <section className="panel">
        <h2>Move to quarantine</h2>
        {running && progress ? (
          <>
            <ProgressBar value={progress.done} max={progress.total} />
            <div className="scan-numbers">
              <span>{count(progress.done)} / {count(progress.total)} checked</span>
              <span>{count(progress.moved)} moved ({bytes(progress.moved_bytes)})</span>
              <span className={progress.blocked ? "num-warn" : ""}>{count(progress.blocked)} blocked</span>
              <span>{count(progress.skipped)} skipped</span>
              {progress.failed > 0 && <span className="num-bad">{count(progress.failed)} failed</span>}
            </div>
            {progress.current && <div className="scan-current">{progress.current}</div>}
            <button className="btn" onClick={() => act(api.pausePlan(id))}>Pause</button>
          </>
        ) : (
          <>
            {progress && progress.state !== "starting" && (
              <div className="note">
                Last run: {count(progress.moved)} moved ({bytes(progress.moved_bytes)}), {count(progress.blocked)} blocked,{" "}
                {count(progress.skipped)} skipped, {count(progress.failed)} failed. {progress.message}
              </div>
            )}
            <label className="check">
              <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />
              Re-check each file's contents before moving it (reads every file once more; recommended)
            </label>
            <label className="check">
              <input type="checkbox" checked={requireConnected} onChange={(e) => setRequireConnected(e.target.checked)} />
              Only move a file if a copy being kept is on a connected drive and is actually found there
            </label>
            {!requireConnected && (
              <div className="note note-warn small">
                Files will be moved even if the only kept copy is on a disconnected drive, trusting the catalog that it is
                still there. Use this only for drives you know are healthy.
              </div>
            )}
            <div className="row">
              <button className="btn btn-primary" disabled={runnable === 0 || !!scan?.running}
                onClick={() => act(api.runPlan(id, { verify_hash: verify, require_connected_copy: requireConnected }))}>
                Move {count(runnable)} files to quarantine
              </button>
              {scan?.running && <span className="muted small">Wait for the running scan to finish.</span>}
            </div>
          </>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Files</h2>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All</option>
            {ORDER.filter((s) => totals[s]).map((s) => (
              <option key={s} value={s}>{STATUS_LABEL[s]} ({count(totals[s].files)})</option>
            ))}
          </select>
        </div>
        <ItemList id={id} status={status} version={detail.data ? JSON.stringify(totals) : ""}
          onRestore={(itemIds) => act(api.restorePlan(id, itemIds), (r) =>
            setNote(`Restored ${plural(r.restored, "file")}.${r.problems.length ? " " + r.problems.join("; ") : ""}`))} />
      </section>

      {n(totals, "quarantined") > 0 && (
        <section className="panel danger-zone">
          <h2>Quarantine</h2>
          <p>
            {count(n(totals, "quarantined"))} files ({bytes(b(totals, "quarantined"))}) are in quarantine and can still be
            restored.
          </p>
          <div className="row">
            <button className="btn" disabled={running} onClick={() => act(api.restorePlan(id), (r) =>
              setNote(`Restored ${plural(r.restored, "file")}.${r.problems.length ? " " + r.problems.join("; ") : ""}`))}>
              Restore all
            </button>
            <button className="btn btn-danger" disabled={running} onClick={() => setPurging(true)}>Delete permanently…</button>
          </div>
        </section>
      )}
      {purging && (
        <PurgeDialog files={n(totals, "quarantined")} size={b(totals, "quarantined")} onClose={() => setPurging(false)}
          onConfirm={(text) => { setPurging(false); act(api.purgePlan(id, text), (r) =>
            setNote(`Deleted ${plural(r.purged, "file")}, freeing ${bytes(r.bytes)}.${r.problems.length ? " " + r.problems.join("; ") : ""}`)); }} />
      )}
    </div>
  );
}

function ItemList({ id, status, version, onRestore }: {
  id: number; status: string; version: string; onRestore: (ids: number[]) => void;
}) {
  const [items, setItems] = useState<PlanItem[]>([]);
  const [next, setNext] = useState<number | null>(0);
  const load = (reset: boolean) => {
    const after = reset ? 0 : next;
    if (after === null) return;
    api.planItems(id, status || undefined, after)
      .then((p) => { setItems((prev) => (reset ? p.items : [...prev, ...p.items])); setNext(p.next); });
  };
  useEffect(() => load(true), [id, status, version]); // eslint-disable-line react-hooks/exhaustive-deps

  if (items.length === 0) return <p className="muted">Nothing here.</p>;
  return (
    <>
      <table className="table plan-items">
        <tbody>
          {items.map((it) => (
            <tr key={it.id}>
              <td>
                <div className="loc-path">{it.volume_label}: {it.path}</div>
                {it.error && <div className={`small ${it.status === "blocked" ? "num-warn" : "muted"}`}>{it.error}</div>}
              </td>
              <td className="small muted">{it.reason}</td>
              <td className="num small">{bytes(it.size)}</td>
              <td><span className={`badge item-${it.status}`}>{STATUS_LABEL[it.status] ?? it.status}</span></td>
              <td>{it.status === "quarantined" && <button className="btn btn-small" onClick={() => onRestore([it.id])}>Restore</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {next !== null && <button className="btn" onClick={() => load(false)}>Show more</button>}
    </>
  );
}

function PurgeDialog({ files, size, onClose, onConfirm }: {
  files: number; size: number; onClose: () => void; onConfirm: (text: string) => void;
}) {
  const [text, setText] = useState("");
  return (
    <Modal title="Delete permanently" onClose={onClose} footer={
      <>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-danger" disabled={text !== "delete"} onClick={() => onConfirm(text)}>
          Delete {count(files)} files
        </button>
      </>
    }>
      <p>
        This permanently deletes the {count(files)} files ({bytes(size)}) this plan moved into quarantine, on every
        connected drive. <strong>It cannot be undone.</strong> Files on drives that are not connected stay in quarantine.
      </p>
      <label className="field">
        <span>Type <code>delete</code> to confirm</span>
        <input value={text} onChange={(e) => setText(e.target.value)} autoFocus />
      </label>
    </Modal>
  );
}
