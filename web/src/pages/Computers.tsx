import { useState } from "react";
import { api, type HubState } from "../api";
import { CopyButton, ErrorNote, Modal } from "../components/Bits";
import { ago, count, useLoad } from "../util";

/** Hub & agents: computers that send their scans here, or the hub this computer sends to. */
export function Computers() {
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <h2>Other computers</h2>
      <p className="muted">
        Drives that only another computer can read (a Mac's APFS drive, say) are scanned by that computer — an
        <em> agent</em> — and appear here on the <em>hub</em>, where you browse, review and plan. Agents reach the hub
        through Tailscale or Cloudflare (above), or carry their scans over in a bundle file.
      </p>
      <ErrorNote error={error} />
      <AgentsCard onError={setError} />
      <HubCard onError={setError} />
      <BundlesCard onError={setError} />
    </>
  );
}

function AgentsCard({ onError }: { onError: (e: string | null) => void }) {
  const agents = useLoad(() => api.agents(), []);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<{ name: string; token: string } | null>(null);
  return (
    <section className="panel">
      <h3>Agents sending scans to this computer</h3>
      {agents.data?.length === 0 && <p className="muted small">None yet.</p>}
      {agents.data && agents.data.length > 0 && (
        <table className="table">
          <tbody>
            {agents.data.map((a) => (
              <tr key={a.id}>
                <td><strong>{a.name}</strong> <span className="muted small">{a.machine ? `${a.machine} (${a.os})` : "not synced yet"}</span>
                  <div className="small">{a.connected_volumes.length ? `Connected drives: ${a.connected_volumes.join(", ")}` : "No drives connected"}</div></td>
                <td className="small muted">last sync {ago(a.last_sync_at)}</td>
                <td><button className="btn btn-small" onClick={() => api.revokeAgent(a.id).then((x) => agents.setData(x)).catch((e: Error) => onError(e.message))}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form className="row" onSubmit={(e) => {
        e.preventDefault();
        api.addAgent(name).then((r) => { setCreated({ name, token: r.token }); setName(""); agents.reload(); })
          .catch((err: Error) => onError(err.message));
      }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name, e.g. Mac mini" />
        <button className="btn" disabled={!name.trim()}>Add agent</button>
      </form>
      {created && (
        <Modal title={`Agent “${created.name}” added`} onClose={() => setCreated(null)}
          footer={<button className="btn btn-primary" onClick={() => setCreated(null)}>Done</button>}>
          <p>Copy this token now; it is shown only once.</p>
          <div className="cmd"><code className="token">{created.token}</code><CopyButton text={created.token} label="Copy token" /></div>
          <p className="small">On the agent computer, open Settings → “Send scans to a hub”, enter this computer's https address
            (its Tailscale name or Cloudflare hostname) and the token.</p>
        </Modal>
      )}
    </section>
  );
}

function HubCard({ onError }: { onError: (e: string | null) => void }) {
  const hub = useLoad(() => api.hub(), []);
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [cfId, setCfId] = useState("");
  const [cfSecret, setCfSecret] = useState("");
  const h: HubState | null = hub.data;
  const save = () => api.updateHub({ url: url || h?.url, ...(token ? { token } : {}), ...(cfId ? { cf_client_id: cfId } : {}),
    ...(cfSecret ? { cf_client_secret: cfSecret } : {}) })
    .then((x) => { hub.setData(x); setToken(""); setCfSecret(""); onError(null); }).catch((e: Error) => onError(e.message));
  return (
    <section className="panel">
      <h3>Send this computer's scans to a hub</h3>
      {h?.url && (
        <p className="small">
          Sending to <code>{h.url}</code> · last sync {ago(h.last_sync_at)}
          {h.last && <> · sent {count(h.last.files)} file changes, carried out {count(h.last.actions)} actions</>}
          {h.last_error && <div className="num-bad">Last attempt failed: {h.last_error}</div>}
        </p>
      )}
      <div className="grid2">
        <label className="field"><span>Hub address</span>
          <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder={h?.url || "https://desktop.tail1234.ts.net"} /></label>
        <label className="field"><span>Agent token {h?.token && <span className="tag">saved</span>}</span>
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={h?.token ? "(unchanged)" : "from the hub"} /></label>
        <label className="field"><span>Cloudflare service token id <span className="muted">(only if the hub is behind Cloudflare)</span></span>
          <input value={cfId} onChange={(e) => setCfId(e.target.value)} placeholder={h?.cf_client_id || ""} /></label>
        <label className="field"><span>Cloudflare service token secret {h?.cf_client_secret && <span className="tag">saved</span>}</span>
          <input type="password" value={cfSecret} onChange={(e) => setCfSecret(e.target.value)} /></label>
      </div>
      <div className="row">
        <button className="btn" onClick={save}>Save</button>
        <button className="btn" disabled={!h?.url || h?.running}
          onClick={() => api.syncNow().then((x) => hub.setData(x)).catch((e: Error) => onError(e.message))}>
          {h?.running ? "Syncing…" : "Sync now"}
        </button>
        <span className="muted small">Syncs automatically after every scan and every 5 minutes.</span>
      </div>
    </section>
  );
}

function BundlesCard({ onError }: { onError: (e: string | null) => void }) {
  const [result, setResult] = useState<string | null>(null);
  return (
    <section className="panel">
      <h3>Bundles (no connection between the computers)</h3>
      <p className="muted small">Export on the agent, carry the file over, import on the hub. Each export holds what changed
        since the previous one. Actions for the agent's drives still need a connection.</p>
      <div className="row">
        <a className="btn" href={api.bundleExportUrl(false)}>Export changes</a>
        <a className="btn" href={api.bundleExportUrl(true)}>Export everything</a>
        <label className="btn">
          Import a bundle…
          <input type="file" hidden accept=".imagelib-bundle" onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) api.importBundle(f).then((r) => { setResult(`Imported ${count(r.files)} file changes and ${count(r.previews)} previews.`); onError(null); })
              .catch((err: Error) => onError(err.message));
          }} />
        </label>
      </div>
      {result && <div className="note">{result}</div>}
    </section>
  );
}
