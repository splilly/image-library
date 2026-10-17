import { useEffect, useState } from "react";
import { api, type AuthStatus, type RemoteSettings, type RemoteState } from "../api";
import { CopyButton, ErrorNote } from "../components/Bits";
import { ago, useLoad } from "../util";
import { Computers } from "./Computers";

const lines = (v: string) => v.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

export default function Settings({ auth }: { auth: AuthStatus | null }) {
  if (auth && !auth.local) {
    return (
      <div className="page">
        <h1>Settings</h1>
        <p>
          You are signed in through {auth.via === "tailscale" ? "Tailscale" : "Cloudflare"} as{" "}
          <strong>{auth.identity}</strong>. Remote access settings can only be changed on the computer running the app.
        </p>
        <button className="btn" onClick={() => api.logout().then(() => window.location.reload())}>Sign out</button>
      </div>
    );
  }
  return <RemoteAccess />;
}

function RemoteAccess() {
  const state = useLoad(() => api.remote(), []);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const run = (p: Promise<RemoteState>, message?: string) =>
    p.then((s) => { state.setData(s); setError(null); setSaved(message ?? null); })
      .catch((e: Error) => { setError(e.message); setSaved(null); });
  const s = state.data;

  return (
    <div className="page settings">
      <h1>Settings</h1>
      <h2>Remote access</h2>
      <p className="muted">
        Use this app from your phone or another computer. Remote devices must pass two checks: the tunnel's own login
        (Tailscale or Cloudflare Access) and this app's password. The app itself stays reachable only from this
        computer; Tailscale or Cloudflare forward to it.
      </p>
      <ErrorNote error={error} />
      {saved && <div className="note">{saved}</div>}
      {s && (
        <>
          <PasswordCard state={s} onSave={(pw) => run(api.setPassword(pw), "Password saved. Every remote device has been signed out.")} />
          <TailscaleCard state={s} onSave={(c) => run(api.updateRemote(c), "Saved.")} />
          <CloudflareCard state={s} onSave={(c) => run(api.updateRemote(c), "Saved.")}
            onTunnel={(a) => run(api.cloudflared(a))} />
          <section className="panel">
            <h3>Signed-in devices</h3>
            {s.sessions.length === 0 ? <p className="muted">None.</p> : (
              <table className="table">
                <tbody>
                  {s.sessions.map((x) => (
                    <tr key={x.id}>
                      <td><strong>{x.via === "tailscale" ? "Tailscale" : "Cloudflare"}</strong> · {x.client}
                        <div className="muted small">{x.user_agent}</div></td>
                      <td className="small muted">last seen {ago(x.last_seen_at)}</td>
                      <td><button className="btn btn-small" onClick={() => run(api.revokeSession(x.id), "Signed out.")}>Sign out</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
      <Computers />
    </div>
  );
}

function PasswordCard({ state, onSave }: { state: RemoteState; onSave: (pw: string) => void }) {
  const [pw, setPw] = useState("");
  const [again, setAgain] = useState("");
  const mismatch = again.length > 0 && pw !== again;
  return (
    <section className="panel">
      <h3>1. App password {state.password_set && <span className="tag">set</span>}</h3>
      <p className="muted small">Asked on every new remote device. Changing it signs every device out.</p>
      <form className="row" onSubmit={(e) => { e.preventDefault(); onSave(pw); setPw(""); setAgain(""); }}>
        <input type="password" autoComplete="new-password" placeholder="New password (10+ characters)" value={pw}
          onChange={(e) => setPw(e.target.value)} />
        <input type="password" autoComplete="new-password" placeholder="Repeat" value={again}
          onChange={(e) => setAgain(e.target.value)} />
        <button className="btn" disabled={pw.length < 10 || pw !== again}>{state.password_set ? "Change" : "Set"} password</button>
        {mismatch && <span className="num-bad small">Passwords differ.</span>}
      </form>
    </section>
  );
}

function TailscaleCard({ state, onSave }: { state: RemoteState; onSave: (c: Partial<RemoteSettings>) => void }) {
  const ts = state.tailscale;
  const cfg = state.settings;
  const [hosts, setHosts] = useState(cfg.tailscale_hosts.join(", "));
  const [users, setUsers] = useState(cfg.tailscale_users.join(", "));
  useEffect(() => { setHosts(cfg.tailscale_hosts.join(", ")); setUsers(cfg.tailscale_users.join(", ")); }, [cfg]);
  const url = cfg.tailscale_hosts[0] ? `https://${cfg.tailscale_hosts[0]}/` : null;
  return (
    <section className={`panel ${cfg.tailscale_enabled ? "enabled" : ""}`}>
      <div className="panel-head">
        <h3>2a. Tailscale <span className="muted small">private network between your devices; nothing exposed to the internet</span></h3>
        <label className="check">
          <input type="checkbox" checked={cfg.tailscale_enabled}
            onChange={(e) => onSave({ tailscale_enabled: e.target.checked, tailscale_hosts: lines(hosts) })} />
          Allow
        </label>
      </div>
      <p className="small">
        {!ts.installed ? <>Tailscale is not installed on this computer. Install it from tailscale.com and sign in.</>
          : !ts.running ? <>Tailscale is installed but not connected{ts.error ? ` (${ts.error})` : ""}.</>
          : <>This computer is <code>{ts.host}</code>, owned by <strong>{ts.owner}</strong>.</>}
      </p>
      <label className="field">
        <span>This computer's Tailscale name</span>
        <div className="row">
          <input value={hosts} onChange={(e) => setHosts(e.target.value)} placeholder="desktop.tail1234.ts.net" />
          {ts.host && <button className="btn btn-small" onClick={() => setHosts(ts.host!)}>Use {ts.host}</button>}
        </div>
      </label>
      <label className="field">
        <span>Allowed Tailscale users <span className="muted">(empty: only this computer's owner{ts.owner ? `, ${ts.owner}` : ""})</span></span>
        <input value={users} onChange={(e) => setUsers(e.target.value)} placeholder="me@example.com" />
      </label>
      <button className="btn" onClick={() => onSave({ tailscale_hosts: lines(hosts), tailscale_users: lines(users) })}>Save</button>
      <ol className="steps small">
        <li>On this computer, run once (it keeps running in the background):
          <div className="cmd"><code>{state.tailscale_command}</code><CopyButton text={state.tailscale_command} label="Copy command" /></div>
        </li>
        <li>On your other device, with Tailscale signed in to the same account, open {url ? <code>{url}</code> : "https://<this computer's name>/"}.</li>
      </ol>
    </section>
  );
}

function CloudflareCard({ state, onSave, onTunnel }: {
  state: RemoteState; onSave: (c: Partial<RemoteSettings>) => void; onTunnel: (a: "start" | "stop") => void;
}) {
  const cfg = state.settings;
  const cf = state.cloudflared;
  const [hosts, setHosts] = useState(cfg.cloudflare_hosts.join(", "));
  const [team, setTeam] = useState(cfg.cloudflare_team);
  const [aud, setAud] = useState(cfg.cloudflare_aud);
  const [token, setToken] = useState("");
  useEffect(() => { setHosts(cfg.cloudflare_hosts.join(", ")); setTeam(cfg.cloudflare_team); setAud(cfg.cloudflare_aud); }, [cfg]);
  const fields = (): Partial<RemoteSettings> => ({
    cloudflare_hosts: lines(hosts), cloudflare_team: team, cloudflare_aud: aud,
    ...(token ? { cloudflare_tunnel_token: token } : {}),
  });
  return (
    <section className={`panel ${cfg.cloudflare_enabled ? "enabled" : ""}`}>
      <div className="panel-head">
        <h3>2b. Cloudflare Tunnel + Access <span className="muted small">a web address that works from any browser, behind Cloudflare's login</span></h3>
        <label className="check">
          <input type="checkbox" checked={cfg.cloudflare_enabled}
            onChange={(e) => onSave({ ...fields(), cloudflare_enabled: e.target.checked })} />
          Allow
        </label>
      </div>
      <ol className="steps small">
        <li>In the Cloudflare dashboard (Zero Trust → Networks → Tunnels), create a tunnel and give it a public hostname
          that points to <code>http://localhost:{state.port}</code>.</li>
        <li>In Zero Trust → Access → Applications, add a self-hosted application for that hostname with a policy that
          allows only your email. Copy its <em>Application Audience (AUD) tag</em>.</li>
        <li>Fill in the fields below. Paste the tunnel token to let this app run the tunnel, or run
          <code>cloudflared</code> yourself.</li>
      </ol>
      <div className="grid2">
        <label className="field"><span>Public hostname</span>
          <input value={hosts} onChange={(e) => setHosts(e.target.value)} placeholder="photos.example.com" /></label>
        <label className="field"><span>Team name</span>
          <input value={team} onChange={(e) => setTeam(e.target.value)} placeholder="myteam (from myteam.cloudflareaccess.com)" /></label>
        <label className="field"><span>Application AUD tag</span>
          <input value={aud} onChange={(e) => setAud(e.target.value)} placeholder="4714c1358e65fe4b408ad6d432a5f878f08194bd…" /></label>
        <label className="field"><span>Tunnel token {cfg.cloudflare_tunnel_token && <span className="tag">saved</span>}</span>
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)}
            placeholder={cfg.cloudflare_tunnel_token ? "(unchanged)" : "optional"} /></label>
      </div>
      <div className="row">
        <button className="btn" onClick={() => { onSave(fields()); setToken(""); }}>Save</button>
        <span className="grow" />
        <span className="small">
          {!cf.installed ? "cloudflared is not installed on this computer." : cf.running ? "Tunnel running." : "Tunnel not running."}
        </span>
        {cf.installed && cfg.cloudflare_tunnel_token && (
          cf.running ? <button className="btn btn-small" onClick={() => onTunnel("stop")}>Stop tunnel</button>
            : <button className="btn btn-small" onClick={() => onTunnel("start")}>Start tunnel</button>
        )}
      </div>
      {cf.log.length > 0 && <pre className="log">{cf.log.join("\n")}</pre>}
    </section>
  );
}
