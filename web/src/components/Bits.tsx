import type { ReactNode } from "react";
import { STATUS_LABEL } from "../util";
import { Icon } from "./Icon";

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return null;
  return <span className={`badge badge-${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export function OnlineDot({ online, title }: { online: boolean; title?: string }) {
  return <span className={`dot ${online ? "dot-on" : "dot-off"}`} title={title ?? (online ? "Connected" : "Not connected")} />;
}

export function ProgressBar({ value, max, indeterminate }: { value: number; max: number; indeterminate?: boolean }) {
  const pct = max > 0 ? Math.min(100, (100 * value) / max) : 0;
  return (
    <div className={`progress ${indeterminate ? "indeterminate" : ""}`} role="progressbar"
      aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <div style={{ width: indeterminate ? undefined : `${pct}%` }} />
    </div>
  );
}

export function Modal({ title, onClose, children, footer }: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label={title}>
        <header>
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null | undefined }) {
  return error ? <div className="note note-error">{error}</div> : null;
}

export function CopyButton({ text, label = "Copy path" }: { text: string; label?: string }) {
  return (
    <button className="icon-btn" title={label} aria-label={label}
      onClick={() => navigator.clipboard?.writeText(text)}>
      <Icon name="copy" size={15} />
    </button>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
