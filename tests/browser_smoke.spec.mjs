/** Real browser checks using synthetic orders and an isolated Python server. */
import {expect} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {test, boot, send, currentOrder, expand, openOrder, openCompare, openHandoff, openTools, openSidebar, openHistoryMenu, reviewInput, returnToChat, switchHistory} from './journey.fixture.mjs';
const field = (page, path) => page.locator(`.review-field[data-field-id="${path}"]`);

const syntheticCard = '我要印1200张名片，成品尺寸9×5.4cm，250g铜版纸，双面四色，无特殊工艺，无需装订';

test('history restores the active order after reload and survives cleared browser storage', async ({page, appURL}) => {
  await boot(page, appURL);
  const before = await send(page, syntheticCard);
  await expect(page.locator(`.history-item[data-session-id="${before.sessionId}"]`)).toBeVisible();
  await page.reload();
  await expect(page.locator('#chat-feed')).toContainText('已恢复上次订单记忆');
  await expect(await reviewInput(page,'quantity')).toHaveValue('1200 张');
  const restored = await currentOrder(page);
  expect(restored.revision).toBe(before.revision);
  expect(restored.order).toEqual(before.order);
  expect(restored.canonicalOrder).toEqual(before.canonicalOrder);
  expect((await currentOrder(page)).revision).toBe(restored.revision);
  // Remove every browser cache key; history must still come from SQLite.
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const historyItem = page.locator(`.history-item[data-session-id="${before.sessionId}"]`);
  await expect(historyItem).toBeVisible();
  await switchHistory(page,before.sessionId);
  await expect(await reviewInput(page,'quantity')).toHaveValue('1200 张');
  expect((await currentOrder(page)).sessionId).toBe(before.sessionId);
  await expect(page.locator('#chat-feed')).toContainText(syntheticCard);
});

test('history actions share a right-click, keyboard, and touch menu', async ({page, appURL}) => {
  await boot(page, appURL);
  const created = await send(page, syntheticCard);
  const item = page.locator(`.history-item[data-session-id="${created.sessionId}"]`);
  const trigger = page.locator(`[data-history-menu="${created.sessionId}"]`);
  const menu = page.locator('#history-context-menu');
  await item.click({button: 'right'});
  await expect(menu).toBeVisible();
  await expect(menu.locator('[data-history-menu-action="export"]')).toHaveCount(1);
  await expect(menu.locator('[data-history-menu-action="archive"]')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await item.focus();
  await item.press('Shift+F10');
  await expect(menu).toBeVisible();
  await page.keyboard.press('ArrowDown');
  await expect(menu.locator('[data-history-menu-action="archive"]')).toBeFocused();
  await page.keyboard.press('Escape');
  await trigger.click({button: 'right'});
  await expect(menu).toBeVisible();
  await page.locator('#workspace-title').click();
  await expect(menu).toBeHidden();
  await page.setViewportSize({width: 390, height: 844});
  await openSidebar(page);
  await trigger.click({button: 'right'});
  await expect(menu).toBeVisible();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
});

test('history titles stay compact and reveal right-aligned hover actions', async ({page, appURL}) => {
  await boot(page, appURL);
  const created = await send(page, syntheticCard);
  const item = page.locator(`.history-item[data-session-id="${created.sessionId}"]`);
  const row = item.locator('xpath=..');
  const actions = row.locator('.history-hover-actions');
  await expect(item.locator('strong')).toHaveText('名片 · 1200 张');
  await item.hover();
  await expect(actions).toBeVisible();
  await expect.poll(async () => Number(await actions.evaluate((element) => getComputedStyle(element).opacity))).toBe(1);
  await expect(actions.locator('[data-history-action="export"]')).toBeVisible();
  await expect(actions.locator('[data-history-action="archive"]')).toBeVisible();
  const itemBox = await item.boundingBox();
  const actionBox = await actions.boundingBox();
  expect(actionBox.x).toBeGreaterThanOrEqual(itemBox.x + itemBox.width - actionBox.width - 2);
  await item.focus();
  await expect(actions).toBeVisible();
  const heading = page.locator('.current-order-heading');
  const reset = page.locator('#reset-order');
  const headingBox = await heading.boundingBox();
  const resetBox = await reset.boundingBox();
  expect(resetBox.x + resetBox.width).toBeCloseTo(headingBox.x + headingBox.width, 0);
  expect(resetBox.y + resetBox.height / 2).toBeCloseTo(headingBox.y + headingBox.height / 2, 0);
});

test('risk checklist uses a compact hierarchy with actions aligned to the right', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, syntheticCard);
  const panel = page.locator('#risk-panel');
  await expect(panel).toBeVisible();
  await expect(panel.locator('.risk-section-heading')).toHaveCount(2);
  const metrics = await panel.evaluate((element) => ({
    heading: Number.parseFloat(getComputedStyle(element.querySelector('.risk-heading h3')).fontSize),
    item: Number.parseFloat(getComputedStyle(element.querySelector('.risk-item strong')).fontSize),
    actionHeight: Number.parseFloat(getComputedStyle(element.querySelector('.issue-action')).minHeight),
    actionAlign: getComputedStyle(element.querySelector('.risk-item')).gridTemplateColumns
  }));
  expect(metrics.heading).toBeLessThanOrEqual(14);
  expect(metrics.item).toBeLessThanOrEqual(12);
  expect(metrics.actionHeight).toBeLessThanOrEqual(36);
  expect(Number.parseFloat(metrics.actionAlign.split(' ').at(-1))).toBeGreaterThan(0);
});

