import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built GUI is shipped inside the Python package and served by `imagelib gui`.
// During development, `npm run dev` proxies API calls to a running `imagelib gui --no-browser`.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../src/imagelib/ui", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
