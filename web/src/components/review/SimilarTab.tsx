import { useCallback, useEffect, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { api, previewUrl, type Cluster, type ClusterBrief } from "../../api";
import { bytes, count, date } from "../../util";
import { Empty, OnlineDot } from "../Bits";
import { ActionError, DecisionBadge, KeyHint, useKeys } from "./shared";

const KIND_LABEL = { same_pixels: "Same picture, different metadata", near: "Near-identical (re-saved or edited)" };
const PAGE = 100;

export function SimilarTab({ onChanged }: { onChanged: () => void }) {
  const [undecided, setUndecided] = useState(true);
  const [items, setItems] = useState<ClusterBrief[]>([]);
  const [total, setTotal] = useState<{ n: number; extra: number } | null>(null);
  const [index, setIndex] = useState(0);
  const [cluster, setCluster] = useState<Cluster | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const list = useRef<VirtuosoHandle>(null);
  const loading = useRef(false);

  const load = useCallback((reset: boolean) => {
    if (loading.current && !reset) return;
    if (!reset && total && items.length >= total.n) return;
    loading.current = true;
    api.similar(undecided, reset ? 0 : items.length, PAGE)
      .then((page) => {
        setItems((prev) => (reset ? page.items : [...prev, ...page.items]));
        setTotal({ n: page.total, extra: page.total_extra });
      })
      .catch((e: Error) => setError(e))
      .finally(() => { loading.current = false; });
  }, [undecided, items.length, total]);
  useEffect(() => { setIndex(0); load(true); }, [undecided]); // eslint-disable-line react-hooks/exhaustive-deps

  const brief = items[index] as ClusterBrief | undefined;
  const loadCluster = useCallback(() => {
    if (!brief) { setCluster(null); return; }
    api.cluster(brief.cluster).then(setCluster).catch((e: Error) => setError(e));
  }, [brief?.cluster]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(loadCluster, [loadCluster]);

  const move = (delta: number) => {
    const i = Math.max(0, Math.min(items.length - 1, index + delta));
    setIndex(i);
    list.current?.scrollIntoView({ index: i, behavior: "auto" });
    if (i > items.length - 20) load(false);
  };
  const keep = (hash: string) => {
    if (!cluster) return;
    api.keepClusterMember(cluster.cluster, hash)
      .then(() => { setError(null); onChanged(); loadCluster(); move(1); })
      .catch((e: Error) => setError(e));
  };
  const clear = () => cluster && api.decide(cluster.members.flatMap((m) => m.locations.map((l) => l.file_id)), null)
    .then(() => { onChanged(); loadCluster(); }).catch((e: Error) => setError(e));

  const keys: Record<string, () => void> = {
    ArrowDown: () => move(1), ArrowRight: () => move(1), j: () => move(1), " ": () => move(1),
    ArrowUp: () => move(-1), ArrowLeft: () => move(-1),
    Enter: () => cluster && keep(cluster.suggested_keep), u: () => { clear(); },
  };
  for (let n = 1; n <= 9; n++) keys[String(n)] = () => cluster?.members[n - 1] && keep(cluster.members[n - 1].hash);
  useKeys(keys);

  return (
    <div className="review-split">
      <div className="review-list">
        <div className="review-filters">
          <label className="check">
            <input type="checkbox" checked={undecided} onChange={(e) => setUndecided(e.target.checked)} />
            Undecided only
          </label>
        </div>
        {total && <div className="review-count small">{count(total.n)} groups · {bytes(total.extra)} in extra versions</div>}
        {items.length === 0 && total ? (
          <Empty>No similar images{undecided ? " left to decide" : ""}.</Empty>
        ) : (
          <Virtuoso ref={list} className="review-rows" data={items} endReached={() => load(false)}
            itemContent={(i, c) => (
              <button className={`review-row ${i === index ? "selected" : ""}`} onClick={() => setIndex(i)}>
                <div className="mini-thumb">{c.sample?.has_preview ? <img src={previewUrl(c.sample.hash)} alt="" loading="lazy" /> : null}</div>
                <div className="grow">
                  <div className="row-title">{c.members} versions · {bytes(c.extra_bytes)} extra</div>
                  <div className="muted small">{KIND_LABEL[c.kind]}</div>
                </div>
              </button>
            )} />
        )}
      </div>
      <div className="review-viewer">
        <ActionError error={error} />
        {cluster && cluster.members.length > 0 ? (
          <div className="members">
            {cluster.members.map((m, i) => {
              const suggested = m.hash === cluster.suggested_keep;
              return (
                <div key={m.hash} className={`member ${suggested ? "suggested" : ""}`}>
                  <div className="viewer-image">{m.has_preview ? <img src={previewUrl(m.hash, "m")} alt="" /> : null}</div>
                  <div className="row">
                    <kbd>{i + 1}</kbd>
                    <strong>{m.width} × {m.height}</strong> · {m.format} · {bytes(m.size)}
                    {suggested && <span className="tag">suggested keep</span>}
                  </div>
                  <div className="small muted">
                    {m.has_exif ? "Has camera metadata" : "No camera metadata"}
                    {m.camera ? ` · ${m.camera}` : ""}{m.taken_at ? ` · ${date(m.taken_at)}` : ""}
                    {m.software ? ` · edited with ${m.software}` : ""}
                  </div>
                  <ul className="locations">
                    {m.locations.map((l) => (
                      <li key={l.file_id}>
                        <OnlineDot online={l.online} />
                        <div><strong>{l.volume_label}</strong> <DecisionBadge decision={l.decision} />
                          <div className="loc-path">{l.path}</div></div>
                      </li>
                    ))}
                  </ul>
                  <button className="btn btn-small" onClick={() => keep(m.hash)}>Keep this one, discard the others</button>
                </div>
              );
            })}
            <p className="muted small full">Suggested: {cluster.suggestion_reason}.</p>
          </div>
        ) : (
          <Empty>Select a group.</Empty>
        )}
        {cluster && (
          <div className="review-actions">
            <button className="btn btn-primary" onClick={() => keep(cluster.suggested_keep)}>Keep suggested, discard others</button>
            <button className="btn btn-ghost" onClick={clear}>Clear</button>
            <span className="grow" />
            <KeyHint k="Enter">accept suggestion</KeyHint>
            <KeyHint k="1–9">keep that version</KeyHint>
            <KeyHint k="U">clear</KeyHint>
          </div>
        )}
      </div>
    </div>
  );
}