test('settings consolidates low-frequency sidebar controls without hiding integrations', async ({page, appURL}, info) => {
  await boot(page, appURL);
  await openSidebar(page);
  await expect(page.locator('#settings-button')).toHaveAccessibleName(/^设置与能力/);
  await expect(page.locator('.sidebar-settings')).toHaveCount(0);
  await expect(page.locator('.current-order-heading .settings-launcher-compact')).toHaveCount(1);
  await expect(page.locator('.sidebar .catalog-panel, .sidebar .tool-panel')).toHaveCount(0);
  await page.locator('#settings-button').click();
  await expect(page.locator('#settings-dialog')).toBeVisible();
  const fixedDialog = await page.locator('#settings-dialog').boundingBox();
  await expect(page.locator('#settings-panel-overview')).toBeVisible();
  await expect(page.locator('.integration-grid article')).toHaveCount(6);
  const overviewShot = info.outputPath('settings-overview.png');
  await page.screenshot({path: overviewShot, animations: 'disabled'});
  await page.locator('[data-settings-view="products"]').click();
  await expect(page.locator('#settings-panel-products')).toBeVisible();
  const productsDialog = await page.locator('#settings-dialog').boundingBox();
  expect(productsDialog.width).toBe(fixedDialog.width);
  expect(productsDialog.height).toBe(fixedDialog.height);
  await expect(page.locator('#settings-panel-products .catalog-panel')).toHaveAttribute('open', '');
  await expect(page.locator('#catalog-list .catalog-chip').first()).toBeVisible();
  await page.locator('[data-settings-view="history"]').click();
  await expect(page.locator('#settings-panel-history')).toBeVisible();
  await expect(page.locator('#history-include-archived')).toBeVisible();
  const historyDialog = await page.locator('#settings-dialog').boundingBox();
  expect(historyDialog.width).toBe(fixedDialog.width);
  expect(historyDialog.height).toBe(fixedDialog.height);
  await page.locator('[data-settings-view="tools"]').click();
  const toolsDialog = await page.locator('#settings-dialog').boundingBox();
  expect(toolsDialog.width).toBe(fixedDialog.width);
  expect(toolsDialog.height).toBe(fixedDialog.height);
  await expect(page.locator('#settings-panel-tools .tool-panel')).toHaveAttribute('open', '');
  await expect(page.locator('#tool-list .tool-item')).toHaveCount(7);
  await page.locator('[data-settings-view="appearance"]').click();
  await page.locator('#theme-toggle').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'high-contrast');
  const settingsShot = info.outputPath('settings-panel.png');
  await page.screenshot({path: settingsShot, animations: 'disabled'});
  await info.attach('settings-panel.png', {path: settingsShot, contentType: 'image/png'});
  await page.keyboard.press('Escape');
  await expect(page.locator('#settings-dialog')).toBeHidden();
  await page.setViewportSize({width: 390, height: 844});
  await openSidebar(page);
  await page.locator('#settings-button').click();
  const dialogBox = await page.locator('#settings-dialog').boundingBox();
  expect(dialogBox.width).toBeLessThanOrEqual(390);
  expect(dialogBox.height).toBeLessThanOrEqual(812);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.keyboard.press('Escape');
});

