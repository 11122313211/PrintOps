/** Synthetic end-to-end journeys against a fresh local SQLite HTTP server. */
import {expect} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {test, boot, send, currentOrder, expand, openOrder, openCompare, openHandoff, openTools, reviewInput, returnToChat} from './journey.fixture.mjs';

const cardText = '500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内';
const pdfFixture = fileURLToPath(new URL('fixtures/synthetic-a4-print.pdf', import.meta.url));
const fieldRow = (page, path) => page.locator(`.review-field[data-field-id="${path}"]`);
async function clickWrite(page, path, action, status = 200) {
  const pending = page.waitForResponse(r => new URL(r.url()).pathname === path && r.request().method() === 'POST');
  await action();
  const response = await pending;
  expect(response.status()).toBe(status);
  return response.json();
}
async function openReview(page) { await openHandoff(page); await expand(page, '#review-checklist-section'); }
async function setReview(page, path, value) {
  await openReview(page);
  await expand(page, '#review-checklist-section');
  const row = fieldRow(page, path);
  await expand(page, `.review-field[data-field-id="${path}"] .review-editor`);
  await row.locator('input').fill(value);
  return clickWrite(page, '/api/fields', () => row.getByRole('button', {name: '保存修改'}).click());
}
async function price(page) {
  await openTools(page);
  const result = await clickWrite(page, '/api/tools/call', () => page.getByRole('button', {name: 'PR 估算费用 生成非正式价格区间'}).click());
  await openCompare(page);
  return result;
}
async function httpWrite(page, path, body) {
  const token = await page.evaluate(() => window.__PRINTOPS_TOKEN__);
  const response = await page.request.post(new URL(path, page.url()).href,
    {headers:{'X-PrintOps-Token':token}, data:body});
  return {status:response.status(), body:await response.json()};
}

test('first visit has one direct composer and typing preserves the chosen order-pane preference', async ({page, appURL}) => {
  await boot(page, appURL);
  const before = await currentOrder(page);
  await expect(page.locator('.journey-tabs')).toHaveCount(0);
  await expect(page.locator('#message-input')).toHaveCount(1);
  await expect(page.locator('#starter-panel')).toBeVisible();
  await expect(page.locator('[data-example]')).toHaveCount(3);
  await expect(page.locator('.task-bar')).toBeHidden();
  await expect(page.locator('#order-toggle')).toHaveAttribute('data-collapsed','false');
  await page.locator('#order-toggle').click();
  await expect(page.locator('#order-toggle')).toHaveAttribute('data-collapsed','true');
  await page.locator('#message-input').fill(cardText);
  const after = await currentOrder(page);
  expect(after.order).toEqual(before.order);
  expect(after.revision).toBe(before.revision);
  await expect(page.locator('#order-toggle')).toHaveAttribute('data-collapsed','true');
  await send(page, cardText);
  await expect(page.locator('#order-toggle')).toHaveAttribute('data-collapsed','true');
  await page.reload();
  await expect(page.locator('#order-toggle')).toHaveAttribute('data-collapsed','true');
  expect((await currentOrder(page)).order.quantity).toBe('500 张');
  await expect(await reviewInput(page,'quantity')).toHaveValue('500 张');
});

