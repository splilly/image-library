import { useEffect, useState } from "react";
import { api, previewUrl, type Decision, type DecisionItem, type DecisionSummary } from "../../api";
import { bytes, count, fileName } from "../../util";
import { Empty } from "../Bits";
import { ActionError, DecisionBadge } from "./shared";

const REASON_LABEL: Record<string, string> = {
  problem: "Problem files",
  duplicate: "Duplicates",
  folder: "Duplicate folders",
  manual: "Chosen by hand",
};

export function DecisionsTab({ summary, onChanged }: { summary: DecisionSummary | null; onChanged: () => void }) {
  const [which, setWhich] = useState<Decision>("discard");
  const [items, setItems] = useState<DecisionItem[]>([]);
  const [next, setNext] = useState<number | null>(0);
  const [error, setError] = useState<Error | null>(null);

  const load = (reset: boolean) => {
    const after = reset ? 0 : next;
    if (after === null) return;
    api.decisions(which, after)
      .then((page) => { setItems((prev) => (reset ? page.items : [...prev, ...page.items])); setNext(page.next); })
      .catch((e: Error) => setError(e));
  };
  useEffect(() => load(true), [which]); // eslint-disable-line react-hooks/exhaustive-deps

  const undo = (it: DecisionItem) =>
    api.decide([it.file_id], null)
      .then(() => { setItems((prev) => prev.filter((x) => x.file_id !== it.file_id)); onChanged(); })
      .catch((e: Error) => setError(e));

  return (
    <div className="page wide">
      <div className="note">
        Decisions are only recorded. <strong>Nothing has been deleted or moved.</strong> In a later step they become
        an action plan that moves discarded files into a quarantine folder on each drive, where they can still be
        restored.
      </div>
      {summary && (
        <div className="tiles">
          <div className="tile"><div className="tile-value">{count(summary.discard.files)}</div>
            <div className="tile-label">to discard</div><div className="tile-sub">{bytes(summary.discard.bytes)}</div></div>
          <div className="tile"><div className="tile-value">{count(summary.keep.files)}</div>
            <div className="tile-label">marked keep</div></div>
          {summary.by_reason.filter((r) => r.decision === "discard").map((r) => (
            <div key={r.reason} className="tile"><div className="tile-value">{count(r.files)}</div>
              <div className="tile-label">{REASON_LABEL[r.reason] ?? r.reason}</div>
              <div className="tile-sub">{bytes(r.bytes)}</div></div>
          ))}
        </div>
      )}
      {summary && summary.stale > 0 && (
        <div className="note note-warn">
          {count(summary.stale)} earlier decisions no longer apply because those files changed since; review them again.
        </div>
      )}
      <ActionError error={error} />
      <div className="row">
        <button className={`btn ${which === "discard" ? "btn-primary" : ""}`} onClick={() => setWhich("discard")}>To discard</button>
        <button className={`btn ${which === "keep" ? "btn-primary" : ""}`} onClick={() => setWhich("keep")}>Kept</button>
      </div>
      {items.length === 0 && next === null && <Empty>No decisions yet.</Empty>}
      <table className="table decisions">
        <tbody>
          {items.map((it) => (
            <tr key={it.file_id}>
              <td className="mini-thumb">{it.hash && it.has_preview ? <img src={previewUrl(it.hash)} alt="" loading="lazy" /> : null}</td>
              <td>
                <div>{fileName(it.path)}</div>
                <div className="loc-path small">{it.volume_label}: {it.path}</div>
              </td>
              <td className="small muted">{REASON_LABEL[it.reason] ?? it.reason}</td>
              <td className="num">{bytes(it.size)}</td>
              <td><DecisionBadge decision={it.decision} /></td>
              <td><button className="btn btn-small" onClick={() => undo(it)}>Undo</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {next !== null && items.length > 0 && <button className="btn" onClick={() => load(false)}>Show more</button>}
    </div>
  );
}
