/** Capture baseline UI screenshots of the real app via the isolated smoke server. */
import {chromium} from '@playwright/test';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const outDir = path.join(root, 'design', 'baseline');
const syntheticCard = '我要印1200张名片，成品尺寸9×5.4cm，250g铜版纸，双面四色，无特殊工艺，无需装订';

const python = process.platform === 'win32' ? 'python' : 'python3';
const child = spawn(python, ['-u', 'tests/browser_smoke_server.py'], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
let stderr = '';
child.stderr.on('data', (chunk) => { stderr += chunk; });

const appURL = await new Promise((resolve, reject) => {
  let output = '';
  const timeout = setTimeout(() => reject(new Error(`server did not start: ${stderr}`)), 15_000);
  child.once('error', (error) => { clearTimeout(timeout); reject(error); });
  child.once('exit', (code) => { clearTimeout(timeout); reject(new Error(`server exited ${code}: ${stderr}`)); });
  child.stdout.on('data', (chunk) => {
    output += chunk;
    const match = output.match(/PRINTOPS_BROWSER_SMOKE_READY (.+)\r?\n/);
    if (match) { clearTimeout(timeout); resolve(JSON.parse(match[1]).url); }
  });
});
console.log('server ready:', appURL);

const expand = async (page, selector) => {
  const details = page.locator(selector);
  if (!await details.evaluate((element) => element.open)) await details.locator(':scope > summary').click();
};

const send = async (page) => {
  await page.locator('#message-input').fill(syntheticCard);
  const response = page.waitForResponse((r) => r.url().endsWith('/api/chat') && r.request().method() === 'POST');
  await page.locator('#message-form button[type="submit"]').click();
  const received = await response;
  if (received.status() !== 200) throw new Error(`chat failed: ${received.status()}`);
  await page.locator('#message-input').waitFor({state: 'visible'});
};

const browser = await chromium.launch();
try {
  await mkdir(outDir, {recursive: true});
  const desktop = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await desktop.newPage();
  await page.goto(appURL);
  await page.locator('#message-input').waitFor({state: 'visible'});
  await page.screenshot({path: `${outDir}/01-desktop-first-use.png`});
  await send(page);
  await page.waitForTimeout(400);
  await page.screenshot({path: `${outDir}/02-desktop-intake.png`});

  await expand(page, '.draft-group[data-draft-group="specs"]');
  await page.locator('.order-document-links [data-view="compare"]').click();
  await page.locator('#phase-compare').waitFor({state: 'visible'});
  await page.screenshot({path: `${outDir}/03-desktop-compare.png`});
  await page.locator('.order-document-links [data-view="review"]').click();
  await page.locator('#phase-review').waitFor({state: 'visible'});
  await page.screenshot({path: `${outDir}/04-desktop-review.png`});
  await page.keyboard.press('Escape');
  await desktop.close();

  const mobile = await browser.newContext({viewport: {width: 390, height: 844}});
  const mpage = await mobile.newPage();
  await mpage.goto(appURL);
  await mpage.locator('#message-input').waitFor({state: 'visible'});
  await send(mpage);
  await mpage.waitForTimeout(400);
  await mpage.screenshot({path: `${outDir}/05-mobile-intake.png`});
  await mobile.close();
  console.log('baseline captured');
} finally {
  await browser.close();
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
}
