// Two projects:
//   offline — hermetic: the real UI backend with synthetic cluster data and
//             stub upstreams (e2e/fixture_server.py). Safe to run anywhere.
//   live    — against the deployed UI on gx10-01 (http://127.0.0.1:8088) with
//             REAL model calls. Password from GX_UI_PASSWORD or the 0600
//             initial-password file. Run deliberately: npm run test:live
import { randomBytes } from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

const e2ePassword = process.env.GX_E2E_PASSWORD || `E2e-${randomBytes(12).toString('hex')}`;
process.env.GX_E2E_PASSWORD = e2ePassword;
const offlinePort = Number(process.env.GX_E2E_PORT || 18089);
const onlyLive = process.argv.includes('--project=live');

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list'], ['json', { outputFile: 'test-results/results.json' }]],
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    colorScheme: 'dark',
  },
  projects: [
    {
      name: 'offline',
      testMatch: /offline\..*\.spec\.js/,
      use: { ...devices['Desktop Chrome'], baseURL: `http://127.0.0.1:${offlinePort}` },
    },
    {
      name: 'live',
      testMatch: /live\..*\.spec\.js/,
      timeout: 60 * 60_000,
      // Google Chrome (not the bundled Chromium) so H.264 MP4 from gx-video can be
      // decoded for the frame check.
      use: { ...devices['Desktop Chrome'], channel: 'chrome', baseURL: process.env.GX_UI_URL || 'http://127.0.0.1:8088' },
    },
  ],
  webServer: onlyLive ? undefined : {
    command: `python3 e2e/fixture_server.py ${offlinePort}`,
    url: `http://127.0.0.1:${offlinePort}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
    env: { GX_E2E_PASSWORD: e2ePassword, GX_UI_ACCESS_LOG: '0' },
  },
});
