import { useEffect, useState } from "react";
import { api } from "../api";
import { useLoad } from "../util";
import { ErrorNote } from "./Bits";
import { Icon } from "./Icon";

/** Browse folders on this computer (server side; browsers cannot reveal real paths). */
export function FolderPicker({ path, onChange }: { path: string | null; onChange: (path: string | null) => void }) {
  const { data, error, loading } = useLoad(() => api.fs(path ?? undefined), [path]);
  const [typed, setTyped] = useState(path ?? "");
  useEffect(() => setTyped(path ?? ""), [path]);
  return (
    <div className="picker">
      <div className="picker-bar">
        <button className="btn btn-small" disabled={!path} onClick={() => onChange(data?.parent ?? null)}>
          <Icon name="up" size={14} /> Up
        </button>
        <button className="btn btn-small" disabled={!path} onClick={() => onChange(null)}>
          Drives
        </button>
        <form className="picker-path" onSubmit={(e) => { e.preventDefault(); onChange(typed.trim() || null); }}>
          <input value={typed} placeholder="Drives and mount points — or type / paste a path and press Enter"
            onChange={(e) => setTyped(e.target.value)} aria-label="Folder path" />
        </form>
      </div>
      {data?.volume && (
        <div className="note">
          This is on the registered drive <strong>{data.volume.label}</strong> (root {data.volume.root}).
        </div>
      )}
      <ErrorNote error={error} />
      <ul className="picker-list">
        {loading && !data && <li className="muted">Loading…</li>}
        {data?.dirs.map((d) => (
          <li key={d.path}>
            <button onClick={() => onChange(d.path)}>
              <Icon name={path ? "folder" : "drive"} size={16} /> {d.name}
            </button>
          </li>
        ))}
        {data && data.dirs.length === 0 && <li className="muted">No sub-folders</li>}
      </ul>
    </div>
  );
}
