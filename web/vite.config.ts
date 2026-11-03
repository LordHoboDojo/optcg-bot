import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the pages on :5173 and sends everything else to the Python server on :8000.
const server = "http://127.0.0.1:8000";
const passthrough = ["/api", "/game", "/matches", "/art", "/login", "/logout", "/auth", "/decks", "/healthz"];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(passthrough.map((p) => [p, { target: server, ws: p === "/api" }])),
  },
});
