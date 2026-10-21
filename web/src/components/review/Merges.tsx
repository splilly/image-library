import { useState } from "react";
import { api, type MergeInfo } from "../../api";
import { ago, bytes, count, plural, useInterval, useLoad } from "../../util";
import { Modal, ProgressBar } from "../Bits";
import { ActionError } from "./shared";

const STATUS: Record<MergeInfo["status"], string> = {
  planned: "Ready to run",
  running: "Running",
  blocked: "Stopped",
  failed: "Failed",
  merged: "Merged",
  restored: "Undone",
  purged: "Merged, quarantine deleted",
};

const where = (root: string) => root || "(top of the drive)";

/** What a prepared merge will do, then its progress once started. ``fresh``: just prepared, so closing
 * the dialog without running it forgets it again. */
export function MergeDialog({ prepared, fresh = false, onClose }: {
  prepared: MergeInfo; fresh?: boolean; onClose: () => void;
}) {
  const [info, setInfo] = useState(prepared);
  const [started, setStarted] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const live = started && (info.status === "planned" || info.status === "running" || !!info.progress?.running);
  useInterval(() => { api.merge(info.id).then(setInfo).catch(setError); }, 700, live);

  const start = () => {
    setStarted(true);
    api.runMerge(info.id).then(setInfo).catch((e: Error) => { setError(e); setStarted(false); });
  };
  const cancel = () => {
    if (fresh && !started) api.cancelMerge(info.id).catch(() => undefined);
    onClose();
  };
  const c = info.counts;
  const p = info.progress;
  return (
    <Modal title={`Merge into ${where(info.keep_root)}`} onClose={cancel} footer={
      !started ? (
        <>
          <button className="btn" onClick={cancel}>Cancel</button>
          <button className="btn btn-primary" onClick={start}>Merge</button>
        </>
      ) : (
        <button className="btn" onClick={onClose} disabled={live}>{live ? "Merging…" : "Close"}</button>
      )
    }>
      <ActionError error={error} />
      <p>
        Keep <strong>{info.volume_label}</strong> <code>{where(info.keep_root)}</code> and bring into it what only{" "}
        <code>{info.drop_root}</code> has:
      </p>
      <ul className="merge-plan">
        <li>
          <strong>{plural(c.move.files, "file")}</strong> ({bytes(c.move.bytes)}) that only{" "}
          <code>{info.drop_root}</code> has {c.move.files === 1 ? "moves" : "move"} to the same place in the kept tree.
          <Examples items={info.examples.move} />
        </li>
        {c.conflict.files > 0 && (
          <li className="num-warn">
            <strong>{plural(c.conflict.files, "file")}</strong> whose path is taken by different content{" "}
            {c.conflict.files === 1 ? "is" : "are"} kept too, beside it, named “… (imagelib merge {info.id})”.
            <Examples items={info.examples.conflict} />
          </li>
        )}
        <li>
          <strong>{plural(c.covered.files, "file")}</strong> ({bytes(c.covered.bytes)}){" "}
          {c.covered.files === 1 ? "is" : "are"} already in the kept tree and not copied.
        </li>
      </ul>
      <p>
        Then the rest of <code>{info.drop_root}</code> is moved to <code>_imagelib_quarantine/merge-{info.id}</code>{" "}
        on the same drive. Nothing is deleted: <strong>Undo</strong> puts every file back where it was.
      </p>
      <p className="muted small">
        The drive is checked first: if the folders changed since they were scanned, nothing is moved.
      </p>
      {started && (
        <div className="merge-progress">
          {p && p.total > 0 && <ProgressBar value={p.done} max={p.total} />}
          <p>
            {info.status === "merged" ? <strong className="num-ok">Merged.</strong> :
              info.status === "blocked" || info.status === "failed" ? <strong className="num-bad">{info.error}</strong> :
                p?.state === "checking" ? "Checking the drive…" :
                  p?.state === "quarantining" ? "Moving the rest to quarantine…" :
                    p ? `Moving ${count(p.done)} of ${count(p.total)}…` : "Starting…"}
          </p>
          <Problems items={p?.problems ?? []} />
        </div>
      )}
    </Modal>
  );
}

