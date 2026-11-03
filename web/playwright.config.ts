import { defineConfig, devices } from "@playwright/test";

/** Browser tests for the site and the game table, against the real server (see e2e/README). */
const PORT = 8765;

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // the pages as served in production (web/dist), and a server with test settings
    command: `npm run build && cd .. && .venv/bin/python -m optcg.server --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      OPTCG_DEV_LOGIN: "1",
      OPTCG_DB: ":memory:",
      OPTCG_REPLAYS: "/tmp/optcg-e2e-replays",
      OPTCG_BOTS: "greedy;random",
      OPTCG_DECKS: "rocks;(Yellow) Charlotte Linlin",
      OPTCG_HOUSE_DECK: "(Yellow) Charlotte Linlin",
      OPTCG_MAX_GAMES: "50",
    },
  },
});
