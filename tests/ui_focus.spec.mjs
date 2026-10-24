/** Local UI follow-up: real synthetic API responses, keyboard focus, and entry semantics. */
import {expect} from '@playwright/test';
import {test, boot, send, currentOrder, openCompare, openHandoff, openSidebar, returnToChat} from './journey.fixture.mjs';

const card = '500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内';
const settled = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
function gate() {
  let release;
  const promise = new Promise(resolve => {release = resolve;});
  return {promise, release};
}
async function posted(page, path, action) {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === path && r.request().method() === 'POST');
  await action();
  const result = await response;
  expect(result.status()).toBe(200);
  return result.json();
}
async function showOrder(page) {
  if (!await page.locator('.order-section').isVisible()) await page.locator('#order-toggle').click();
  await expect(page.locator('.order-section')).toBeVisible();
}
async function keyboardActivate(locator, key = 'Enter') {
  await locator.focus();
  await locator.press(key);
}
async function expectOrderDisclosure(page) {
  const visible = await page.locator('.order-section').isVisible();
  const toggle = page.locator('#order-toggle');
  const label = visible ? '收起订单栏' : '展开订单栏';
  await expect.soft(toggle).toHaveAttribute('aria-label', label);
  if (await toggle.isVisible()) await expect.soft(toggle).toHaveAccessibleName(label);
  await expect.soft(toggle).toHaveAttribute('aria-expanded', String(visible));
  const controls = await toggle.getAttribute('aria-controls');
  expect.soft(controls, 'Order disclosure identifies its controlled pane').toBeTruthy();
  if (controls) expect.soft(await page.locator('.order-section').getAttribute('id')).toBe(controls);
}

for (const width of [390, 1440]) {
  test(`order disclosure describes actual pane state and survives width changes at ${width}px`, async ({page, appURL}, info) => {
    await page.setViewportSize({width, height:844});
    await boot(page, appURL);
    const pane = page.locator('.order-section');
    if (width === 390) await expect(pane).toBeHidden();
    else await expect(pane).toBeVisible();
    await expectOrderDisclosure(page);
    await page.locator('#order-toggle').click();
    if (width === 390) await expect(pane).toBeVisible();
    else await expect(pane).toBeHidden();
    await expectOrderDisclosure(page);
    if (width === 390) await page.locator('#mobile-back').click();
    else await page.locator('#order-toggle').click();
    if (width === 390) await expect(pane).toBeHidden();
    else await expect(pane).toBeVisible();
    await expectOrderDisclosure(page);
    await page.setViewportSize({width:width === 390 ? 1440 : 390, height:844});
    await settled(page);
    await expectOrderDisclosure(page);
    await page.setViewportSize({width, height:844});
    await settled(page);
    await expectOrderDisclosure(page);
    await showOrder(page);
    if (width === 1440) {
      await page.locator('#platform-select').scrollIntoViewIfNeeded();
      await page.locator('#platform-select').focus();
    }
    const filename = width === 1440 ? 'platform-focused-1440.png' : 'mobile-order-390.png';
    const evidence = info.outputPath(filename);
    await page.screenshot({path:evidence, fullPage:true, animations:'disabled'});
    await info.attach(filename, {path:evidence, contentType:'image/png'});
  });

  test(`keyboard review navigation cannot confirm an unconfirmed draft at ${width}px`, async ({page, appURL}) => {
    await page.setViewportSize({width, height:844});
    await boot(page, appURL);
    await send(page, card);
    await openCompare(page);
    await posted(page, '/api/choose', () => page.locator('.choose-option').first().click());
    await openHandoff(page);
    const generated = await posted(page, '/api/generate', () => page.locator('#generate-order').click());
    expect(generated.orderGenerated).toBe(true);
    expect(generated.confirmation.status).not.toBe('confirmed');
    await returnToChat(page);
    await page.locator('#message-input').fill('尚未发送的补充内容');
    const writes = [], downloads = [];
    page.on('request', request => {if(request.method() === 'POST') writes.push(new URL(request.url()).pathname);});
    page.on('download', download => downloads.push(download.suggestedFilename()));
    await expect(page.locator('.task-bar')).toBeHidden();
    await showOrder(page);
    await keyboardActivate(page.locator('.order-document-links [data-view="review"]'));
    await expect(page.locator('#phase-review')).toBeVisible();
    expect.soft(await page.evaluate(() => Boolean(document.activeElement.closest('#phase-review'))), 'Navigation moves focus into the review pane').toBe(true);
    await page.keyboard.press('Tab');
    expect.soft(await page.evaluate(() => Boolean(document.activeElement.closest('#phase-review'))), 'Next Tab remains in review content').toBe(true);
    const after = await currentOrder(page);
    expect(after.revision).toBe(generated.revision);
    expect(after.confirmation.status).not.toBe('confirmed');
    expect(writes).toEqual([]);
    expect(downloads).toEqual([]);
    await returnToChat(page);
    await expect(page.locator('#message-input')).toHaveValue('尚未发送的补充内容');
  });

  test(`keyboard option selection keeps a usable focus after its real response at ${width}px`, async ({page, appURL}) => {
    await page.setViewportSize({width, height:844});
    await boot(page, appURL);
    await send(page, card);
    await openCompare(page);
    const chosen = await posted(page, '/api/choose', () => keyboardActivate(page.locator('.choose-option').first(), 'Space'));
    expect(chosen.selectedOption).toBeTruthy();
    await expect(page.locator('#request-options')).toBeEnabled();
    await settled(page);
    expect(await page.evaluate(() => document.activeElement !== document.body && Boolean(document.activeElement.closest('#phase-compare'))), 'A rerendered option must not drop keyboard focus to body').toBe(true);
  });

  for (const userAction of ['change-view', 'change-focus']) {
    test(`delayed option response respects user ${userAction} at ${width}px`, async ({page, appURL}) => {
      await page.setViewportSize({width, height:844});
      await boot(page, appURL);
      await send(page, card);
      await openCompare(page);
      const received = gate(), release = gate();
      let realSnapshot;
      await page.route('**/api/choose', async route => {
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        realSnapshot = await response.json();
        received.release();
        await release.promise;
        await route.fulfill({response});
      });
      try {
        await keyboardActivate(page.locator('.choose-option').first(), 'Space');
        await received.promise;
        const back = page.locator('#phase-compare [data-view="intake"]');
        if (userAction === 'change-view') {
          await keyboardActivate(back);
          await page.locator('#sidebar-toggle').focus();
        } else await back.focus();
        const focused = userAction === 'change-view' ? page.locator('#sidebar-toggle') : back;
        await expect(focused).toBeFocused();
        const view = await page.locator('#workspace').getAttribute('data-view');
        const arrived = page.waitForResponse(response => new URL(response.url()).pathname === '/api/choose');
        release.release();
        await arrived;
        await expect(page.locator('#message-input')).toBeEnabled();
        await settled(page);
        await expect(page.locator('#workspace')).toHaveAttribute('data-view', view);
        await expect(focused).toBeFocused();
        const saved = await currentOrder(page);
        expect(saved.selectedOption).toBe(realSnapshot.selectedOption);
        expect(saved.revision).toBe(realSnapshot.revision);
      } finally {release.release();}
    });
  }

  test(`native platform control has a real 44px border box at ${width}px`, async ({page, appURL}) => {
    await page.setViewportSize({width, height:844});
    await boot(page, appURL);
    const before = await currentOrder(page);
    await showOrder(page);
    const select = page.locator('#platform-select');
    await select.scrollIntoViewIfNeeded();
    const box = await select.boundingBox();
    expect(box).toBeTruthy();
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect((await currentOrder(page)).revision).toBe(before.revision);
  });
}