test('one product reaches authoritative confirmed Markdown and JSON with the same revision', async ({page, appURL}, info) => {
  await boot(page, appURL);
  await send(page, cardText);
  await openCompare(page);
  await expect(page.locator('.option-card')).toHaveCount(3);
  const chosen = await clickWrite(page, '/api/choose', () => page.locator('.choose-option').first().click());
  expect(chosen.selectedOption).toBeTruthy();
  await openHandoff(page);
  const generated = await clickWrite(page, '/api/generate', () => page.locator('#generate-order').click());
  expect(generated.handoffDocument.exportable).toBe(true);
  expect(generated.handoffDocument.status).not.toBe('confirmed');
  await expect(page.locator('#handoff-preview')).toContainText('尚未发送');
  const confirmed = await clickWrite(page, '/api/confirm', () => page.locator('#confirm-order').click());
  expect(confirmed.confirmation.status).toBe('confirmed');
  expect(confirmed.handoffDocument.confirmedRevision).toBe(confirmed.revision);
  const authoritative = confirmed.handoffDocument;
  for (const [format, extension] of [['json','json'], ['markdown','md']]) {
    const downloadEvent = page.waitForEvent('download');
    await page.locator(`.export-option[data-format="${format}"]`).click();
    const download = await downloadEvent;
    expect(download.suggestedFilename()).toBe(`printops-handoff-${confirmed.sessionId}-r${confirmed.revision}.${extension}`);
    const path = info.outputPath(`authoritative-handoff.${extension}`);
    await download.saveAs(path);
    const text = await readFile(path, 'utf8');
    if (format === 'json') {
      const document = JSON.parse(text);
      expect(document.revision).toBe(authoritative.revision);
      expect(document.confirmedRevision).toBe(authoritative.confirmedRevision);
      expect(document.items).toEqual(authoritative.items);
      expect(document.sent).toBe(false);
    } else expect(text).toBe(authoritative.markdown);
    await info.attach(`confirmed-${format}`, {path, contentType: format === 'json' ? 'application/json' : 'text/markdown'});
  }
  const changed = await setReview(page, 'quantity', '700 张');
  expect(changed.confirmation.status).not.toBe('confirmed');
  expect(changed.orderGenerated).toBe(false);
  await expect(page.locator('#order-export')).toBeHidden();
  await expect(page.locator('#handoff-preview')).toContainText('尚未就绪');
});

test('candidate confirmation, clear and undo preserve explicit field decisions after reload', async ({page, appURL}) => {
  await boot(page, appURL);
  const pending = await send(page, '大约1k张名片');
  expect(pending.canonicalOrder.fields.quantity.value).toBeNull();
  await openReview(page);
  const row = fieldRow(page, 'quantity');
  await expect(row).toContainText('待确认');
  const confirmed = await clickWrite(page, '/api/fields', () => row.getByRole('button', {name:'确认候选值'}).click());
  expect(confirmed.canonicalOrder.fields.quantity.value).toBe('1000 张');
  await expand(page, '.review-field[data-field-id="quantity"] .review-editor');
  const cleared = await clickWrite(page, '/api/fields', () => row.getByRole('button', {name:'清空此字段'}).click());
  expect(cleared.canonicalOrder.fields.quantity.value).toBeNull();
  await expect(row).toContainText('未填写');
  const undone = await clickWrite(page, '/api/fields', () => page.getByRole('button', {name:'撤销本次修改'}).click());
  expect(undone.revision).toBeGreaterThan(cleared.revision);
  expect(undone.canonicalOrder.fields.quantity.value).toBe('1000 张');
  await page.reload();
  expect((await currentOrder(page)).canonicalOrder.fields.quantity.value).toBe('1000 张');
});

test('rejecting a candidate keeps it out of production specifications', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, '大约1k张名片');
  await openReview(page);
  const rejected = await clickWrite(page, '/api/fields', () => fieldRow(page, 'quantity').getByRole('button', {name:'拒绝候选'}).click());
  expect(rejected.canonicalOrder.fields.quantity.value).toBeNull();
  await expect(fieldRow(page, 'quantity')).toContainText('未填写');
  await expect(fieldRow(page, 'quantity').getByRole('button', {name:'确认候选值'})).toHaveCount(0);
  await page.reload();
  expect((await currentOrder(page)).canonicalOrder.fields.quantity.value).toBeNull();
});

