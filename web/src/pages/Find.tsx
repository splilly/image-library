import { useState } from "react";
import { api, previewUrl, type Volume, type WhereMatch } from "../api";
import { CopyButton, ErrorNote, OnlineDot, StatusBadge } from "../components/Bits";
import { Icon } from "../components/Icon";
import { bytes, date, osPath, useLoad } from "../util";

/** "Which drive is this photo on?" Drop any image; exact copies and look-alikes are listed. */
export default function Find() {
  const [file, setFile] = useState<File | null>(null);
  const [localPreview, setLocalPreview] = useState<string | null>(null);
  const [matches, setMatches] = useState<WhereMatch[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const vols = useLoad(() => api.volumes(), []);
  const volumes = new Map((vols.data ?? []).map((v) => [v.id, v] as [string, Volume]));

  const search = (f: File) => {
    setFile(f);
    setMatches(null);
    setError(null);
    setLocalPreview(f.type.startsWith("image/") ? URL.createObjectURL(f) : null);
    setBusy(true);
    api.where(f)
      .then((r) => setMatches(r.matches))
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="page">
      <h1>Find an image</h1>
      <p className="muted">
        Drop a photo (from your desktop, an email, a phone) to see which drives hold it — identical copies and
        resized or edited versions. Works for drives that are not connected, too.
      </p>
      <label
        className={`dropzone ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const f = e.dataTransfer.files[0];
          if (f) search(f);
        }}
      >
        <input type="file" hidden onChange={(e) => e.target.files?.[0] && search(e.target.files[0])} />
        {localPreview ? <img src={localPreview} alt="" /> : <Icon name="upload" size={32} />}
        <span>{file ? file.name : "Drop an image here, or click to choose one"}</span>
        {busy && <span className="muted">Searching…</span>}
      </label>
      <ErrorNote error={error} />
      {matches && matches.length === 0 && <div className="empty">Not found in the catalog, and nothing looks similar.</div>}
      {matches && matches.length > 0 && (
        <div className="matches">
          {matches.map((m) => (
            <div key={m.hash} className="match">
              <div className="match-thumb">
                {m.has_preview ? <img src={previewUrl(m.hash)} alt="" /> : <span>?</span>}
              </div>
              <div className="match-body">
                <div className="row">
                  <strong>{m.exact ? "Identical copy" : `Looks similar (difference ${m.distance})`}</strong>
                  <StatusBadge status={m.status} />
                </div>
                <div className="muted small">
                  {m.width}×{m.height} · {m.format} · {bytes(m.size)} {m.taken_at ? `· taken ${date(m.taken_at)}` : ""}
                </div>
                <ul className="locations">
                  {m.locations.map((loc) => {
                    const v = volumes.get(loc.volume_id);
                    const full = v?.root ? osPath(v.root, loc.path) : loc.path;
                    return (
                      <li key={loc.file_id}>
                        <OnlineDot online={loc.online} />
                        <div>
                          <strong>{loc.volume_label}</strong>
                          {!loc.online && <span className="muted small"> — connect this drive to open it</span>}
                          <div className="loc-path" title={full}>{loc.path}</div>
                        </div>
                        <CopyButton text={full} />
                      </li>
                    );
                  })}
                </ul>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
