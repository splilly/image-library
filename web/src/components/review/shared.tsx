import { useEffect, useRef } from "react";
import { ApiError, type Decision } from "../../api";

export function DecisionBadge({ decision }: { decision: Decision | null | undefined }) {
  if (!decision) return <span className="decision decision-none">Undecided</span>;
  return <span className={`decision decision-${decision}`}>{decision === "keep" ? "Keep" : "Discard"}</span>;
}

/** Error box that also lists the images a refused decision would have left without any copy. */
export function ActionError({ error }: { error: ApiError | Error | null }) {
  if (!error) return null;
  const refused = (error as ApiError).refused;
  return (
    <div className="note note-error">
      {error.message}
      {refused && refused.length > 0 && (
        <ul>
          {refused.slice(0, 5).map((r) => (
            <li key={r.hash}>{r.paths.join("  ·  ")}</li>
          ))}
          {refused.length > 5 && <li>…and {refused.length - 5} more</li>}
        </ul>
      )}
    </div>
  );
}

/** Keyboard shortcuts for a review screen; ignored while typing in a field. */
export function useKeys(handlers: Record<string, (e: KeyboardEvent) => void>) {
  const ref = useRef(handlers);
  ref.current = handlers;
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (document.querySelector(".modal-backdrop")) return;
      const fn = ref.current[e.key] ?? ref.current[e.key.toLowerCase()];
      if (fn) {
        e.preventDefault();
        fn(e);
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
}

export function KeyHint({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <span className="keyhint">
      <kbd>{k}</kbd> {children}
    </span>
  );
}

/** Keep the selected row of a list in view. */
export function scrollIntoViewIfNeeded(container: HTMLElement | null, index: number) {
  const el = container?.querySelector(`[data-index="${index}"]`) as HTMLElement | null;
  el?.scrollIntoView({ block: "nearest" });
}
