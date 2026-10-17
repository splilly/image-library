// A handful of inline stroke icons (no icon font or library needed).
const PATHS: Record<string, string> = {
  home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  drive: "M3 14l2.5-8h13L21 14M3 14v5h18v-5M3 14h18M17 17h.01",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
  camera: "M4 8h3l2-3h6l2 3h3v11H4zM12 11a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  folder: "M3 6h6l2 2h10v11H3z",
  chevron: "M9 6l6 6-6 6",
  close: "M6 6l12 12M18 6L6 18",
  copy: "M8 8h11v12H8zM5 16V4h11",
  pause: "M8 5v14M16 5v14",
  play: "M7 5l12 7-12 7z",
  plus: "M12 5v14M5 12h14",
  lock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 8 0v3",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  up: "M12 19V5M5 12l7-7 7 7",
  upload: "M12 16V4M6 10l6-6 6 6M4 20h16",
};

export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={PATHS[name] ?? ""} />
    </svg>
  );
}
