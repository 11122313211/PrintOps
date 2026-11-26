import journey from './journey.config.mjs';
import {defineConfig} from '@playwright/test';
import {fileURLToPath} from 'node:url';

const browserName = process.env.PRINTOPS_TEST_BROWSER || 'chromium';
const reportRoot = fileURLToPath(new URL(`../reports/harness-ui/${browserName}/`, import.meta.url));

export default defineConfig({
  ...journey,
  testMatch: 'ui_harness.spec.mjs',
  expect: {timeout: 3000},
  outputDir: `${reportRoot}results`,
  reporter: [['list'], ['json', {outputFile: `${reportRoot}report.json`}]],
});
