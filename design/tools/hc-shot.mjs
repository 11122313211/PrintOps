/** Capture high-contrast theme screenshots via the isolated smoke server. */
import {chromium} from '@playwright/test';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const outDir = path.join(root, 'design', 'after');
const syntheticCard = '我要印1200张名片，成品尺寸9×5.4cm，250g铜版纸，双面四色，无特殊工艺，无需装订';

const python = process.platform === 'win32' ? 'python' : 'python3';
const child = spawn(python, ['-u', 'tests/browser_smoke_server.py'], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
child.stderr.on('data', () => {});
const appURL = await new Promise((resolve, reject) => {
  let output = '';
  const timeout = setTimeout(() => reject(new Error('server did not start')), 15_000);
  child.stdout.on('data', (chunk) => {
    output += chunk;
    const match = output.match(/PRINTOPS_BROWSER_SMOKE_READY (.+)\r?\n/);
    if (match) { clearTimeout(timeout); resolve(JSON.parse(match[1]).url); }
  });
});

const browser = await chromium.launch();
try {
  await mkdir(outDir, {recursive: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  await page.goto(appURL);
  await page.locator('#message-input').waitFor({state: 'visible'});
  await page.evaluate(() => {
    document.documentElement.dataset.theme = 'high-contrast';
    localStorage.setItem('printops_theme', 'high-contrast');
  });
  await page.locator('#message-input').fill(syntheticCard);
  const response = page.waitForResponse((r) => r.url().endsWith('/api/chat') && r.request().method() === 'POST');
  await page.locator('#message-form button[type="submit"]').click();
  await response;
  await page.waitForTimeout(400);
  await page.screenshot({path: `${outDir}/06-desktop-high-contrast.png`});
  await context.close();
  console.log('high-contrast captured');
} finally {
  await browser.close();
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
}
