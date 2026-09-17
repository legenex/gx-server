// GX-Playground browser tests.
//   offline — hermetic: the REAL Control Center backend with synthetic cluster
//             data and stub upstreams (../control-ui/e2e/fixture_server.py),
//             behind the REAL Playground proxy (gx_playground). Safe anywhere.
//   live    — against the deployed Playground on gx10-01 (http://127.0.0.1:8090)
//             with REAL generations. Run deliberately: npm run test:live
import { randomBytes } from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

const e2ePassword = process.env.GX_E2E_PASSWORD || `E2e-${randomBytes(12).toString('hex')}`;
process.env.GX_E2E_PASSWORD = e2ePassword;
const backendPort = Number(process.env.GX_E2E_BACKEND_PORT || 18189);
const pgPort = Number(process.env.GX_E2E_PORT || 18190);
const onlyLive = process.argv.includes('--project=live');
const staticDir = process.env.GX_PG_STATIC_DIR || 'web';
const outputDir = process.env.GX_E2E_OUTPUT_DIR || 'test-results';

// Parallel workstreams running Playwright in the same checkout share the output
// directory and delete each other's traces mid-run ("browserContext.close:
// ENOENT ... recording.trace"), which reads as a flaky test. Each run can take
// its own directory (and its own proxy-token file) with GX_E2E_OUTPUT_DIR.
export default defineConfig({
  outputDir,
  testDir: process.env.GX_PG_TEST_DIR || './e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list'], ['json', { outputFile: `${outputDir}/results.json` }]],
  use: { trace: 'retain-on-failure', screenshot: 'only-on-failure', colorScheme: 'dark' },
  projects: [
    {
      name: 'offline',
      testMatch: /offline\..*\.spec\.js/,
      use: { ...devices['Desktop Chrome'], baseURL: `http://127.0.0.1:${pgPort}` },
    },
    {
      name: 'live',
      testMatch: /live\..*\.spec\.js/,
      timeout: 60 * 60_000,
      use: { ...devices['Desktop Chrome'], channel: 'chrome', baseURL: process.env.GX_PG_URL || 'http://127.0.0.1:8090', trace: 'off' },
    },
  ],
  webServer: onlyLive ? undefined : [
    {
      command: `python3 ../control-ui/e2e/fixture_server.py ${backendPort}`,
      url: `http://127.0.0.1:${backendPort}/api/health`,
      reuseExistingServer: false,
      timeout: 30_000,
      env: { GX_E2E_PASSWORD: e2ePassword, GX_UI_ACCESS_LOG: '0', GX_E2E_PROXY_TOKEN_FILE: `${process.cwd()}/${outputDir}/proxy-token` },
    },
    {
      command: 'python3 -m gx_playground',
      url: `http://127.0.0.1:${pgPort}/pg/health`,
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        GX_PG_HOSTS: '127.0.0.1', GX_PG_PORT: String(pgPort),
        GX_PG_UPSTREAM: `http://127.0.0.1:${backendPort}`,
        GX_PG_STATIC_DIR: staticDir,
        GX_PG_PROXY_TOKEN_FILE: `${process.cwd()}/${outputDir}/proxy-token`,
        GX_PG_CONTROL_URL: `http://127.0.0.1:${backendPort}/`,
      },
    },
  ],
});