for (const itemCount of [2, 3]) test(`${itemCount} product focus changes no business messages and subsequent edits carry the intended item`, async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, '500张名片，90×54mm；200张单页，210×297mm' + (itemCount === 3 ? '；20张海报，420×594mm' : ''));
  expect(initial.order.items).toHaveLength(itemCount);
  const targetIndex = itemCount - 1;
  const before = await currentOrder(page);
  await openOrder(page);
  const second = page.locator('.multi-product-item').nth(targetIndex);
  const focused = await clickWrite(page, '/api/focus', () => second.locator('.multi-product-focus').click());
  expect(focused.activeItemId).toBe(initial.order.items[targetIndex].itemId);
  const afterFocus = await currentOrder(page);
  expect(afterFocus.messages).toEqual(before.messages);
  expect(afterFocus.revision).toBe(before.revision);
  await expect(page.locator('#active-item-context')).toContainText(`第 ${itemCount} 项`);
  const request = page.waitForRequest(r => r.url().endsWith('/api/chat') && r.method() === 'POST');
  const changed = await send(page, '数量改为300张');
  expect((await request).postDataJSON().itemId).toBe(initial.order.items[targetIndex].itemId);
  expect(changed.order.items[0].quantity).toBe('500 张');
  expect(changed.order.items[targetIndex].quantity).toBe('300 张');
  if (itemCount === 3) expect(changed.order.items[1].quantity).toBe('200 张');
});

test('a real second-client write causes 409 and requires explicit comparison before reapply', async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, cardText);
  const competing = await httpWrite(page, '/api/fields', {sessionId:initial.sessionId, expectedRevision:initial.revision, action:'set', values:{quantity:'600 张'}});
  expect(competing.status).toBe(200);
  const input = await reviewInput(page, 'quantity');
  await input.fill('700 张');
  await clickWrite(page, '/api/fields', () => fieldRow(page,'quantity').getByRole('button',{name:'保存修改'}).click(), 409);
  await expect(page.locator('#operation-error')).toBeVisible();
  await expect(page.locator('#save-status')).toContainText('版本冲突');
  await expect(input).toHaveValue('700 张');
  await page.getByRole('button', {name:'读取最新状态'}).click();
  await expect(page.locator('#operation-error-diff')).toContainText('当前 600 张；本次输入 700 张');
  expect((await currentOrder(page)).order.quantity).toBe('600 张');
  const applied = await clickWrite(page, '/api/fields', () => page.getByRole('button', {name:'核对后重新应用'}).click());
  expect(applied.order.quantity).toBe('700 张');
  await expect(page.locator('#operation-error')).toBeHidden();
  await expect(page.locator('#save-status')).toContainText('已保存');
});

test('lost response after a committed write is recovered by reading without replay', async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, cardText);
  let writes = 0;
  await page.route('**/api/fields', async route => {
    writes++;
    await route.fetch(); // The real server commits; the browser loses the receipt.
    await route.abort('failed');
  });
  const input = await reviewInput(page, 'quantity');
  await input.fill('650 张');
  await fieldRow(page,'quantity').getByRole('button',{name:'保存修改'}).click();
  await expect(page.locator('#operation-error-title')).toContainText('保存结果尚未确认');
  expect(writes).toBe(1);
  await page.unroute('**/api/fields');
  await page.getByRole('button', {name:'读取最新状态'}).click();
  await expect(page.locator('#operation-error-message')).toContainText('已读取服务器最新状态');
  await expect(input).toHaveValue('650 张');
  await expect(page.getByRole('button', {name:'核对后重新应用'})).toHaveCount(0);
  const saved = await currentOrder(page);
  expect(saved.order.quantity).toBe('650 张');
  expect(saved.revision).toBeGreaterThan(initial.revision);
  expect(writes).toBe(1);
});

test('keyboard expansion of recommendation reasons never selects an option', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, cardText);
  await openCompare(page);
  const before = await currentOrder(page);
  const card = page.locator('.option-card').first();
  const toggle = card.getByRole('button', {name:'展开推荐理由'});
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(card.locator('.option-details')).toBeVisible();
  await expect(card.locator('.choose-option')).toHaveAttribute('aria-pressed','false');
  await page.keyboard.press('Space');
  await expect(card.locator('.option-details')).toBeHidden();
  expect((await currentOrder(page)).selectedOption).toBe(before.selectedOption);
  expect((await currentOrder(page)).revision).toBe(before.revision);
});

