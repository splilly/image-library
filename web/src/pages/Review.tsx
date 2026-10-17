import { api } from "../api";
import { DecisionsTab } from "../components/review/DecisionsTab";
import { DuplicatesTab } from "../components/review/DuplicatesTab";
import { FoldersTab } from "../components/review/FoldersTab";
import { ProblemsTab } from "../components/review/ProblemsTab";
import { bytes, count, navigate, useLoad } from "../util";

const TABS = [
  { id: "problems", label: "Problem files" },
  { id: "duplicates", label: "Duplicates" },
  { id: "folders", label: "Duplicate folders" },
  { id: "decisions", label: "Decisions" },
] as const;

export default function Review({ params }: { params: URLSearchParams }) {
  const tab = params.get("tab") || "problems";
  const summary = useLoad(() => api.decisionSummary(), [tab]);
  const s = summary.data;

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
      <div className="review-body">
        {tab === "duplicates" ? (
          <DuplicatesTab onChanged={summary.reload} />
        ) : tab === "folders" ? (
          <FoldersTab onChanged={summary.reload} />
        ) : tab === "decisions" ? (
          <DecisionsTab summary={s} onChanged={summary.reload} />
        ) : (
          <ProblemsTab onChanged={summary.reload} />
        )}
      </div>
    </div>
  );
}