function Examples({ items }: { items: { from: string; to: string }[] }) {
  if (!items.length) return null;
  return (
    <ul className="small muted merge-examples">
      {items.slice(0, 4).map((e) => <li key={e.from}><code>{e.from}</code> → <code>{e.to}</code></li>)}
    </ul>
  );
}

function Problems({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <ul className="small merge-examples">
      {items.slice(0, 10).map((p) => <li key={p}>{p}</li>)}
      {items.length > 10 && <li>…and {count(items.length - 10)} more</li>}
    </ul>
  );
}

/** Merges made so far, with Undo and Purge. */
export function MergesPanel({ refresh }: { refresh: number }) {
  const list = useLoad(() => api.merges(), [refresh]);
  const [error, setError] = useState<Error | null>(null);
  const [purging, setPurging] = useState<MergeInfo | null>(null);
  const [open, setOpen] = useState<MergeInfo | null>(null);
  const running = list.data?.some((m) => m.status === "running") ?? false;
  useInterval(() => list.reload(), 1500, running);
  if (!list.data?.length) return null;

  const act = (promise: Promise<unknown>) => promise.then(() => { setError(null); list.reload(); }).catch(setError);
  return (
    <section className="panel merges">
      <h3>Merges</h3>
      <ActionError error={error} />
      <table className="table small">
        <tbody>
          {list.data.map((m) => (
            <tr key={m.id}>
              <td>#{m.id}</td>
              <td>
                <code>{m.drop_root}</code> into <code>{where(m.keep_root)}</code>
                <span className="muted"> · {m.volume_label} · {ago(m.finished_at ?? m.created_at)}</span>
                {m.error && <div className="num-bad">{m.error}</div>}
              </td>
              <td className="num">
                {count(m.counts.move.files + m.counts.conflict.files)} brought over
                {m.counts.conflict.files > 0 && (
                  <span className="num-warn"> ({plural(m.counts.conflict.files, "conflict")})</span>
                )}
              </td>
              <td><span className="badge">{STATUS[m.status]}</span></td>
              <td className="row-actions">
                {(m.status === "planned" || m.status === "blocked" || m.status === "failed") && (
                  <button className="btn btn-small" onClick={() => setOpen(m)}>Run…</button>
                )}
                {m.status === "running" && (
                  <button className="btn btn-small" onClick={() => act(api.pauseMerge(m.id))}>Pause</button>
                )}
                {(m.status === "merged" || m.status === "blocked" || m.status === "failed") && (
                  <button className="btn btn-small" onClick={() => act(api.undoMerge(m.id))}>Undo</button>
                )}
                {m.status === "merged" && (
                  <button className="btn btn-small btn-danger" onClick={() => setPurging(m)}>Purge…</button>
                )}
                {m.status === "planned" && (
                  <button className="btn btn-small" onClick={() => act(api.cancelMerge(m.id))}>Cancel</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {open && <MergeDialog prepared={open} onClose={() => { setOpen(null); list.reload(); }} />}
      {purging && (
        <Modal title="Delete the quarantined tree" onClose={() => setPurging(null)} footer={
          <>
            <button className="btn" onClick={() => setPurging(null)}>Cancel</button>
            <button className="btn btn-danger" onClick={() => { act(api.purgeMerge(purging.id)); setPurging(null); }}>
              Delete permanently
            </button>
          </>
        }>
          <p>
            Permanently delete <code>{purging.quarantine_path}</code> on <strong>{purging.volume_label}</strong>:
            the {count(purging.counts.covered.files)} files that were already in{" "}
            <code>{where(purging.keep_root)}</code>, and the empty folders. This cannot be undone.
          </p>
        </Modal>
      )}
    </section>
  );
}
