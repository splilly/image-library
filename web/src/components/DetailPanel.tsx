import { useState } from "react";
import { api, originalUrl, previewUrl, type FileItem, type Volume } from "../api";
import { bytes, date, fileName, osPath, useLoad } from "../util";
import { CopyButton, ErrorNote, OnlineDot, StatusBadge } from "./Bits";
import { Icon } from "./Icon";

const BROWSER_EXT = /\.(jpe?g|jpe|jfif|png|gif|webp|bmp)$/i;

const META_KEYS: [string, string][] = [
  ["Exif.Photo.ExposureTime", "Exposure"],
  ["Exif.Photo.FNumber", "Aperture"],
  ["Exif.Photo.ISOSpeedRatings", "ISO"],
  ["Exif.Photo.FocalLength", "Focal length"],
  ["Exif.Photo.LensModel", "Lens"],
  ["Exif.Image.Artist", "Artist"],
  ["Exif.Image.ImageDescription", "Description"],
];

/** Everything known about one file: preview, problems, every copy and its drive, look-alikes. */
export function DetailPanel({ file, volumes, onClose, onOpenHash }: {
  file: FileItem;
  volumes: Map<string, Volume>;
  onClose: () => void;
  onOpenHash: (hash: string) => void;
}) {
  const { data, error } = useLoad(
    () => (file.hash ? api.content(file.hash) : Promise.resolve(null)),
    [file.hash],
  );
  const [full, setFull] = useState(false);
  const vol = volumes.get(file.volume_id);
  const canOpenOriginal = BROWSER_EXT.test(file.path) && vol?.online;
  const meta = data?.metadata ?? {};

  return (
    <aside className="detail">
      <header>
        <h2 title={file.path}>{fileName(file.path)}</h2>
        <button className="icon-btn" onClick={onClose} aria-label="Close details">
          <Icon name="close" />
        </button>
      </header>

      <div className="detail-preview">
        {file.hash && (data?.has_preview ?? file.has_preview) ? (
          <img src={full && canOpenOriginal ? originalUrl(file.id) : previewUrl(file.hash, "m")} alt="" />
        ) : (
          <div className="no-preview">No preview{file.state !== "done" ? ` (${file.state.replace("_", " ")})` : ""}</div>
        )}
      </div>
      {canOpenOriginal && (
        <div className="detail-actions">
          <button className="btn btn-small" onClick={() => setFull(!full)}>
            {full ? "Show preview" : "Load full resolution"}
          </button>
          <a className="btn btn-small" href={originalUrl(file.id)} target="_blank" rel="noreferrer">
            <Icon name="external" size={14} /> Open original
          </a>
        </div>
      )}

      <ErrorNote error={error} />
      {file.error && <div className="note note-error">{file.error}</div>}
      {data && data.problems.length > 0 && (
        <div className={`note ${data.status === "corrupt" ? "note-error" : "note-warn"}`}>
          <StatusBadge status={data.status} />
          <ul>
            {data.problems.map((p) => <li key={p}>{p}</li>)}
          </ul>
        </div>
      )}

      <dl className="facts">
        <dt>Status</dt>
        <dd><StatusBadge status={data?.status ?? (file.state === "done" ? file.status : file.state)} /></dd>
        {(data?.taken_at || file.taken_at) && (<><dt>Taken</dt><dd>{date(data?.taken_at ?? file.taken_at)}</dd></>)}
        {data?.camera_model && (<><dt>Camera</dt><dd>{data.camera_model}</dd></>)}
        {(data?.width ?? file.width) && (
          <><dt>Size</dt><dd>{data?.width ?? file.width} × {data?.height ?? file.height} · {bytes(file.size)}</dd></>
        )}
        {(data?.format ?? file.format) && (<><dt>Format</dt><dd>{data?.format ?? file.format}</dd></>)}
        {data?.duration_s && (<><dt>Length</dt><dd>{Math.round(data.duration_s)} s</dd></>)}
        {data?.software && (<><dt>Software</dt><dd>{data.software}</dd></>)}
        {META_KEYS.filter(([k]) => meta[k]).map(([k, label]) => (
          <span key={k} style={{ display: "contents" }}><dt>{label}</dt><dd>{meta[k]}</dd></span>
        ))}
      </dl>

      <section>
        <h3>{data && data.locations.length > 1 ? `${data.locations.length} copies` : "Location"}</h3>
        <ul className="locations">
          {(data?.locations ?? [{ file_id: file.id, volume_id: file.volume_id, volume_label: vol?.label ?? "?",
            path: file.path, state: file.state, read_only: vol?.read_only ?? false, online: vol?.online ?? false }])
            .map((loc) => {
              const v = volumes.get(loc.volume_id);
              const full = v?.root ? osPath(v.root, loc.path) : loc.path;
              return (
                <li key={loc.file_id} className={loc.file_id === file.id ? "current" : ""}>
                  <OnlineDot online={loc.online} />
                  <div>
                    <strong>{loc.volume_label}</strong>
                    {loc.state === "missing" && <span className="badge badge-missing">Missing</span>}
                    <div className="loc-path" title={full}>{loc.path}</div>
                  </div>
                  <CopyButton text={full} />
                </li>
              );
            })}
        </ul>
      </section>

      {data && data.similar.length > 0 && (
        <section>
          <h3>Looks similar ({data.similar.length})</h3>
          <p className="muted small">Resized copies, edits or thumbnails of this image.</p>
          <div className="similar">
            {data.similar.map((s) => (
              <button key={s.hash} onClick={() => onOpenHash(s.hash)}
                title={`${s.width}×${s.height} · ${bytes(s.size)} · difference ${s.distance}`}>
                {s.has_preview ? <img src={previewUrl(s.hash)} alt="" loading="lazy" /> : <span>?</span>}
                <small>{s.width}×{s.height}</small>
              </button>
            ))}
          </div>
        </section>
      )}
    </aside>
  );
}
