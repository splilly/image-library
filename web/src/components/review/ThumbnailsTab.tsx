import { useCallback, useEffect, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { api, previewUrl, type Decision, type ThumbDetail, type ThumbItem } from "../../api";
import { bytes, count } from "../../util";
import { Empty, Modal, OnlineDot } from "../Bits";
import { ActionError, DecisionBadge, KeyHint, useKeys } from "./shared";

const CLASS_LABEL: Record<string, string> = {
  original_ok: "Original found",
  original_damaged: "Original damaged",
  original_uncertain: "Possible original — not certain",
  orphan: "No original — only copy",
};
const CLASS_HELP: Record<string, string> = {
  original_ok: "A larger, intact version of this picture exists and is not marked to discard. This copy can go.",
  original_damaged: "Larger versions were found, but they are damaged or all marked to discard. This copy may be the best one left.",
  original_uncertain: "A larger image looks very much like this one but differs in places — often the next frame of a burst, not the original. Compare them yourself; it is never discarded automatically.",
  orphan: "No larger version was found anywhere in the catalog. This may be the only surviving copy of the picture — keep it.",
};
const PAGE = 100;

export function ThumbnailsTab({ onChanged }: { onChanged: () => void }) {
  const [cls, setCls] = useState("");
  const [kind, setKind] = useState("thumbnail");
  const [undecided, setUndecided] = useState(true);
  const [items, setItems] = useState<ThumbItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [more, setMore] = useState(true);
  const [index, setIndex] = useState(0);
  const [detail, setDetail] = useState<ThumbDetail | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [confirm, setConfirm] = useState(false);
  const list = useRef<VirtuosoHandle>(null);
  const loading = useRef(false);

  const load = useCallback((reset: boolean) => {
    if (loading.current && !reset) return;
    if (!reset && !more) return;
    loading.current = true;
    api.thumbnails({ cls: cls || undefined, kind: kind || undefined, undecided }, reset ? 0 : items.length, PAGE)
      .then((page) => {
        setItems((prev) => (reset ? page.items : [...prev, ...page.items]));
        setCounts(page.counts);
        setMore(page.items.length === PAGE);
      })
      .catch((e: Error) => setError(e))
      .finally(() => { loading.current = false; });
  }, [cls, kind, undecided, items.length, more]);
  useEffect(() => { setIndex(0); load(true); }, [cls, kind, undecided]); // eslint-disable-line react-hooks/exhaustive-deps

  const item = items[index] as ThumbItem | undefined;
  const loadDetail = useCallback(() => {
    if (!item) { setDetail(null); return; }
    api.thumbnail(item.hash).then(setDetail).catch((e: Error) => setError(e));
  }, [item?.hash]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(loadDetail, [loadDetail]);

  const move = (delta: number) => {
    const i = Math.max(0, Math.min(items.length - 1, index + delta));
    setIndex(i);
    list.current?.scrollIntoView({ index: i, behavior: "auto" });
    if (i > items.length - 20) load(false);
  };
  const decide = (decision: Decision | null) => {
    if (!detail) return;
    api.decide(detail.locations.map((l) => l.file_id), decision, "thumbnail")
      .then(() => {
        setError(null);
        onChanged();
        setItems((prev) => prev.map((x) => (x.hash === detail.hash ? { ...x, decided: !!decision } : x)));
        loadDetail();
        if (decision) move(1);
      })
      .catch((e: Error) => setError(e));
  };
  useKeys({
    ArrowDown: () => move(1), ArrowRight: () => move(1), j: () => move(1), " ": () => move(1),
    ArrowUp: () => move(-1), ArrowLeft: () => move(-1),
    d: () => decide("discard"), k: () => decide("keep"), u: () => decide(null),
  });

  const verified = (counts[`original_ok/${kind || "thumbnail"}`] ?? 0) + (kind ? 0 : counts["original_ok/reduced"] ?? 0);
  const bulk = () => {
    setConfirm(false);
    api.discardVerifiedThumbnails(kind)
      .then(() => { onChanged(); load(true); })
      .catch((e: Error) => setError(e));
  };
  const n = (c: string) => count((counts[`${c}/thumbnail`] ?? 0) * (kind !== "reduced" ? 1 : 0) +
    (counts[`${c}/reduced`] ?? 0) * (kind !== "thumbnail" ? 1 : 0));

  return (
    <div className="review-split">
      <div className="review-list">
        <div className="review-filters">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="thumbnail">Thumbnails</option>
            <option value="reduced">Reduced copies (web exports)</option>
            <option value="">Both</option>
          </select>
          <select value={cls} onChange={(e) => setCls(e.target.value)}>
            <option value="">Any status</option>
            {Object.entries(CLASS_LABEL).map(([v, l]) => <option key={v} value={v}>{l} ({n(v)})</option>)}
          </select>
          <label className="check">
            <input type="checkbox" checked={undecided} onChange={(e) => setUndecided(e.target.checked)} />
            Undecided only
          </label>
        </div>
        {undecided && verified > 0 && (
          <div className="review-count small">
            {count(verified)} with an intact original
            <button className="btn btn-small" onClick={() => setConfirm(true)}>Discard all of these…</button>
          </div>
        )}
        {items.length === 0 && !more ? (
          <Empty>Nothing here.</Empty>
        ) : (
          <Virtuoso ref={list} className="review-rows" data={items} endReached={() => load(false)}
            itemContent={(i, t) => (
              <button className={`review-row ${i === index ? "selected" : ""}`} onClick={() => setIndex(i)}>
                <div className="mini-thumb">{t.has_preview ? <img src={previewUrl(t.hash)} alt="" loading="lazy" /> : null}</div>
                <div className="grow">
                  <div className="row-title">{t.width} × {t.height}</div>
                  <div className={`small class-${t.class}`}>{CLASS_LABEL[t.class]}</div>
                </div>
                {t.decided && <span className="decision decision-keep">Decided</span>}
              </button>
            )} />
        )}
      </div>
      <div className="review-viewer">
        <ActionError error={error} />
        {detail ? (
          <div className="viewer compare">
            <figure>
              <figcaption>This {detail.kind === "thumbnail" ? "thumbnail" : "reduced copy"} · {detail.width} × {detail.height} · {bytes(detail.size)}</figcaption>
              <div className="viewer-image small-img">
                {detail.has_preview ? <img src={previewUrl(detail.hash, "m")} alt="" /> : null}
              </div>
            </figure>
            <figure>
              <figcaption>
                {detail.originals[0]
                  ? <>{detail.originals[0].certain ? "Original" : "Possible original (not certain)"} · {detail.originals[0].width} × {detail.originals[0].height} · {bytes(detail.originals[0].size)}
                      {detail.originals[0].status !== "ok" && <span className="badge badge-corrupt"> {detail.originals[0].status}</span>}</>
                  : "No original found"}
              </figcaption>
              <div className="viewer-image">
                {detail.originals[0]?.has_preview ? <img src={previewUrl(detail.originals[0].hash, "m")} alt="" />
                  : <div className="no-preview">—</div>}
              </div>
            </figure>
            <div className="viewer-info full">
              <div className={`note class-note-${detail.class}`}>
                <strong>{CLASS_LABEL[detail.class]}.</strong> {CLASS_HELP[detail.class]}
                {detail.reason && <div className="small muted">Looks like a generated thumbnail: {detail.reason}.</div>}
              </div>
              <h3>This {detail.kind === "thumbnail" ? "thumbnail" : "copy"} ({detail.locations.length})</h3>
              <Locations locs={detail.locations} />
              {detail.originals.map((o) => (
                <div key={o.hash}>
                  <h3>{o.certain ? "Original" : "Possible original"} {o.width} × {o.height} — {o.locations.length} {o.locations.length === 1 ? "copy" : "copies"}</h3>
                  <Locations locs={o.locations} />
                </div>
              ))}
            </div>
          </div>
        ) : (
          <Empty>{item ? "Loading…" : "Select an image."}</Empty>
        )}
        {detail && (
          <div className="review-actions">
            <button className="btn btn-danger" onClick={() => decide("discard")}>Discard this {detail.kind === "thumbnail" ? "thumbnail" : "copy"}</button>
            <button className="btn" onClick={() => decide("keep")}>Keep</button>
            <button className="btn btn-ghost" onClick={() => decide(null)}>Undo</button>
            <span className="grow" />
            <KeyHint k="D">discard</KeyHint>
            <KeyHint k="K">keep</KeyHint>
            <KeyHint k="U">undo</KeyHint>
          </div>
        )}
      </div>
      {confirm && (
        <Modal title="Discard thumbnails whose original is intact" onClose={() => setConfirm(false)} footer={
          <>
            <button className="btn" onClick={() => setConfirm(false)}>Cancel</button>
            <button className="btn btn-danger" onClick={bulk}>Mark {count(verified)} to discard</button>
          </>
        }>
          <p>
            Every undecided {kind === "reduced" ? "reduced copy" : kind ? "thumbnail" : "thumbnail and reduced copy"} that
            has a larger, intact original which is not marked to discard will be marked to discard.
          </p>
          <p>Orphans (no original found) and those whose original is damaged are left alone.</p>
          <p className="muted small">Nothing is deleted now; undo any time on the Decisions tab.</p>
        </Modal>
      )}
    </div>
  );
}

function Locations({ locs }: { locs: { file_id: number; volume_label: string; path: string; online: boolean; decision: Decision | null }[] }) {
  return (
    <ul className="locations">
      {locs.map((l) => (
        <li key={l.file_id}>
          <OnlineDot online={l.online} />
          <div>
            <strong>{l.volume_label}</strong> <DecisionBadge decision={l.decision} />
            <div className="loc-path">{l.path}</div>
          </div>
        </li>
      ))}
    </ul>
  );
}
