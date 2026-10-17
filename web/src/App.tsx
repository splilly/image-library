import { useEffect, useState } from "react";
import { api, LOGIN_REQUIRED, type AuthStatus } from "./api";
import Actions from "./pages/Actions";
import Dashboard from "./pages/Dashboard";
import Drives from "./pages/Drives";
import Find from "./pages/Find";
import Library from "./pages/Library";
import Login from "./pages/Login";
import Organize from "./pages/Organize";
import Settings from "./pages/Settings";
import Review from "./pages/Review";
import { Icon } from "./components/Icon";
import { useRoute, useScanState } from "./util";

const PAGES = [
  { id: "dashboard", label: "Overview", icon: "home" },
  { id: "library", label: "Library", icon: "grid" },
  { id: "review", label: "Review", icon: "check" },
  { id: "actions", label: "Actions", icon: "play" },
  { id: "organize", label: "Organize", icon: "folder" },
  { id: "drives", label: "Drives", icon: "drive" },
  { id: "find", label: "Find image", icon: "search" },
  { id: "settings", label: "Settings", icon: "gear" },
] as const;

export default function App() {
  const route = useRoute();
  const scan = useScanState();
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [needLogin, setNeedLogin] = useState(false);
  const [blocked, setBlocked] = useState<string | null>(null);
  useEffect(() => {
    api.authStatus()
      .then((a) => { setAuth(a); setNeedLogin(!a.signed_in); })
      .catch((e: Error) => setBlocked(e.message));
    const onLogin = () => setNeedLogin(true);
    window.addEventListener(LOGIN_REQUIRED, onLogin);
    return () => window.removeEventListener(LOGIN_REQUIRED, onLogin);
  }, []);
  if (blocked) return <div className="login"><div className="login-box">Access refused: {blocked}</div></div>;
  if (needLogin) return <Login status={auth} onSignedIn={() => window.location.reload()} />;
  const active = scan?.running ? scan.targets.filter((t) => t.state === "analyzing" || t.state === "discovering") : [];

  return (
    <div className="app">
      <nav className="nav">
        <div className="brand">
          <Icon name="camera" /> <span>Image Library</span>
        </div>
        {PAGES.map((p) => (
          <a key={p.id} href={`#/${p.id}`} className={route.page === p.id ? "active" : ""}>
            <Icon name={p.icon} /> <span>{p.label}</span>
          </a>
        ))}
        {scan?.plan?.running && (
          <a href="#/actions" className="nav-scan" title="An action plan is running">
            <span className="pulse" /> <span>Moving files to quarantine</span>
          </a>
        )}
        {scan?.export?.running && (
          <a href="#/organize" className="nav-scan" title="Copying into a new library">
            <span className="pulse" /> <span>Organizing</span>
          </a>
        )}
        {scan?.running && (
          <a href="#/dashboard" className="nav-scan" title="A scan is running">
            <span className="pulse" /> <span>Scanning {active.length > 1 ? `${active.length} drives` : active[0]?.label ?? ""}</span>
          </a>
        )}
      </nav>
      <main className="main">
        {route.page === "library" ? (
          <Library params={route.params} />
        ) : route.page === "actions" ? (
          <Actions params={route.params} scan={scan} />
        ) : route.page === "review" ? (
          <Review params={route.params} scan={scan} />
        ) : route.page === "drives" ? (
          <Drives scan={scan} />
        ) : route.page === "organize" ? (
          <Organize scan={scan} />
        ) : route.page === "settings" ? (
          <Settings auth={auth} />
        ) : route.page === "find" ? (
          <Find />
        ) : (
          <Dashboard scan={scan} />
        )}
      </main>
    </div>
  );
}
