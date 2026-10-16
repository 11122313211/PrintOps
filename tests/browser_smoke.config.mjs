import {defineConfig, devices} from '@playwright/test';
import {fileURLToPath} from 'node:url';

const reportRoot = fileURLToPath(new URL('../reports/browser-smoke/', import.meta.url));

export default defineConfig({
  testDir: '.',
  testMatch: 'browser_smoke.spec.mjs',
  timeout: 30_000,
  expect: {timeout: 8_000},
  workers: 1,
  retries: 0,
  outputDir: `${reportRoot}results`,
  reporter: [
    ['list'],
    ['json', {outputFile: `${reportRoot}report.json`}],
    ['html', {outputFolder: `${reportRoot}html`, open: 'never'}],
  ],
  use: {
    ...devices['Desktop Chrome'],
    viewport: {width: 1440, height: 1000},
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