test('chat converts dimensions to mm and manual dimensions reject cm', async ({page, appURL}) => {
  await boot(page, appURL);
  const result = await send(page, '500份折页，成品尺寸21×29.7cm，展开尺寸42×29.7cm');
  expect(result.order.dimensions.finishedSize).toBe('210×297 mm');
  expect(result.order.dimensions.expandedSize).toBe('420×297 mm');
  expect(result.canonicalOrder.unitSystem).toBe('mm');
  const size = await reviewInput(page, 'dimensions.finishedSize');
  await expect(size).toHaveValue('210×297 mm');
  await expect(await reviewInput(page,'dimensions.expandedSize')).toHaveValue('420×297 mm');
  await size.fill('21×29.7cm');
  await field(page,'dimensions.finishedSize').getByRole('button',{name:'保存修改'}).click();
  await expect(page.locator('#toast')).toContainText('手工尺寸请使用 mm');
  expect((await currentOrder(page)).order.dimensions.finishedSize).toBe('210×297 mm');
  await size.fill('148×210');
  const response = page.waitForResponse((r) => r.url().endsWith('/api/fields'));
  await field(page,'dimensions.finishedSize').getByRole('button',{name:'保存修改'}).click();
  expect((await response).status()).toBe(200);
  await expect(size).toHaveValue('148×210 mm');
  expect((await currentOrder(page)).order.dimensions.expandedSize).toBe('420×297 mm');
});

test('price line items expand and retain illustrative provenance', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, syntheticCard);
  await openTools(page);
  const response = page.waitForResponse((r) => r.url().endsWith('/api/tools/call'));
  await page.getByRole('button', {name: 'PR 估算费用 生成非正式价格区间'}).click();
  const result = await (await response).json();
  expect(result.toolResult.type).toBe('estimate');
  expect(result.toolResult.lineItems.length).toBeGreaterThan(0);
  await openCompare(page);
  const details = page.locator('#estimate-panel .price-details');
  await expect(details).toBeVisible();
  await expect(details).not.toHaveAttribute('open', '');
  await details.locator('summary').click();
  await expect(details).toHaveAttribute('open', '');
  await expect(details.locator('dl > div')).toHaveCount(result.toolResult.lineItems.length);
  for (const line of result.toolResult.lineItems) await expect(details).toContainText(`¥${line.amount}`);
  await expect(details).toContainText('示例参数');
  await expect(details).toContainText(/不构成(?:供应商)?报价/);
});

test('supplier selection visibly reports the unconfigured local-draft state', async ({page, appURL}) => {
  await boot(page, appURL);
  await openOrder(page);
  await expect(page.locator('#provider-status')).toContainText('仅导出 · 无网络请求');
  const platforms = await page.locator('#platform-select option').evaluateAll((nodes) => nodes.map((node) => ({value: node.value, label: node.textContent})));
  expect(platforms.length).toBeGreaterThan(1);
  const unconfigured = platforms.filter((item) => !['generic', 'mock'].includes(item.value));
  expect(unconfigured.length).toBeGreaterThan(0);
  for (const platform of unconfigured) {
    expect(platform.label).toContain('未配置');
    const response = page.waitForResponse((r) => r.url().endsWith('/api/platform'));
    await page.locator('#platform-select').selectOption(platform.value);
    expect((await response).status()).toBe(200);
    await expect(page.locator('#provider-status')).toContainText('未配置 · 仅生成本地草稿');
    await expect(page.locator('#provider-status')).toContainText('生产能力、价格与交期均待供应商确认');
  }
  const sandbox = platforms.find((item) => item.value === 'mock');
  if (sandbox) {
    expect(sandbox.label).toContain('沙箱数据');
    const response = page.waitForResponse((r) => r.url().endsWith('/api/platform'));
    await page.locator('#platform-select').selectOption(sandbox.value);
    expect((await response).status()).toBe(200);
    await expect(page.locator('#provider-status')).toContainText('沙箱数据 · 不产生真实订单');
  }
});

