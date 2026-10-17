import Dashboard from "./pages/Dashboard";
import Drives from "./pages/Drives";
import Find from "./pages/Find";
import Library from "./pages/Library";
import { Icon } from "./components/Icon";
import { useRoute, useScanState } from "./util";

const PAGES = [
  { id: "dashboard", label: "Overview", icon: "home" },
  { id: "library", label: "Library", icon: "grid" },
  { id: "drives", label: "Drives", icon: "drive" },
  { id: "find", label: "Find image", icon: "search" },
] as const;

export default function App() {
  const route = useRoute();
  const scan = useScanState();
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
        {scan?.running && (
          <a href="#/dashboard" className="nav-scan" title="A scan is running">
            <span className="pulse" /> <span>Scanning {active.length > 1 ? `${active.length} drives` : active[0]?.label ?? ""}</span>
          </a>
        )}
      </nav>
      <main className="main">
        {route.page === "library" ? (
          <Library params={route.params} />
        ) : route.page === "drives" ? (
          <Drives scan={scan} />
        ) : route.page === "find" ? (
          <Find />
        ) : (
          <Dashboard scan={scan} />
        )}
      </main>
    </div>
  );
}
