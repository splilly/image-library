import { useCallback, useEffect, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { api, originalUrl, previewUrl, type Decision, type ProblemItem } from "../../api";
import { bytes, fileName, STATUS_LABEL, useLoad } from "../../util";
import { Empty, StatusBadge } from "../Bits";
import { ActionError, DecisionBadge, KeyHint, useKeys } from "./shared";

const FILTERS: [string, string][] = [
  ["", "All problems"],
  ["corrupt", "Corrupt"],
  ["warning", "Suspicious"],
  ["unsupported", "Unsupported"],
  ["read_error", "Unreadable (disk error)"],
];
const BROWSER_EXT = /\.(jpe?g|jpe|jfif|png|gif|webp|bmp)$/i;

export function ProblemsTab({ onChanged }: { onChanged: () => void }) {
  const [status, setStatus] = useState("");
  const [undecided, setUndecided] = useState(true);
  const [items, setItems] = useState<ProblemItem[]>([]);
  const [next, setNext] = useState<number | null>(0);
  const [index, setIndex] = useState(0);
  const [error, setError] = useState<Error | null>(null);
  const list = useRef<VirtuosoHandle>(null);
  const loading = useRef(false);
  const gen = useRef(0);

  const load = useCallback((reset: boolean) => {
    if (loading.current && !reset) return;
    const after = reset ? 0 : next;
    if (after === null) return;
    const mine = reset ? ++gen.current : gen.current;
    loading.current = true;
    api.problems({ status: status || undefined, undecided }, after)
      .then((page) => {
        if (mine !== gen.current) return;
        setItems((prev) => (reset ? page.items : [...prev, ...page.items]));
        setNext(page.next);
      })
      .catch((e: Error) => setError(e))
      .finally(() => { if (mine === gen.current) loading.current = false; });
  }, [status, undecided, next]);

  useEffect(() => { setIndex(0); load(true); }, [status, undecided]); // eslint-disable-line react-hooks/exhaustive-deps

  const item = items[index] as ProblemItem | undefined;
  const move = (delta: number) => {
    const i = Math.max(0, Math.min(items.length - 1, index + delta));
    setIndex(i);
    list.current?.scrollIntoView({ index: i, behavior: "auto" });
    if (i > items.length - 20) load(false);
  };

  const decide = (decision: Decision | null) => {
    if (!item) return;
    api.decide([item.file_id], decision, "problem")
      .then(() => {
        setError(null);
        setItems((prev) => prev.map((x) => (x.file_id === item.file_id ? { ...x, decision } : x)));
        onChanged();
        if (decision) move(1);
      })
      .catch((e: Error) => setError(e));
  };

  useKeys({
    ArrowDown: () => move(1), ArrowRight: () => move(1), j: () => move(1), " ": () => move(1),
    ArrowUp: () => move(-1), ArrowLeft: () => move(-1),
    d: () => decide("discard"), k: () => decide("keep"), u: () => decide(null),
  });

  return (
    <div className="review-split">
      <div className="review-list">
        <div className="review-filters">
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {FILTERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <label className="check">
            <input type="checkbox" checked={undecided} onChange={(e) => setUndecided(e.target.checked)} />
            Undecided only
          </label>
        </div>
        {items.length === 0 && next === null ? (
          <Empty>{undecided ? "Nothing left to review here." : "No problem files found."}</Empty>
        ) : (
          <Virtuoso
            ref={list}
            className="review-rows"
            data={items}
            endReached={() => load(false)}
            itemContent={(i, it) => (
              <button className={`review-row ${i === index ? "selected" : ""}`} onClick={() => setIndex(i)}>
                <div className="mini-thumb">
                  {it.hash && it.has_preview ? <img src={previewUrl(it.hash)} alt="" loading="lazy" /> : <span>{it.format ?? "?"}</span>}
                </div>
                <div className="grow">
                  <div className="row-title">{fileName(it.path)}</div>
                  <div className="muted small">{it.volume_label} · {STATUS_LABEL[it.status] ?? it.status}</div>
                </div>
                {it.decision && <DecisionBadge decision={it.decision} />}
              </button>
            )}
          />
        )}
      </div>
      <div className="review-viewer">
        <ActionError error={error} />
        {item ? <ProblemViewer item={item} /> : <Empty>Select a file.</Empty>}
        {item && (
          <div className="review-actions">
            <button className="btn btn-danger" onClick={() => decide("discard")}>Discard</button>
            <button className="btn" onClick={() => decide("keep")}>Keep</button>
            <button className="btn btn-ghost" disabled={!item.decision} onClick={() => decide(null)}>Undo</button>
            <span className="grow" />
            <KeyHint k="D">discard</KeyHint>
            <KeyHint k="K">keep</KeyHint>
            <KeyHint k="U">undo</KeyHint>
            <KeyHint k="↑↓">move</KeyHint>
          </div>
        )}
      </div>
    </div>
  );
}

function ProblemViewer({ item }: { item: ProblemItem }) {
  const detail = useLoad(() => (item.hash ? api.content(item.hash) : Promise.resolve(null)), [item.hash]);
  const intact = useLoad(() => (item.hash ? api.intact(item.hash) : Promise.resolve([])), [item.hash]);
  const vol = detail.data?.locations.find((l) => l.file_id === item.file_id);
  const otherCopies = detail.data?.locations.filter((l) => l.file_id !== item.file_id) ?? [];

  return (
    <div className="viewer">
      <div className="viewer-image">
        {item.hash && item.has_preview ? <img src={previewUrl(item.hash, "m")} alt="" /> :
          <div className="no-preview">No picture could be recovered from this file</div>}
      </div>
      <div className="viewer-info">
        <div className="row">
          <h2 title={item.path}>{fileName(item.path)}</h2>
          <StatusBadge status={item.status} />
          <DecisionBadge decision={item.decision} />
        </div>
        <div className="muted small loc-path">{item.volume_label}: {item.path}</div>
        <div className="small">
          {item.width ? `${item.width} × ${item.height} · ` : ""}{item.format ?? ""} · {bytes(item.size)}
          {vol?.online && BROWSER_EXT.test(item.path) && (
            <> · <a href={originalUrl(item.file_id)} target="_blank" rel="noreferrer">open original</a></>
          )}
        </div>
        {item.problems && (
          <ul className="problems">
            {item.problems.split("; ").map((p) => <li key={p}>{p}</li>)}
          </ul>
        )}
        {otherCopies.length > 0 && (
          <p className="small">
            The same damaged file also exists at: {otherCopies.map((l) => `${l.volume_label}: ${l.path}`).join(", ")}
          </p>
        )}
        <h3>Intact versions elsewhere</h3>
        {intact.loading && <p className="muted small">Looking…</p>}
        {intact.data && intact.data.length === 0 && (
          <p className="muted small">None found. This may be the only copy — keep it if the damage is minor.</p>
        )}
        <div className="intact-list">
          {intact.data?.map((v) => (
            <div key={v.hash} className="intact">
              {v.has_preview && <img src={previewUrl(v.hash)} alt="" />}
              <div>
                <strong>{v.why.join(", ")}</strong>
                <div className="small">{v.width} × {v.height} · {bytes(v.size)}</div>
                {v.locations.map((l) => (
                  <div key={l.file_id} className="loc-path small">{l.volume_label}: {l.path}</div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
