// Default Playwright config: runs the V4.1 hermetic suite. The pre-V4.1
// offline fixture (e2e/fixture_server.py) and the live specs were retired
// with the rebuilt dashboard; this config now extends the V4.1 config
// (e2e/playwright.v41.config.js → e2e/fixture_server_v41.py +
// offline.v41.spec.js). The "offline" project name is kept so
// `npm run test:e2e` (--project=offline) keeps working; there is no "live"
// project any more (its specs are retired).
import v41Config from './e2e/playwright.v41.config.js';

export default {
  ...v41Config,
  projects: [{ name: 'offline' }],
};
