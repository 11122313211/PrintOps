import journey from './journey.config.mjs';
import {defineConfig} from '@playwright/test';
import {fileURLToPath} from 'node:url';

const browserName = process.env.PRINTOPS_TEST_BROWSER || 'chromium';
const reportRoot = fileURLToPath(new URL(`../reports/print-intelligence/${browserName}/`, import.meta.url));

export default defineConfig({
  ...journey,
  testMatch: 'ui_print_intelligence.spec.mjs',
  expect: {timeout: 4000},
  outputDir: `${reportRoot}results`,
  reporter: [['list'], ['json', {outputFile: `${reportRoot}report.json`}]],
});
