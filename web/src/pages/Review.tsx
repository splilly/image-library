import { api, type ScanState } from "../api";
import { DecisionsTab } from "../components/review/DecisionsTab";
import { DuplicatesTab } from "../components/review/DuplicatesTab";
import { FoldersTab } from "../components/review/FoldersTab";
import { ProblemsTab } from "../components/review/ProblemsTab";
import { SimilarTab } from "../components/review/SimilarTab";
import { ThumbnailsTab } from "../components/review/ThumbnailsTab";
import { bytes, count, navigate, useLoad } from "../util";

const TABS = [
  { id: "problems", label: "Problem files" },
  { id: "duplicates", label: "Duplicates" },
  { id: "folders", label: "Duplicate trees" },
  { id: "thumbnails", label: "Thumbnails" },
  { id: "similar", label: "Similar images" },
  { id: "decisions", label: "Decisions" },
] as const;

export default function Review({ params, scan }: { params: URLSearchParams; scan: ScanState | null }) {
  const tab = params.get("tab") || "problems";
  const summary = useLoad(() => api.decisionSummary(), [tab]);
  const s = summary.data;
  const relate = scan?.relate;
  const relationTab = tab === "thumbnails" || tab === "similar";

  return (
    <div className="review">
      <div className="review-head">
        <nav className="tabs">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => navigate("review", { tab: t.id })}>
              {t.label}
            </button>
          ))}
        </nav>
        {s && (
          <button className="review-tally" onClick={() => navigate("review", { tab: "decisions" })}
            title="Decisions are only recorded. Nothing is deleted until you run an action plan.">
            <strong>{count(s.discard.files)}</strong> to discard ({bytes(s.discard.bytes)}) ·{" "}
            <strong>{count(s.keep.files)}</strong> kept
          </button>
        )}
      </div>
      {relationTab && relate && relate.running && (
        <div className="note banner">
          Finding thumbnails and similar images… {relate.total ? `${Math.round((100 * relate.done) / relate.total)}%` : ""}
          {" "}({count(relate.pairs)} found so far). Results below may be incomplete until this finishes.
        </div>
      )}
      {relationTab && relate && !relate.running && !relate.current && (
        <div className="note banner">
          {scan?.running ? "Thumbnails and similar images are updated when the current scan finishes."
            : <>These results are out of date with the latest scans.{" "}
                <button className="btn btn-small" onClick={() => api.startRelate()}>Update now</button></>}
        </div>
      )}
      <div className="review-body" key={relationTab ? `${tab}-${relate?.current}` : tab}>
        {tab === "duplicates" ? (
          <DuplicatesTab onChanged={summary.reload} />
        ) : tab === "thumbnails" ? (
          <ThumbnailsTab onChanged={summary.reload} />
        ) : tab === "similar" ? (
          <SimilarTab onChanged={summary.reload} />
        ) : tab === "folders" ? (
          <FoldersTab />
        ) : tab === "decisions" ? (
          <DecisionsTab summary={s} onChanged={summary.reload} />
        ) : (
          <ProblemsTab onChanged={summary.reload} />
        )}
      </div>
    </div>
  );
}
