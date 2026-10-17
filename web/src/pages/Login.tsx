import { useState } from "react";
import { api, type AuthStatus } from "../api";
import { Icon } from "../components/Icon";

/** Sign-in for remote devices. (On the computer running the app, no sign-in is needed.) */
export default function Login({ status, onSignedIn }: { status: AuthStatus | null; onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    api.login(password)
      .then(() => { setError(null); onSignedIn(); })
      .catch((err: Error) => { setError(err.message === "wrong password" ? "Wrong password." : err.message); setPassword(""); })
      .finally(() => setBusy(false));
  };

  return (
    <div className="login">
      <form className="login-box" onSubmit={submit}>
        <div className="brand"><Icon name="camera" /> <span>Image Library</span></div>
        {status?.identity && (
          <p className="muted small">
            Connected through {status.via === "tailscale" ? "Tailscale" : "Cloudflare"} as <strong>{status.identity}</strong>.
          </p>
        )}
        {status && !status.password_set ? (
          <p>Remote sign-in is not set up. Set a password in Settings on the computer running the app.</p>
        ) : (
          <>
            <label className="field">
              <span>Password</span>
              <input type="password" autoComplete="current-password" autoFocus value={password}
                onChange={(e) => setPassword(e.target.value)} />
            </label>
            {error && <div className="note note-error">{error}</div>}
            <button className="btn btn-primary" disabled={!password || busy}>{busy ? "Signing in…" : "Sign in"}</button>
          </>
        )}
      </form>
    </div>
  );
}
