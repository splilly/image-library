import { useEffect, useState } from "react";
import { api, type MergeInfo, type TreeChild, type TreeComparison, type TreePair, type TreeSide } from "../../api";
import { ago, bytes, count, navigate, useInterval, useLoad } from "../../util";
import { Empty } from "../Bits";
import { MergeDialog, MergesPanel } from "./Merges";
import { ActionError } from "./shared";

const SORTS: [string, string][] = [
  ["shared", "Most data in common"],
  ["strength", "Strongest match"],
  ["size", "Largest trees"],
];
const PAGE = 50;

const pct = (n: number, of: number) => (of ? `${Math.round((100 * n) / of)}%` : "–");

/** Folder trees holding the same files: backups of backups, often thinned out by an old de-duplication. */
export function FoldersTab() {
  const [sort, setSort] = useState("shared");
  const [shown, setShown] = useState(PAGE);
  const listing = useLoad(() => api.treePairs(sort, 0), [sort]);
  const [more, setMore] = useState<TreePair[]>([]);
  const waiting = !listing.data || listing.data.building || !listing.data.ready;
  useInterval(() => listing.reload(), 2000, waiting);
  useEffect(() => { setMore([]); setShown(PAGE); }, [sort, listing.data?.built_at]);

  const loadMore = () => {
    api.treePairs(sort, shown).then((page) => {
      setMore((m) => [...m, ...page.items]);
      setShown((n) => n + PAGE);
    });
  };

  const vols = useLoad(() => api.volumes(), []);
  const readOnly = new Set((vols.data ?? []).filter((v) => v.read_only).map((v) => v.id));
  const [prepared, setPrepared] = useState<MergeInfo | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [merged, setMerged] = useState(0);
  const merge = (keep: { volume: string; root: string }, drop: { volume: string; root: string }) =>
    api.prepareMerge(keep, drop).then((m) => { setError(null); setPrepared(m); }).catch(setError);

  const data = listing.data;
  const items = [...(data?.items ?? []), ...more];
  return (
    <div className="page wide">
      <p className="muted">
        Folder trees that hold the same things, reported at the top of each tree. Every file counts, not only
        pictures. <strong>Match</strong> is how much of either tree the other already holds, by content or by
        layout (the same sub-folders and file names), so a backup thinned out by an old de-duplication still shows.
      </p>
      <div className="row">
        <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
          {SORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <span className="muted small">
          {data?.building ? `Comparing folders… ${data.progress}` :
            data?.built_at ? `${count(data.total)} pairs · compared ${ago(data.built_at)}` : "Comparing folders…"}
        </span>
      </div>
      {data?.error && <div className="note note-error">Comparing folders failed: {data.error}</div>}
      <ActionError error={error} />
      <MergesPanel refresh={merged} />
      {data?.ready && data.total === 0 && <Empty>No folder trees hold the same files.</Empty>}
      <div className="pairs">
        {items.map((p) => (
          <PairCard key={`${p.a_volume}/${p.a_root}|${p.b_volume}/${p.b_root}`} pair={p} onMerge={merge}
            readOnly={readOnly.has(p.a_volume) || readOnly.has(p.b_volume)} />
        ))}
      </div>
      {data && items.length < data.total && (
        <button className="btn" onClick={loadMore}>Show more ({count(data.total - items.length)} left)</button>
      )}
      {prepared && (
        <MergeDialog prepared={prepared} fresh onClose={() => { setPrepared(null); setMerged((n) => n + 1); }} />
      )}
    </div>
  );
}

type Place = { volume: string; root: string };

function PairCard({ pair, readOnly, onMerge }: {
  pair: TreePair; readOnly: boolean; onMerge: (keep: Place, drop: Place) => void;
}) {
  const [open, setOpen] = useState(false);
  const left = { volume: pair.a_volume, root: pair.a_root };
  const right = { volume: pair.b_volume, root: pair.b_root };
  const why = readOnly ? "A drive is registered read-only: nothing on it can be moved or copied." : undefined;
  return (
    <div className="pair tree-pair">
      <div className="pair-stat">
        <strong className={pair.strength >= 0.8 ? "num-ok" : pair.strength >= 0.4 ? "" : "muted"}>
          {Math.round(pair.strength * 100)}%
        </strong>
        <span className="muted small">match</span>
      </div>
      <Side label={pair.a_label} volume={pair.a_volume} root={pair.a_root} side={pair.a} />
      <Side label={pair.b_label} volume={pair.b_volume} root={pair.b_root} side={pair.b} />
      <div className="pair-actions tree-facts">
        <Facts c={pair} />
        <span className="row">
          <button className="btn btn-small" onClick={() => setOpen(!open)}>
            {open ? "Hide sub-folders" : "Compare sub-folders"}
          </button>
          <button className="btn btn-small" disabled={!!why || !pair.b_root} title={why}
            onClick={() => onMerge(left, right)}>Merge right into left…</button>
          <button className="btn btn-small" disabled={!!why || !pair.a_root} title={why}
            onClick={() => onMerge(right, left)}>Merge left into right…</button>
        </span>
      </div>
      {open && <Children pair={pair} />}
    </div>
  );
}

function Side({ label, volume, root, side }: { label: string; volume: string; root: string; side: TreeSide }) {
  return (
    <button className="pair-side" title="Open in the library"
      onClick={() => navigate("library", { volume, folder: root || undefined })}>
      <strong>{label}</strong>
      <span className="loc-path">{root || "(whole drive)"}</span>
      <span className="small">{count(side.files)} files · {bytes(side.bytes)}</span>
      <span className="small">
        <strong>{pct(side.in_other, side.files)}</strong> already in the other tree
        <span className="muted"> ({count(side.in_other)} files)</span>
      </span>
      <span className={`small ${side.only ? "" : "muted"}`}>
        {side.only ? `${count(side.only)} files (${bytes(side.only_bytes)}) only here` : "nothing only here"}
      </span>
      {side.unhashed > 0 && <span className="small muted">{count(side.unhashed)} not read yet (compared by name)</span>}
    </button>
  );
}

function Facts({ c }: { c: TreeComparison }) {
  return (
    <span className="small muted">
      {count(c.same_path)} identical at the same path
      {c.conflicts > 0 && <> · <span className="num-warn">{count(c.conflicts)} same path, different content</span></>}
      {" "}· {count(c.shared_folders)} of {count(Math.max(c.a.folders, c.b.folders))} sub-folders in both
    </span>
  );
}

function Children({ pair }: { pair: TreePair }) {
  const kids = useLoad(() => api.treeChildren(pair), [pair.a_volume, pair.a_root, pair.b_volume, pair.b_root]);
  if (!kids.data) return <div className="tree-children muted small">Loading…</div>;
  const direct = (side: "a" | "b") =>
    side === "a" ? pair.a.files - kids.data!.reduce((n, k) => n + k.a.files, 0)
      : pair.b.files - kids.data!.reduce((n, k) => n + k.b.files, 0);
  return (
    <div className="tree-children">
      <table className="table small">
        <thead>
          <tr>
            <th>Left</th><th className="num">files</th><th className="num">in right</th><th className="num">only left</th>
            <th>Right</th><th className="num">files</th><th className="num">in left</th><th className="num">only right</th>
            <th className="num">conflicts</th>
          </tr>
        </thead>
        <tbody>
          {kids.data.map((k: TreeChild) => (
            <tr key={(k.a_name ?? "") + "|" + (k.b_name ?? "")}>
              <td>{k.a_name ?? <span className="muted">—</span>}</td>
              <td className="num">{k.a_name ? count(k.a.files) : ""}</td>
              <td className="num">{k.a_name && k.b_name ? pct(k.a.in_other, k.a.files) : ""}</td>
              <td className="num">{k.a_name ? count(k.a.only) : ""}</td>
              <td>{k.b_name ?? <span className="muted">—</span>}</td>
              <td className="num">{k.b_name ? count(k.b.files) : ""}</td>
              <td className="num">{k.a_name && k.b_name ? pct(k.b.in_other, k.b.files) : ""}</td>
              <td className="num">{k.b_name ? count(k.b.only) : ""}</td>
              <td className={`num ${k.conflicts ? "num-warn" : ""}`}>{k.conflicts ? count(k.conflicts) : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(direct("a") > 0 || direct("b") > 0) && (
        <p className="muted small">
          Plus files directly in the top folders: {count(direct("a"))} on the left, {count(direct("b"))} on the right.
        </p>
      )}
    </div>
  );
}
