// V4.1 offline project (Worker F): run with
//   npx playwright test -c e2e/playwright.v41.config.js
// It launches e2e/fixture_server_v41.py (the post-rebuild hermetic backend:
// real server code, schema-2 registry fixture, stub orchestrator/LiteLLM/
// AgentOS) and runs only the V4.1 specs, leaving the legacy config untouched.
import { randomBytes } from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

const e2ePassword = process.env.GX_E2E_PASSWORD || `E2e-${randomBytes(12).toString('hex')}`;
process.env.GX_E2E_PASSWORD = e2ePassword;
const port = Number(process.env.GX_E2E_PORT_V41 || 18091);
const outputDir = process.env.GX_E2E_OUTPUT_DIR || 'test-results-v41';

export default defineConfig({
  outputDir,
  testDir: __dirname,
  testMatch: /offline\.v41\.spec\.js/,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: { ...devices['Desktop Chrome'], baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    command: `python3 fixture_server_v41.py ${port}`,
    url: `http://127.0.0.1:${port}/api/health`,
    cwd: __dirname,
    reuseExistingServer: false,
    timeout: 30_000,
    env: { GX_E2E_PASSWORD: e2ePassword, GX_UI_ACCESS_LOG: '0' },
  },
});