test('响应到达时正在编辑草稿字段，焦点与选区不丢', async ({page, appURL}) => {
  await page.setViewportSize({width:1440, height:844});
  await boot(page, appURL);
  await send(page, card);
  await showOrder(page);
  const input = page.locator('.field-input[data-field-path="purpose"]');
  // 保存一次用途，让重绘后的输入框持有服务端值，选区才有意义。
  const saved = page.waitForResponse(r => new URL(r.url()).pathname === '/api/fields');
  await input.fill('线下门店推广');
  await input.press('Tab');
  expect((await saved).status()).toBe(200);
  await expect(input).toHaveValue('线下门店推广');
  await expect(input).toHaveAttribute('data-field-key', 'order:purpose');
  // 平台切换的真实响应被闸住；响应到达前用户正带着选区编辑这个草稿字段。
  const received = gate(), release = gate();
  await page.route('**/api/platform', async route => {
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    received.release();
    await release.promise;
    await route.fulfill({response});
  });
  try {
    const arrived = page.waitForResponse(r => new URL(r.url()).pathname === '/api/platform');
    await page.locator('#platform-select').selectOption('shengda');
    await received.promise;
    await input.focus();
    await input.evaluate(node => node.setSelectionRange(2, 5));
    await expect(input).toBeFocused();
    expect(await input.evaluate(node => [node.selectionStart, node.selectionEnd])).toEqual([2, 5]);
    release.release();
    await arrived;
    await settled(page);
    const after = await page.locator('.field-input[data-field-path="purpose"]').evaluate(node => ({
      focused: document.activeElement === node,
      start: node.selectionStart,
      end: node.selectionEnd,
      value: node.value,
    }));
    expect(after.focused, '重绘后焦点必须回到正在编辑的草稿字段').toBe(true);
    expect([after.start, after.end], '重绘后选区必须保持').toEqual([2, 5]);
    expect(after.value).toBe('线下门店推广');
  } finally { release.release(); }
});

for (const direction of ['backward', 'forward']) {
  test(`mobile sidebar cycles ${direction} through visible controls when native details are closed`, async ({page, appURL}) => {
    await page.setViewportSize({width:390, height:844});
    await boot(page, appURL);
    await openSidebar(page);
    for (const selector of ['.catalog-panel', '.tool-panel', '#run-panel']) {
      expect(await page.locator(selector).evaluate(node => node.open)).toBe(false);
    }
    const first = page.locator('#reset-order');
    const last = page.locator('#settings-button');
    if (direction === 'backward') {
      await first.focus();
      await page.keyboard.press('Shift+Tab');
      await expect(last).toBeFocused();
    } else {
      await last.focus();
      await page.keyboard.press('Tab');
      await expect(first).toBeFocused();
    }
    expect(await page.evaluate(() => Boolean(document.activeElement.closest('.sidebar')))).toBe(true);
    const active = await page.evaluate(() => ({tag:document.activeElement.tagName, id:document.activeElement.id}));
    expect(active.tag).not.toBe('BODY');
  });
}
