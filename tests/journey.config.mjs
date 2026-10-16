import {defineConfig, devices} from '@playwright/test';
import smoke from './browser_smoke.config.mjs';
import {fileURLToPath} from 'node:url';
const browserName = process.env.PRINTOPS_TEST_BROWSER || 'chromium';
const desktop = {chromium:'Desktop Chrome', firefox:'Desktop Firefox', webkit:'Desktop Safari'};
if (!Object.hasOwn(desktop, browserName)) throw new Error(`Unsupported test browser: ${browserName}`);
const reportName = browserName === 'chromium' ? 'journey-browser' : `journey-${browserName}`;
const reportRoot = fileURLToPath(new URL(`../reports/${reportName}/`, import.meta.url));
export default defineConfig({...smoke,
  testMatch: ['journey.spec.mjs', 'journey_edges.spec.mjs'],
  use: {...smoke.use, ...devices[desktop[browserName]], browserName},
  outputDir: `${reportRoot}results`,
  reporter: [['list'], ['json', {outputFile: `${reportRoot}report.json`}],
    ['html', {outputFolder: `${reportRoot}html`, open: 'never'}]],
});