test('dangerous HTML is inert in chat and persisted field rendering', async ({page, appURL}) => {
  await boot(page, appURL);
  const payload = '<img src=x onerror="window.__printopsXss=1"><script>window.__printopsXss=2</script>';
  await send(page, `合成安全测试：${payload}`);
  await expect(page.locator('#chat-feed')).toContainText(payload);
  await send(page, syntheticCard);
  const purpose = await reviewInput(page, 'paper');
  await purpose.fill(payload);
  const response = page.waitForResponse((r) => r.url().endsWith('/api/fields'));
  await field(page,'paper').getByRole('button',{name:'保存修改'}).click();
  expect((await response).status()).toBe(200);
  await page.reload();
  await expect(await reviewInput(page, 'paper')).toHaveValue(payload);
  expect(await page.evaluate(() => window.__printopsXss)).toBeUndefined();
  await expect(page.locator('#chat-feed script, #chat-feed img[onerror], #review-checklist script, #review-checklist img[onerror]')).toHaveCount(0);
});

test('low-confidence quantity requires the visible confirmation button and persists confirmation', async ({page, appURL}) => {
  await boot(page, appURL);
  const before = await send(page, '大约1k张名片');
  expect(before.canonicalOrder.fields.quantity.status).toBe('pending_confirmation');
  expect(before.canonicalOrder.fields.quantity.value).toBeNull();
  await openHandoff(page);
  const row = field(page,'quantity');
  await expect(row).toContainText('待确认');
  const response = page.waitForResponse((r) => r.url().endsWith('/api/fields'));
  await row.getByRole('button', {name: '确认候选值'}).click();
  const after = await (await response).json();
  expect(after.canonicalOrder.fields.quantity.status).toBe('confirmed');
  expect(after.canonicalOrder.fields.quantity.value).toBe('1000 张');
  expect(after.fieldMeta.quantity.source).toBe('user');
  expect(after.fieldMeta.quantity.confidence).toBe(1);
  await expect(row.getByRole('button', {name: '确认候选值'})).toHaveCount(0);
  await page.reload();
  await expect(row).toContainText('已确认');
  expect((await currentOrder(page)).canonicalOrder.fields.quantity.status).toBe('confirmed');
});

