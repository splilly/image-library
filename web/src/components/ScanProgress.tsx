import type { TargetProgress } from "../api";
import { bytes, count, duration } from "../util";
import { ProgressBar } from "./Bits";

const STATE_TEXT: Record<string, string> = {
  starting: "Starting",
  discovering: "Finding files",
  analyzing: "Checking files",
  paused: "Paused",
  offline: "Drive disconnected",
  done: "Finished",
  failed: "Failed",
};

export function ScanProgress({ t }: { t: TargetProgress }) {
  const discovering = t.state === "discovering" || t.state === "starting";
  const live = t.state === "analyzing" || discovering;
  return (
    <div className={`scan-card state-${t.state}`}>
      <div className="scan-head">
        <strong>{t.label}</strong>
        <span className="scan-state">{STATE_TEXT[t.state] ?? t.state}</span>
        {live && !discovering && (
          <span className="scan-eta">
            {bytes(t.rate_bps)}/s · {t.eta_s !== null ? `${duration(t.eta_s)} left` : "estimating…"}
          </span>
        )}
      </div>
      <ProgressBar value={t.bytes_done} max={t.bytes_total} indeterminate={discovering} />
      <div className="scan-numbers">
        {discovering ? (
          <span>{count(t.discovered)} media files found so far</span>
        ) : (
          <>
            <span>
              {count(t.files_done)} / {count(t.files_total)} files
            </span>
            <span>
              {bytes(t.bytes_done)} / {bytes(t.bytes_total)}
            </span>
            <span className={t.corrupt ? "num-bad" : ""}>{count(t.corrupt)} corrupt</span>
            <span className={t.warnings ? "num-warn" : ""}>{count(t.warnings)} suspicious</span>
            <span>{count(t.reused)} duplicate copies</span>
            {t.read_errors > 0 && <span className="num-bad">{count(t.read_errors)} read errors</span>}
          </>
        )}
      </div>
      {live && t.current && <div className="scan-current" title={t.current}>{t.current}</div>}
      {t.message && <div className="scan-message">{t.message}</div>}
    </div>
  );
}
