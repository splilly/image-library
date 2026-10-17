import { useState } from "react";
import { api, type ScanState, type Volume } from "../api";
import { Empty, ErrorNote, Modal, OnlineDot } from "../components/Bits";
import { FolderPicker } from "../components/FolderPicker";
import { Icon } from "../components/Icon";
import { ScanProgress } from "../components/ScanProgress";
import { ago, bytes, count, navigate, useLoad } from "../util";

export default function Drives({ scan }: { scan: ScanState | null }) {
  const vols = useLoad(() => api.volumes(true), []);
  const [adding, setAdding] = useState(false);
  const [scanFolderOf, setScanFolderOf] = useState<Volume | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = (p: Promise<unknown>) =>
    p.then(() => { setError(null); vols.reload(); }).catch((e: Error) => setError(e.message));

  return (
    <div className="page">
      <div className="page-head">
        <h1>Drives</h1>
        <div className="row">
          <button className="btn" onClick={() => vols.reload()}>Re-check connections</button>
          <button className="btn btn-primary" onClick={() => setAdding(true)}>
            <Icon name="plus" size={15} /> Add drive
          </button>
        </div>
      </div>
      <ErrorNote error={error ?? vols.error} />
      {vols.data?.length === 0 && (
        <Empty>No drives registered yet. Connect a drive and choose <strong>Add drive</strong>.</Empty>
      )}
      <div className="drive-list">
        {vols.data?.map((v) => (
          <DriveCard key={v.id} v={v} scan={scan}
            onScan={() => act(api.startScan({ volume_ids: [v.id] }))}
            onScanFolder={() => setScanFolderOf(v)}
            onRename={(label) => act(api.editVolume(v.id, { label }))} />
        ))}
      </div>
      {adding && <AddDrive onClose={() => setAdding(false)} onAdded={() => { setAdding(false); vols.reload(); }} />}
      {scanFolderOf && (
        <ScanFolder volume={scanFolderOf} onClose={() => setScanFolderOf(null)}
          onScan={(path) => { setScanFolderOf(null); act(api.startScan({ path })); }} />
      )}
    </div>
  );
}

function DriveCard({ v, scan, onScan, onScanFolder, onRename }: {
  v: Volume; scan: ScanState | null; onScan: () => void; onScanFolder: () => void; onRename: (l: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(v.label);
  const progress = scan?.targets.find((t) => t.volume_id === v.id);
  const scanning = !!scan?.running && !!progress && ["starting", "discovering", "analyzing"].includes(progress.state);
  const st = v.stats;
  return (
    <div className={`drive-card ${v.online ? "" : "offline"}`}>
      <div className="drive-head">
        <OnlineDot online={v.online} />
        {editing ? (
          <form onSubmit={(e) => { e.preventDefault(); setEditing(false); onRename(label); }} className="row">
            <input value={label} onChange={(e) => setLabel(e.target.value)} autoFocus />
            <button className="btn btn-small">Save</button>
          </form>
        ) : (
          <h2 onDoubleClick={() => setEditing(true)} title="Double-click to rename">{v.label}</h2>
        )}
        {v.read_only && (
          <span className="tag" title={`Never written to. Recognised by ${v.fingerprints.join(", ")}`}>
            <Icon name="lock" size={12} /> read-only
          </span>
        )}
      </div>
      <div className="muted small">
        {v.online ? <>Connected at <code>{v.root}</code></> : <>Not connected · last seen {ago(v.last_seen_at)}
          {v.last_seen_path ? <> at <code>{v.last_seen_path}</code></> : null}</>}
      </div>
      <div className="drive-stats">
        <span><strong>{count(st.files)}</strong> files</span>
        <span><strong>{bytes(st.bytes)}</strong></span>
        {st.pending > 0 && <span><strong>{count(st.pending)}</strong> not scanned ({bytes(st.pending_bytes)})</span>}
        {st.read_errors > 0 && <span className="num-bad"><strong>{count(st.read_errors)}</strong> unreadable</span>}
        {st.missing > 0 && <span><strong>{count(st.missing)}</strong> missing</span>}
        {v.total_bytes && <span className="muted">capacity {bytes(v.total_bytes)}</span>}
      </div>
      {progress && (scanning || progress.state !== "done") && <ScanProgress t={progress} />}
      <div className="row">
        <button className="btn" disabled={!v.online || scanning} onClick={onScan}>
          {st.pending ? "Continue scan" : "Scan whole drive"}
        </button>
        <button className="btn" disabled={!v.online || scanning} onClick={onScanFolder}>Scan a folder…</button>
        <button className="btn" onClick={() => navigate("library", { volume: v.id })}>Browse</button>
        <button className="btn btn-ghost" onClick={() => setEditing(true)}>Rename</button>
      </div>
    </div>
  );
}

function AddDrive({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [path, setPath] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    if (!path) return;
    setBusy(true);
    api.addVolume(path, label, readOnly)
      .then(onAdded)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <Modal title="Add a drive" onClose={onClose} footer={
      <>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" disabled={!path || busy} onClick={submit}>
          {busy ? "Adding…" : "Add drive"}
        </button>
      </>
    }>
      <p className="muted">Choose the drive (or the folder that should count as its top level).</p>
      <FolderPicker path={path} onChange={setPath} />
      <label className="field">
        <span>Name</span>
        <input placeholder="e.g. WD 2TB, blue sticker" value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <fieldset className="choice">
        <label>
          <input type="radio" checked={!readOnly} onChange={() => setReadOnly(false)} />
          <div>
            <strong>Write a small ID file to the drive</strong> (recommended)
            <div className="muted small">
              Adds <code>.imagelib-volume.json</code> at the top of the drive so it is always recognised,
              on any computer or operating system. Nothing else is written.
            </div>
          </div>
        </label>
        <label>
          <input type="radio" checked={readOnly} onChange={() => setReadOnly(true)} />
          <div>
            <strong>Never write to this drive</strong>
            <div className="muted small">
              Recognised by its serial number and capacity instead. Reformatting it changes that identity,
              and a cloned drive would look the same. The whole drive is registered, even if you picked a folder.
            </div>
          </div>
        </label>
      </fieldset>
      <ErrorNote error={error} />
    </Modal>
  );
}

function ScanFolder({ volume, onClose, onScan }: { volume: Volume; onClose: () => void; onScan: (p: string) => void }) {
  const [path, setPath] = useState<string | null>(volume.root);
  return (
    <Modal title={`Scan a folder on ${volume.label}`} onClose={onClose} footer={
      <>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" disabled={!path} onClick={() => path && onScan(path)}>Scan this folder</button>
      </>
    }>
      <FolderPicker path={path} onChange={setPath} />
    </Modal>
  );
}
