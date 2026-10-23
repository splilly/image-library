import { useCallback, useEffect, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { api, previewUrl, type Decision, type DupGroup, type DupGroupBrief } from "../../api";
import { bytes, count, date } from "../../util";
import { Empty, Modal, OnlineDot } from "../Bits";
import { ActionError, DecisionBadge, KeyHint, useKeys } from "./shared";

const PAGE = 100;

export function DuplicatesTab({ onChanged }: { onChanged: () => void }) {
  const [scope, setScope] = useState("any");
  const [undecided, setUndecided] = useState(true);
  const [items, setItems] = useState<DupGroupBrief[]>([]);
  const [total, setTotal] = useState<{ groups: number; wasted: number } | null>(null);
  const [index, setIndex] = useState(0);
  const [group, setGroup] = useState<DupGroup | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [confirmBulk, setConfirmBulk] = useState(false);
  const list = useRef<VirtuosoHandle>(null);
  const loading = useRef(false);

  const load = useCallback((reset: boolean) => {
    if (loading.current && !reset) return;
    if (!reset && total && items.length >= total.groups) return;
    loading.current = true;
    api.dupGroups({ scope, undecided }, reset ? 0 : items.length, PAGE)
      .then((page) => {
        setItems((prev) => (reset ? page.items : [...prev, ...page.items]));
        setTotal({ groups: page.total, wasted: page.total_wasted });
      })
      .catch((e: Error) => setError(e))
      .finally(() => { loading.current = false; });
  }, [scope, undecided, items.length, total]);

  useEffect(() => { setIndex(0); load(true); }, [scope, undecided]); // eslint-disable-line react-hooks/exhaustive-deps

  const brief = items[index] as DupGroupBrief | undefined;
  const loadGroup = useCallback(() => {
    if (!brief) { setGroup(null); return; }
    api.dupGroup(brief.hash).then(setGroup).catch((e: Error) => setError(e));
  }, [brief?.hash]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(loadGroup, [loadGroup]);

  const move = (delta: number) => {
    const i = Math.max(0, Math.min(items.length - 1, index + delta));
    setIndex(i);
    list.current?.scrollIntoView({ index: i, behavior: "auto" });
    if (i > items.length - 20) load(false);
  };

  const run = (p: Promise<unknown>, advance: boolean) =>
    p.then(() => {
      setError(null);
      onChanged();
      loadGroup();
      if (group) {
        const decided = advance ? group.copies.length : 0;
        setItems((prev) => prev.map((g) => (g.hash === group.hash ? { ...g, decided } : g)));
      }
      if (advance) move(1);
    }).catch((e: Error) => setError(e));

  /** Keep one copy and discard the rest. Keep first, so the last-copy rule is never tripped. */
  const keepOnly = (fileId: number) => {
    if (!group) return;
    const others = group.copies.filter((c) => c.file_id !== fileId).map((c) => c.file_id);
    run(api.decide([fileId], "keep", "duplicate").then(() => api.decide(others, "discard", "duplicate")), true);
  };
  const setOne = (fileId: number, decision: Decision | null) => run(api.decide([fileId], decision, "duplicate"), false);
  const clearGroup = () => group && run(api.decide(group.copies.map((c) => c.file_id), null), false);

  const keys: Record<string, () => void> = {
    ArrowDown: () => move(1), ArrowRight: () => move(1), j: () => move(1), " ": () => move(1),
    ArrowUp: () => move(-1), ArrowLeft: () => move(-1),
    Enter: () => group && keepOnly(group.suggested_keep),
    u: clearGroup,
  };
  for (let n = 1; n <= 9; n++) keys[String(n)] = () => group?.copies[n - 1] && keepOnly(group.copies[n - 1].file_id);
  useKeys(keys);

  const bulk = () => {
    setConfirmBulk(false);
    api.applySuggestions(items.filter((g) => !g.decided).map((g) => g.hash))
      .then(() => { onChanged(); load(true); })
      .catch((e: Error) => setError(e));
  };
  const undecidedShown = items.filter((g) => !g.decided).length;

  return (
    <div className="review-split">
      <div className="review-list">
        <div className="review-filters">
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="any">On any drive</option>
            <option value="same_drive">Repeated on the same drive</option>
          </select>
          <label className="check">
            <input type="checkbox" checked={undecided} onChange={(e) => setUndecided(e.target.checked)} />
            Undecided only
          </label>
        </div>
        {total && (
          <div className="review-count small">
            {count(total.groups)} groups · {bytes(total.wasted)} in extra copies
            {undecidedShown > 0 && (
              <button className="btn btn-small" onClick={() => setConfirmBulk(true)}>
                Accept suggestions for {count(undecidedShown)} listed
              </button>
            )}
          </div>
        )}
        {items.length === 0 && total ? (
          <Empty>{undecided ? "No undecided duplicates." : "No duplicates found."}</Empty>
        ) : (
          <Virtuoso
            ref={list}
            className="review-rows"
            data={items}
            endReached={() => load(false)}
            itemContent={(i, g) => (
              <button className={`review-row ${i === index ? "selected" : ""}`} onClick={() => setIndex(i)}>
                <div className="mini-thumb">{g.has_preview ? <img src={previewUrl(g.hash)} alt="" loading="lazy" /> : <span>?</span>}</div>
                <div className="grow">
                  <div className="row-title">{g.copies} copies · {bytes(g.wasted)} extra</div>
                  <div className="muted small">{g.drives > 1 ? `${g.drives} drives` : "one drive"}{g.taken_at ? ` · ${date(g.taken_at).slice(0, 10)}` : ""}</div>
                </div>
                {g.decided > 0 && <span className="decision decision-keep">Decided</span>}
              </button>
            )}
          />
        )}
      </div>
      <div className="review-viewer">
        <ActionError error={error} />
        {group ? (
          <div className="viewer">
            <div className="viewer-image">
              {group.has_preview ? <img src={previewUrl(group.hash, "m")} alt="" /> : <div className="no-preview">No preview</div>}
            </div>
            <div className="viewer-info">
              <h2>{group.copies.length} identical copies</h2>
              <div className="small muted">
                {group.width} × {group.height} · {group.format} · {bytes(group.size)} each
                {group.taken_at ? ` · taken ${date(group.taken_at)}` : ""}
              </div>
              <p className="muted small">Suggested because: {group.suggestion_reason}.</p>
            </div>
              <ol className="copy-list full">
                {group.copies.map((c, i) => (
                  <li key={c.file_id} className={`${c.file_id === group.suggested_keep ? "suggested" : ""} dec-${c.decision ?? "none"}`}>
                    <kbd>{i + 1}</kbd>
                    <OnlineDot online={c.online} />
                    <div className="grow">
                      <strong>{c.volume_label}</strong>
                      {c.file_id === group.suggested_keep && <span className="tag">suggested keep</span>}
                      <div className="loc-path" title={c.path}>{c.path}</div>
                      <div className="muted small">file date {new Date(c.mtime * 1000).toLocaleDateString()}</div>
                    </div>
                    <DecisionBadge decision={c.decision} />
                    <div className="copy-actions">
                      <button className="btn btn-small" onClick={() => setOne(c.file_id, "keep")}>Keep</button>
                      <button className="btn btn-small" onClick={() => setOne(c.file_id, "discard")}>Discard</button>
                    </div>
                  </li>
                ))}
              </ol>
          </div>
        ) : (
          <Empty>Select a group.</Empty>
        )}
        {group && (
          <div className="review-actions">
            <button className="btn btn-primary" onClick={() => keepOnly(group.suggested_keep)}>Keep suggested, discard others</button>
            <button className="btn btn-ghost" onClick={clearGroup}>Clear</button>
            <span className="grow" />
            <KeyHint k="Enter">accept suggestion</KeyHint>
            <KeyHint k="1–9">keep only that copy</KeyHint>
            <KeyHint k="U">clear</KeyHint>
          </div>
        )}
      </div>
      {confirmBulk && (
        <Modal title="Accept suggestions" onClose={() => setConfirmBulk(false)} footer={
          <>
            <button className="btn" onClick={() => setConfirmBulk(false)}>Cancel</button>
            <button className="btn btn-primary" onClick={bulk}>Accept for {count(undecidedShown)} groups</button>
          </>
        }>
          <p>
            For each of the {count(undecidedShown)} undecided groups listed, keep the suggested copy and mark the
            others to discard. Groups you already decided are left alone.
          </p>
          <p className="muted small">Nothing is deleted now; you can review and undo decisions on the Decisions tab.</p>
        </Modal>
      )}
    </div>
  );
}
