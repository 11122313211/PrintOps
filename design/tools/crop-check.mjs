/** Crop bottom strips of candidate pages at 100% scale for layout inspection. */
import {chromium} from '@playwright/test';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const targets = [
  {file: 'direction-a.html', clip: {x: 1000, y: 780, width: 440, height: 220}, out: 'a-bottom.png'},
  {file: 'direction-b.html', clip: {x: 280, y: 780, width: 900, height: 220}, out: 'b-bottom.png'},
  {file: 'direction-c.html', clip: {x: 140, y: 780, width: 800, height: 220}, out: 'c-bottom.png'},
];
const browser = await chromium.launch();
const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
for (const t of targets) {
  await page.goto('file:///' + path.join(root, 'design/candidates', t.file).replace(/\\/g, '/'));
  await page.waitForTimeout(300);
  await page.screenshot({path: path.join(root, 'design/candidates/shots', t.out), clip: t.clip});
}
await browser.close();
console.log('crops done');