test('ready, partial and blocked estimates are distinguishable and stale totals cannot look current', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, cardText);
  const ready = await price(page);
  expect(ready.toolResult.status).toBe('ready');
  await expect(page.locator('#estimate-panel')).toContainText('示例');
  await setReview(page, 'finishing', '镭射膜');
  await openCompare(page);
  // The old total must either disappear or be explicitly labelled stale.
  const panel = page.locator('#estimate-panel');
  if (await panel.isVisible()) await expect(panel).toContainText(/过期|失效|重新/);
  const partial = await price(page);
  expect(partial.toolResult.status).toBe('partial');
  expect(partial.toolResult.range).toBeNull();
  await expect(panel).toContainText(/部分|小计/);
  await expect(panel).toContainText('镭射膜');
  await setReview(page, 'paper', '铜版纸');
  const blocked = await price(page);
  expect(blocked.toolResult.status).toBe('blocked');
  expect(blocked.toolResult.range).toBeNull();
  await expect(panel).toContainText(/暂不能|待补充|缺少/);
});

test('uploaded PDF check survives reload and becomes visibly stale after a specification change', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, '500张单页，成品210×297mm，157g铜版纸，单面四色，无特殊工艺，下周内');
  const uploaded = await clickWrite(page, '/api/preflight', () => page.locator('#file-input').setInputFiles(pdfFixture));
  expect(uploaded.uploadedFile).toBe('synthetic-a4-print.pdf');
  expect(uploaded.preflightResults.fileName).toBe('synthetic-a4-print.pdf');
  expect(uploaded.preflightResults.stale).toBe(false);
  await openOrder(page);
  await expect(page.locator('#file-state')).toContainText('所属产品：单页');
  // 预检卡片的值（如 MediaBox "209.9×297.0 mm"）不得溢出卡片压到相邻列。
  await expect(page.locator('.file-check').first()).toBeVisible();
  const overflowRight = await page.locator('.file-check').evaluateAll(cards => cards.map(card => {
    const value = card.querySelector('strong');
    if (!value) return 0;
    return value.getBoundingClientRect().right - card.getBoundingClientRect().right;
  }));
  expect(Math.max(...overflowRight, 0)).toBeLessThanOrEqual(0.5);
  await page.reload();
  await expect(page.locator('#file-state')).toContainText('synthetic-a4-print.pdf');
  const restored = await currentOrder(page);
  expect(restored.preflightResults).toEqual(uploaded.preflightResults);
  const changed = await setReview(page, 'dimensions.finishedSize', '148×210 mm');
  expect(changed.preflightResults.stale).toBe(true);
  await expect(page.locator('#file-state')).toContainText('原基础检查已过期');
  await expect(page.locator('#file-state')).not.toContainText('基础检查已通过');
  await page.reload();
  await expect(page.locator('#file-state')).toContainText('原基础检查已过期');
  expect((await currentOrder(page)).preflightResults.stale).toBe(true);
});

