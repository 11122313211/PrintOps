import journey from './journey.config.mjs';
import {defineConfig} from '@playwright/test';
import {fileURLToPath} from 'node:url';

const browserName = process.env.PRINTOPS_TEST_BROWSER || 'chromium';
const runName = process.env.PRINTOPS_FOCUS_RUN || 'verification';
if (!/^[a-z0-9-]+$/.test(runName)) throw new Error('Invalid focus report run name');
const reportRoot = fileURLToPath(new URL(`../reports/ui-position-tuning/focus-followup/${browserName}/${runName}/`, import.meta.url));

export default defineConfig({...journey,
  testMatch: 'ui_focus.spec.mjs',
  expect: {timeout: 2500},
  outputDir: `${reportRoot}results`,
  reporter: [['list'], ['json', {outputFile: `${reportRoot}report.json`}]],
});
