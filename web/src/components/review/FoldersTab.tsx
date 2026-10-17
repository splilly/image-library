import { useState } from "react";
import { api, type FolderPair, type FolderSide } from "../../api";
import { bytes, count, navigate, useLoad } from "../../util";
import { Empty, Modal } from "../Bits";
import { ActionError } from "./shared";

/** Folders that share many identical files: usually a backup or a copied import. */
export function FoldersTab({ onChanged }: { onChanged: () => void }) {
  const pairs = useLoad(() => api.folderPairs(), []);
  const [pending, setPending] = useState<{ keep: FolderSide; discard: FolderSide; pair: FolderPair } | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const apply = () => {
    if (!pending) return;
    const { keep, discard } = pending;
    setPending(null);
    api.discardFolderCopies(keep, discard)
      .then((r) => {
        setError(null);
        setDone(`Marked ${count(r.discarded)} files in “${discard.folder || "(top level)"}” to discard; ` +
          `their copies in “${keep.folder || "(top level)"}” are kept.`);
        onChanged();
        pairs.reload();
      })
      .catch((e: Error) => setError(e));
  };

  return (
    <div className="page wide">
      <p className="muted">
        Pairs of folders holding the same files, most shared first. Pick which folder to keep; identical files in the
        other folder are marked to discard. Only files directly in each folder are compared.
      </p>
      <ActionError error={error} />
      {done && <div className="note">{done}</div>}
      {pairs.data?.items.length === 0 && <Empty>No folders share identical files.</Empty>}
      <div className="pairs">
        {pairs.data?.items.map((p) => (
          <div key={`${p.a.volume_id}/${p.a.folder}|${p.b.volume_id}/${p.b.folder}`} className="pair">
            <div className="pair-stat">
              <strong>{count(p.shared)}</strong>
              <span className="muted small">identical files · {bytes(p.bytes)}</span>
            </div>
            <Side side={p.a} shared={p.shared} />
            <Side side={p.b} shared={p.shared} />
            <div className="pair-actions">
              <button className="btn btn-small" onClick={() => setPending({ keep: p.a, discard: p.b, pair: p })}>
                Keep left, discard copies on right
              </button>
              <button className="btn btn-small" onClick={() => setPending({ keep: p.b, discard: p.a, pair: p })}>
                Keep right, discard copies on left
              </button>
            </div>
          </div>
        ))}
      </div>
      {pending && (
        <Modal title="Discard duplicate copies in a folder" onClose={() => setPending(null)} footer={
          <>
            <button className="btn" onClick={() => setPending(null)}>Cancel</button>
            <button className="btn btn-danger" onClick={apply}>Mark {count(pending.pair.shared)} files to discard</button>
          </>
        }>
          <p>Keep: <strong>{pending.keep.volume_label}</strong> <code>{pending.keep.folder || "(top level)"}</code></p>
          <p>
            Discard the files in <strong>{pending.discard.volume_label}</strong>{" "}
            <code>{pending.discard.folder || "(top level)"}</code> that are identical to a file kept above.
            {pending.discard.files > pending.pair.shared &&
              ` Its other ${count(pending.discard.files - pending.pair.shared)} files are not touched.`}
          </p>
          <p className="muted small">Nothing is deleted now. Undo any time on the Decisions tab.</p>
        </Modal>
      )}
    </div>
  );
}

function Side({ side, shared }: { side: FolderSide; shared: number }) {
  const all = side.files > 0 && shared >= side.files;
  return (
    <button className="pair-side" title="Open in the library"
      onClick={() => navigate("library", { volume: side.volume_id, folder: side.folder || undefined, flat: "1" })}>
      <strong>{side.volume_label}</strong>
      <span className="loc-path">{side.folder || "(top level)"}</span>
      <span className="small muted">{count(side.files)} files{all ? " — all duplicated in the other folder" : ""}</span>
    </button>
  );
}