test('switching products during PDF reading binds the file to its original item and preserves current focus', async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, '500张单页，210×297mm；200张名片，90×54mm');
  expect(initial.order.items).toHaveLength(2);
  const first = initial.order.items[0].itemId, second = initial.order.items[1].itemId;
  await openOrder(page);
  await clickWrite(page, '/api/focus', () => page.locator('.multi-product-item').first().locator('.multi-product-focus').click());
  await expect(page.locator('#active-item-context')).toContainText('第 1 项');
  // Controlled browser I/O delay models a file still being read, without a large file or sleep.
  await page.evaluate(() => {
    const original = File.prototype.arrayBuffer;
    File.prototype.arrayBuffer = async function () {
      const buffer = await original.call(this);
      window.__fileReadStarted = true;
      await new Promise(resolve => {window.__releaseFileRead = resolve;});
      return buffer;
    };
  });
  await page.locator('#file-input').setInputFiles(pdfFixture);
  await page.waitForFunction(() => window.__fileReadStarted === true);
  const secondItem = page.locator('.multi-product-item').nth(1);
  if (!await secondItem.evaluate(element => element.open)) await secondItem.locator(':scope > summary').click();
  await clickWrite(page, '/api/focus', () => secondItem.locator('.multi-product-focus').click());
  await expect(page.locator('#active-item-context')).toContainText('第 2 项');
  const sent = page.waitForRequest(r => r.url().endsWith('/api/preflight') && r.method() === 'POST');
  const rebound = page.waitForResponse(r => r.url().endsWith('/api/focus') && r.request().method() === 'POST');
  await page.evaluate(() => window.__releaseFileRead());
  expect((await sent).postDataJSON().itemId).toBe(first);
  const focused = await (await rebound).json();
  expect(focused.activeItemId).toBe(second);
  expect(focused.order.items[0].uploadedFile).toBe('synthetic-a4-print.pdf');
  expect(focused.order.items[1].uploadedFile).toBeFalsy();
  expect(focused.preflightResults[first].itemId).toBe(first);
  expect(focused.preflightResults[second]).toBeUndefined();
  await expect(page.locator('#active-item-context')).toContainText('第 2 项');
  await expect(page.locator('#file-state')).toBeHidden();
});

test('an upload without a selected product is blocked before reading or creating a request', async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, '500张单页，210×297mm；200张名片，90×54mm');
  expect(initial.activeItemId).toBeNull();
  await expect(page.locator('#active-item-context')).toContainText('请先选择处理项');
  let preflights = 0;
  page.on('request', request => {if (request.url().endsWith('/api/preflight')) preflights++;});
  await page.evaluate(() => {
    File.prototype.arrayBuffer = () => {window.__unexpectedFileRead = true; throw new Error('read before explicit scope');};
  });
  await page.locator('#file-input').setInputFiles(pdfFixture);
  await expect(page.locator('#toast')).toContainText('先在订单摘要中选择');
  expect(await page.evaluate(() => window.__unexpectedFileRead)).toBeUndefined();
  const saved = await currentOrder(page);
  expect(saved.order.items.every(item => !item.uploadedFile)).toBe(true);
  expect(saved.revision).toBe(initial.revision);
  expect(preflights).toBe(0);
});

for (const width of [390, 768, 1280, 640]) {
  test(`layout at ${width}px${width === 640 ? ' (1280px at 200% reflow equivalent, not actual browser zoom)' : ''}`, async ({page, appURL}, info) => {
    await page.setViewportSize({width, height:900});
    await boot(page, appURL);
    await send(page, cardText);
    const fits = async () => {
      const size = await page.evaluate(() => ({viewport:innerWidth, document:document.documentElement.scrollWidth, body:document.body.scrollWidth}));
      expect(size.document).toBeLessThanOrEqual(size.viewport);
      expect(size.body).toBeLessThanOrEqual(size.viewport);
    };
    await fits();
    await openCompare(page);
    await expect(page.locator('.choose-option').first()).toBeVisible();
    await fits();
    await returnToChat(page);
    await expect(page.locator('#message-input')).toBeVisible();
    if (width < 1051) {
      await openOrder(page);
      await expect(page.locator('#workspace')).toHaveAttribute('data-mobile-pane','order');
      await expect(page.locator('#drop-zone')).toBeVisible();
      await fits();
      await page.locator('#mobile-back').click();
      await expect(page.locator('#workspace')).toHaveAttribute('data-mobile-pane','work');
      await expect(page.locator('#message-input')).toBeVisible();
    }
    await openReview(page);
    await expect(await reviewInput(page,'quantity')).toBeVisible();
    await fits();
    await info.attach(`layout-${width}.png`, {body:await page.screenshot({fullPage:true}),contentType:'image/png'});
  });
}
