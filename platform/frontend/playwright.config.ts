import { defineConfig } from "@playwright/test";

// Smoke test against the real API and a database filled with LABELLED synthetic demo data
// (python -m app.cli seed-demo). Run through ../scripts/e2e.sh, which prepares that database.
const PY = process.env.E2E_PYTHON ?? "python";
const executablePath = process.env.PW_CHROMIUM_PATH || undefined; // e.g. /opt/pw-browsers/chromium

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  use: { baseURL: "http://127.0.0.1:3000", launchOptions: { executablePath } },
  webServer: [
    {
      command: `${PY} -m uvicorn --factory app.api.main:create_app --host 127.0.0.1 --port 8000`,
      cwd: "../backend",
      url: "http://127.0.0.1:8000/api/health",
      timeout: 60_000,
      reuseExistingServer: false,
    },
    {
      // The same standalone server the Docker image runs (static assets copied next to it).
      command: "cp -r .next/static .next/standalone/.next/ && node .next/standalone/server.js",
      url: "http://127.0.0.1:3000/login",
      env: { API_URL: "http://127.0.0.1:8000", PORT: "3000", HOSTNAME: "127.0.0.1", NEXT_TELEMETRY_DISABLED: "1" },
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
