/** Real desktop browser checks using synthetic orders and an isolated server. */
import {test as base, expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));

export const test = base.extend({
  appURL: [async ({}, use) => {
    // Windows ships a store-alias `python3` stub that fails silently; pick the
    // platform's real interpreter unless the caller overrides explicitly.
    const python = process.env.PRINTOPS_TEST_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
    const child = spawn(python,
      ['-u', 'tests/browser_smoke_server.py'], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
    let stderr = '';
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    try {
      const appURL = await new Promise((resolve, reject) => {
        let output = '';
        const timeout = setTimeout(() => reject(new Error(`Test server did not start: ${stderr}`)), 15_000);
        child.once('error', (error) => { clearTimeout(timeout); reject(error); });
        child.once('exit', (code) => { clearTimeout(timeout); reject(new Error(`Test server exited ${code}: ${stderr}`)); });
        child.stdout.on('data', (chunk) => {
          output += chunk;
          // Windows text-mode pipes emit CRLF; the old bare-LF regex never matched
          // there, so the fixture always timed out with an empty stderr.
          const match = output.match(/PRINTOPS_BROWSER_SMOKE_READY (.+)\r?\n/);
          if (match) {
            clearTimeout(timeout);
            const ready = JSON.parse(match[1]);
            if (ready.llmEnabled) reject(new Error('Browser fixtures must not call a model'));
            else resolve(ready.url);
          }
        });
      });
      await use(appURL);
    } finally {
      if (child.exitCode === null && child.signalCode === null) {
        const exited = once(child, 'exit');
        child.kill('SIGTERM');
        const killTimeout = setTimeout(() => child.kill('SIGKILL'), 5_000);
        await exited;
        clearTimeout(killTimeout);
      }
    }
  }, {scope: 'worker'}],
  page: async ({browser, browserName, appURL}, use, testInfo) => {
    testInfo.annotations.push({type:'browser', description:`${browserName} ${browser.version()}`});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const externalRequests = [];
    await context.route('**/*', (route) => {
      if (new URL(route.request().url()).origin === appURL) return route.continue();
      externalRequests.push(route.request().url());
      return route.abort();
    });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    try {
      await use(page);
      await testInfo.attach('actual-browser.png', {
        body: await page.screenshot({fullPage: true}), contentType: 'image/png',
      });
      expect(pageErrors, 'No uncaught application JavaScript errors').toEqual([]);
      expect(externalRequests, 'Browser checks must use only the isolated local server').toEqual([]);
    } finally {
      await context.close();
    }
  },
});

export async function boot(page, appURL) {
  await page.goto(appURL);
  await expect(page.locator('#order-number')).not.toContainText('----');
  await expect(page.locator('#message-input')).toBeEnabled();
  await expect(page.locator('#agent-status')).not.toContainText('服务未启动');
}

export async function send(page, text) {
  await returnToChat(page);
  await page.locator('#message-input').fill(text);
  const response = page.waitForResponse((r) => r.url().endsWith('/api/chat') && r.request().method() === 'POST');
  await page.locator('#message-form button[type="submit"]').click();
  const received = await response;
  expect(received.status()).toBe(200);
  const snapshot = await received.json();
  await expect(page.locator('#message-input')).toBeEnabled();
  return snapshot;
}

export async function currentOrder(page) {
  return page.evaluate(async () => {
    const id = localStorage.getItem('printops_session');
    const response = await fetch(`/api/orders/${id}`, {headers: {'X-PrintOps-Token': window.__PRINTOPS_TOKEN__}});
    if (!response.ok) throw new Error(`History read failed: ${response.status}`);
    return response.json();
  });
}

export async function expand(page, selector) {
  const details = page.locator(selector);
  if (!await details.evaluate((element) => element.open)) await details.locator(':scope > summary').click();
}

export async function returnToChat(page) {
  if (await page.locator('#settings-dialog[open]').count()) await page.keyboard.press('Escape');
  if (await page.locator('#workspace-backdrop').isVisible()) await page.keyboard.press('Escape');
  if (await page.locator('#mobile-back').isVisible()) await page.locator('#mobile-back').click();
  if (await page.locator('#phase-intake').isVisible()) return;
  const back = page.locator('.journey-pane:not([hidden]) [data-view="intake"]');
  if (await back.isVisible()) await back.click();
  await expect(page.locator('#phase-intake')).toBeVisible();
}

export async function openOrder(page) {
  if (await page.locator('#settings-dialog[open]').count()) await page.keyboard.press('Escape');
  if (await page.locator('#workspace-backdrop').isVisible()) await page.keyboard.press('Escape');
  if (!await page.locator('.order-section').isVisible()) await page.locator('#order-toggle').click();
  await expect(page.locator('.order-section')).toBeVisible();
  // Product focus lives in the existing, initially folded specification group.
  await expand(page, '.draft-group[data-draft-group="specs"]');
  for (const item of await page.locator('.multi-product-item').all()) {
    if (!await item.evaluate(element => element.open)) await item.locator(':scope > summary').click();
  }
}

export async function openCompare(page) {
  await openOrder(page);
  await page.locator('.order-document-links [data-view="compare"]').click();
  await expect(page.locator('#phase-compare')).toBeVisible();
}

export async function openHandoff(page) {
  await openOrder(page);
  await page.locator('.order-document-links [data-view="review"]').click();
  await expect(page.locator('#phase-review')).toBeVisible();
}

export async function openTools(page) {
  await returnToChat(page);
  await openSidebar(page);
  await page.locator('#settings-button').click();
  await expect(page.locator('#settings-dialog')).toBeVisible();
  await page.locator('[data-settings-view="tools"]').click();
  await expect(page.locator('#settings-panel-tools')).toBeVisible();
  await expand(page, '.tool-panel');
}

export async function openSidebar(page) {
  if (await page.locator('#settings-dialog[open]').count()) await page.keyboard.press('Escape');
  if (await page.locator('#mobile-back').isVisible()) await page.locator('#mobile-back').click();
  await page.waitForTimeout(50);
  if (!await page.locator('.sidebar').isVisible()) await page.locator('#sidebar-toggle').click();
  await expect(page.locator('.sidebar')).toBeVisible();
}

export async function reviewInput(page, path) {
  await openHandoff(page);
  await expand(page, '#review-checklist-section');
  const row = page.locator(`.review-field[data-field-id="${path}"]`);
  const group = page.locator('.review-group').filter({has:row});
  if (!await group.evaluate(element => element.open)) await group.locator(':scope > summary').click();
  const editor = row.locator('.review-editor');
  if (!await editor.evaluate(element => element.open)) await editor.locator(':scope > summary').click();
  return row.locator('input');
}

export async function switchHistory(page, id) {
  await openSidebar(page);
  const read = page.waitForResponse(response => new URL(response.url()).pathname === `/api/orders/${id}` && response.request().method()==='GET');
  const item = page.locator(`.history-item[data-session-id="${id}"]`);
  await item.click();
  expect((await read).status()).toBe(200);
  await expect(item).toHaveAttribute('aria-current','true');
}

export async function openHistoryMenu(page, id) {
  const trigger = page.locator(`.history-item[data-session-id="${id}"]`);
  await trigger.click({button: 'right'});
  await expect(page.locator('#history-context-menu')).toBeVisible();
  return page.locator('#history-context-menu [data-history-menu-action]');
}