test('history downloads a complete backup and archives an order without preventing a new order', async ({page, appURL}, testInfo) => {
  await boot(page, appURL);
  const created = await send(page, syntheticCard);
  const downloadBackup = async (label) => {
    await openHistoryMenu(page, created.sessionId);
    const pending = page.waitForEvent('download');
    await page.locator('#history-context-menu [data-history-menu-action="export"]').click();
    const download = await pending;
    expect(download.suggestedFilename()).toBe(`printops-backup-${created.sessionId}.json`);
    const path = testInfo.outputPath(`${label}.json`);
    await download.saveAs(path);
    const backup = JSON.parse(await readFile(path, 'utf8'));
    await testInfo.attach(label, {path, contentType: 'application/json'});
    expect(backup.schemaVersion).toBe('2.0');
    expect(backup.orderId).toBe(created.sessionId);
    expect(backup.state.order.quantity).toBe('1200 张');
    expect(backup.canonicalOrder.unitSystem).toBe('mm');
    expect(backup.messages.some((message) => message.text === syntheticCard)).toBe(true);
    expect(backup.events.length).toBeGreaterThan(0);
    expect(backup.auditLog.at(-1).action).toBe('exported');
    expect(Array.isArray(backup.quoteRequests)).toBe(true);
    return backup;
  };
  const original = await downloadBackup('active-order-backup');
  expect((await currentOrder(page)).revision).toBe(original.revision);
  await openHistoryMenu(page, created.sessionId);
  const archiveResponse = page.waitForResponse((r) => r.url().endsWith(`/api/orders/${created.sessionId}/archive`));
  const dialogEvent = page.waitForEvent('dialog');
  const click = page.locator('#history-context-menu [data-history-menu-action="archive"]').click();
  const dialog = await dialogEvent;
  expect(dialog.type()).toBe('confirm');
  await dialog.accept();
  await click;
  expect((await archiveResponse).status()).toBe(200);
  await expect(page.locator(`.history-item[data-session-id="${created.sessionId}"]`)).toHaveCount(0);
  await expect(page.locator('#message-input')).toBeDisabled();
  await expect(await reviewInput(page,'quantity')).toBeDisabled();
  await expect(page.locator('#drop-zone')).toBeDisabled();
  await page.locator('#settings-button').click();
  await page.locator('[data-settings-view="history"]').click();
  await expect(page.locator('#settings-panel-history')).toBeVisible();
  await page.locator('#history-include-archived').check();
  await expect(page.locator(`.history-item[data-session-id="${created.sessionId}"]`)).toContainText('已归档');
  await page.locator('#history-include-archived').uncheck();
  await expect(page.locator(`.history-item[data-session-id="${created.sessionId}"]`)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await page.locator('#settings-button').click();
  await page.locator('[data-settings-view="history"]').click();
  await page.locator('#history-include-archived').check();
  await expect(page.locator(`.history-item[data-session-id="${created.sessionId}"]`)).toContainText('已归档');
  await page.keyboard.press('Escape');
  await openHistoryMenu(page, created.sessionId);
  await expect(page.locator('#history-context-menu [data-history-menu-action="archive"]')).toHaveCount(0);
  await expect(page.locator('#history-context-menu [data-history-menu-action="purge"]')).toHaveCount(1);
  await page.keyboard.press('Escape');
  const archived = await downloadBackup('archived-order-backup');
  expect(archived.status).toBe('archived');
  await testInfo.attach('archived-readonly.png', {body: await page.screenshot({fullPage: true}), contentType: 'image/png'});
  await openHistoryMenu(page, created.sessionId);
  const purgeResponse = page.waitForResponse((r) => r.url().endsWith(`/api/orders/${created.sessionId}/purge`));
  const purgeDialogEvent = page.waitForEvent('dialog');
  const purgeClick = page.locator('#history-context-menu [data-history-menu-action="purge"]').click();
  const purgeDialog = await purgeDialogEvent;
  expect(purgeDialog.type()).toBe('confirm');
  await purgeDialog.accept();
  await purgeClick;
  expect((await purgeResponse).status()).toBe(200);
  await expect(page.locator(`.history-item[data-session-id="${created.sessionId}"]`)).toHaveCount(0);
  await openSidebar(page);
  await page.locator('#reset-order').click();
  await page.locator('#confirm-reset-ok').click();
  await expect(page.locator('#message-input')).toBeEnabled();
  await expect(page.locator('#drop-zone')).toBeEnabled();
  const next = await send(page, '合成新订单：500张名片');
  expect(next.sessionId).not.toBe(created.sessionId);
  expect(next.order.quantity).toBe('500 张');
  await openCompare(page);
  await expect(page.locator('#request-options')).toBeEnabled();
  await expect(page.locator('#drop-zone')).toBeEnabled();
});

test('390px layout does not overflow horizontally and navigation opens and closes', async ({page, appURL}, testInfo) => {
  await page.setViewportSize({width: 390, height: 844});
  await boot(page, appURL);
  const viewportWidths = () => page.evaluate(() => ({
    viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth,
  }));
  const assertFits = async () => {
    const dimensions = await viewportWidths();
    expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
    expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport);
  };
  await assertFits();
  await expect(page.locator('#sidebar-toggle')).toHaveAttribute('aria-label', '展开导航栏');
  await page.locator('#sidebar-toggle').click();
  await expect(page.locator('.sidebar')).toBeVisible();
  await expect(page.locator('#workspace-backdrop')).toBeVisible();
  const bounds = await page.locator('.sidebar').boundingBox();
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  await assertFits();
  await testInfo.attach('narrow-navigation.png', {body: await page.screenshot(), contentType: 'image/png'});
  await page.keyboard.press('Escape');
  await expect(page.locator('#sidebar-toggle')).toHaveAttribute('aria-label', '展开导航栏');
  await expect(page.locator('#workspace-backdrop')).toBeHidden();
  await send(page, syntheticCard);
  await assertFits();
});
