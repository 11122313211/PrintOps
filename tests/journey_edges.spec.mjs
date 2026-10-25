/** User-visible concurrency, draft retention and recovery boundaries. */
import {expect} from '@playwright/test';
import {readFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {test, boot, send, currentOrder, expand, openOrder, openCompare, openHandoff, openTools, openHistoryMenu, reviewInput, returnToChat, switchHistory} from './journey.fixture.mjs';

const card = '500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内';
const multi = '500张名片，90×54mm，300g铜版纸，双面四色；200张单页，210×297mm，157g铜版纸，单面四色';
const row = (page, path) => page.locator(`.review-field[data-field-id="${path}"]`);
async function posted(page, path, act, status = 200) {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === path && r.request().method() === 'POST');
  await act();
  const result = await response;
  expect(result.status()).toBe(status);
  return result.json();
}
async function writeAsOtherClient(page, path, body) {
  const token = await page.evaluate(() => window.__PRINTOPS_TOKEN__);
  const response = await page.request.post(new URL(path, page.url()).href, {headers:{'X-PrintOps-Token':token},data:body});
  expect(response.status()).toBe(200);
  return response.json();
}
async function focus(page, index) {
  await openOrder(page);
  const item = page.locator('.multi-product-item').nth(index);
  return posted(page, '/api/focus', () => item.locator('.multi-product-focus').click());
}
async function review(page) {
  await openHandoff(page);
  await expand(page, '#review-checklist-section');
}
async function settledFrame(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
function gate() {
  let release;
  const promise = new Promise(resolve => {release = resolve;});
  return {promise, release};
}

test('a delayed tool response from order A cannot overwrite the opened order B', async ({page, appURL}) => {
  await boot(page, appURL);
  const a = await send(page, card);
  const emptyB = await writeAsOtherClient(page, '/api/session', {});
  const b = await writeAsOtherClient(page, '/api/chat', {sessionId:emptyB.sessionId,expectedRevision:emptyB.revision,text:'800张单页，210×297mm'});
  await page.reload();
  await expect(page.locator(`.history-item[data-session-id="${b.sessionId}"]`)).toBeVisible();
  const received = gate(), release = gate();
  await page.route('**/api/tools/call', async route => {
    const response = await route.fetch();
    received.release();
    await release.promise;
    await route.fulfill({response});
  });
  await openTools(page);
  await page.getByRole('button',{name:'PR 估算费用 生成非正式价格区间'}).click();
  await received.promise;
  await switchHistory(page,b.sessionId);
  await expect(await reviewInput(page,'quantity')).toHaveValue('800 张');
  const arrived = page.waitForResponse(r => r.url().endsWith('/api/tools/call'));
  release.release();
  await arrived;
  await settledFrame(page);
  await expect(page.locator('#message-input')).toBeEnabled();
  expect(await page.evaluate(() => localStorage.getItem('printops_session'))).toBe(b.sessionId);
  await expect(await reviewInput(page,'quantity')).toHaveValue('800 张');
  expect((await currentOrder(page)).sessionId).not.toBe(a.sessionId);
  await openCompare(page);
  await expect(page.locator('#estimate-panel')).toBeHidden();
});

for (const action of ['generate','confirm']) test(`a delayed ${action} response cannot replace another order or reopen its handoff`, async ({page,appURL}) => {
  await boot(page,appURL);
  const a = await send(page,card);
  await openCompare(page);
  await posted(page,'/api/choose',() => page.locator('.choose-option').first().click());
  await openHandoff(page);
  if (action === 'confirm') await posted(page,'/api/generate',() => page.locator('#generate-order').click());
  const empty = await writeAsOtherClient(page,'/api/session',{});
  const b = await writeAsOtherClient(page,'/api/chat',{sessionId:empty.sessionId,expectedRevision:empty.revision,text:'800张单页，210×297mm'});
  await page.reload();
  await openHandoff(page);
  const received = gate(), release = gate();
  await page.route(`**/api/${action}`,async route => {
    const response = await route.fetch();
    received.release();
    await release.promise;
    await route.fulfill({response});
  });
  try {
    await page.locator(`#${action}-order`).click();
    await received.promise;
    await switchHistory(page,b.sessionId);
    await expect(await reviewInput(page,'quantity')).toHaveValue('800 张');
    const view = await page.locator('#workspace').getAttribute('data-view');
    const arrived = page.waitForResponse(response => response.url().endsWith(`/api/${action}`));
    release.release();
    await arrived;
    await settledFrame(page);
    await expect(page.locator('#message-input')).toBeEnabled();
    await expect(page.locator('#workspace')).toHaveAttribute('data-view',view);
    await expect(page.locator('.review-field[data-field-id="quantity"] input')).toHaveValue('800 张');
    const current = await currentOrder(page);
    expect(current.sessionId).toBe(b.sessionId);
    expect(current.revision).toBe(b.revision);
    expect(current.confirmation.status).not.toBe('confirmed');
    await expect(page.locator('#handoff-preview')).not.toContainText(a.sessionId);
  } finally { release.release(); }
});

test('out-of-order focus responses at the same revision keep the last requested product', async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, multi);
  const received = gate(), release = gate();
  await page.route('**/api/focus', async route => {
    if (route.request().postDataJSON().itemId !== initial.order.items[0].itemId) return route.continue();
    const response = await route.fetch();
    received.release();
    await release.promise;
    await route.fulfill({response});
  });
  await openOrder(page);
  await page.locator('.multi-product-item').first().locator('.multi-product-focus').click();
  await received.promise;
  const last = await focus(page, 1);
  expect(last.revision).toBe(initial.revision);
  await expect(page.locator('#active-item-context')).toContainText('第 2 项');
  const arrived = page.waitForResponse(r => r.url().endsWith('/api/focus') && r.request().postDataJSON().itemId === initial.order.items[0].itemId);
  release.release();
  await arrived;
  await settledFrame(page);
  await expect(page.locator('#active-item-context')).toContainText('第 2 项');
  const request = page.waitForRequest(r => r.url().endsWith('/api/chat'));
  const changed = await send(page, '数量改为320张');
  expect((await request).postDataJSON().itemId).toBe(initial.order.items[1].itemId);
  expect(changed.order.items[0].quantity).toBe('500 张');
  expect(changed.order.items[1].quantity).toBe('320 张');
});

test('an estimate for one item never appears as the current estimate of another item', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, multi);
  await focus(page, 0);
  await openTools(page);
  const estimated = await posted(page, '/api/tools/call', () => page.getByRole('button',{name:'PR 估算费用 生成非正式价格区间'}).click());
  expect(estimated.toolResult.status).toBe('ready');
  await openCompare(page);
  await expect(page.locator('#estimate-panel')).toBeVisible();
  await focus(page, 1);
  await expect(page.locator('#active-item-context')).toContainText('第 2 项');
  await openCompare(page);
  await expect(page.locator('#estimate-panel')).toBeHidden();
});

test('unsaved review edits survive phase changes and saving another field', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, card);
  await review(page);
  await expand(page, '.review-field[data-field-id="quantity"] .review-editor');
  await row(page,'quantity').locator('input').fill('777 张');
  await openCompare(page);
  await review(page);
  await expand(page, '.review-field[data-field-id="quantity"] .review-editor');
  await expect(row(page,'quantity').locator('input')).toHaveValue('777 张');
  await expand(page, '.review-field[data-field-id="paper"] .review-editor');
  await row(page,'paper').locator('input').fill('350 g/m² 铜版纸');
  await posted(page, '/api/fields', () => row(page,'paper').getByRole('button',{name:'保存修改'}).click());
  await expand(page, '.review-field[data-field-id="quantity"] .review-editor');
  await expect(row(page,'quantity').locator('input')).toHaveValue('777 张');
  expect((await currentOrder(page)).order.quantity).toBe('500 张');
  const saved = await posted(page, '/api/fields', () => row(page,'quantity').getByRole('button',{name:'保存修改'}).click());
  expect(saved.order.quantity).toBe('777 张');
});

test('archived review and option controls remain read-only after phase navigation', async ({page, appURL}) => {
  await boot(page, appURL);
  const current = await send(page, card);
  const empty = await writeAsOtherClient(page, '/api/session', {});
  const writable = await writeAsOtherClient(page, '/api/chat', {sessionId:empty.sessionId,expectedRevision:empty.revision,text:'800张单页，210×297mm，157g铜版纸，单面四色，无特殊工艺，下周内'});
  await page.reload();
  page.once('dialog', dialog => dialog.accept());
  await openHistoryMenu(page, current.sessionId);
  await posted(page, `/api/orders/${current.sessionId}/archive`, () => page.locator('#history-context-menu [data-history-menu-action="archive"]').click());
  await openCompare(page);
  await expect(page.locator('#request-options')).toBeDisabled();
  await review(page);
  await expand(page, '.review-field[data-field-id="quantity"] .review-editor');
  await expect(row(page,'quantity').locator('input')).toBeDisabled();
  await expect(row(page,'quantity').getByRole('button',{name:'保存修改'})).toBeDisabled();
  await returnToChat(page);
  await expect(page.locator('#message-input')).toBeDisabled();
  await switchHistory(page,writable.sessionId);
  await expect(page.locator('#message-input')).toBeEnabled();
  await openCompare(page);
  await expect(page.locator('#request-options')).toBeEnabled();
  const recommended = await posted(page, '/api/tools/call', () => page.locator('#request-options').click());
  expect(recommended.order.quantity).toBe('800 张');
  await openOrder(page);
  await expect(page.locator('#drop-zone')).toBeEnabled();
});

test('the settings history panel lists every order and purges archived ones for good', async ({page, appURL}) => {
  await boot(page, appURL);
  // The worker-scoped server keeps its database across tests, so archived
  // leftovers from earlier cases are purged up front; the panel now lists all
  // orders, so assertions are scoped by session id instead of list length.
  const token = await page.evaluate(() => window.__PRINTOPS_TOKEN__);
  const leftovers = await page.request.get(new URL('/api/orders?includeArchived=true&status=archived', page.url()).href, {headers:{'X-PrintOps-Token':token}});
  for (const entry of (await leftovers.json()).orders) {
    const purged = await page.request.post(new URL(`/api/orders/${entry.orderId}/purge`, page.url()).href, {headers:{'X-PrintOps-Token':token}, data:{expectedRevision: entry.revision, confirm: true}});
    expect(purged.status()).toBe(200);
  }
  const current = await send(page, card);
  page.once('dialog', dialog => dialog.accept());
  await openHistoryMenu(page, current.sessionId);
  await posted(page, `/api/orders/${current.sessionId}/archive`, () => page.locator('#history-context-menu [data-history-menu-action="archive"]').click());
  // The archived order is hidden from the sidebar by default but must be
  // manageable from the settings history panel.
  await expect(page.locator(`.history-item[data-session-id="${current.sessionId}"]`)).toHaveCount(0);
  await page.locator('#settings-button').click();
  await page.locator('[data-settings-view="history"]').click();
  const row = (id) => page.locator('.settings-history-row').filter({has: page.locator(`.settings-history-view[data-order-id="${id}"]`)});
  await expect(row(current.sessionId)).toBeVisible();
  await expect(row(current.sessionId).getByText('已归档')).toBeVisible();
  await expect(row(current.sessionId).getByRole('button', {name: '永久删除'})).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await posted(page, `/api/orders/${current.sessionId}/purge`, () => row(current.sessionId).getByRole('button', {name: '永久删除'}).click());
  await expect(row(current.sessionId)).toHaveCount(0);
  await page.keyboard.press('Escape');
  const list = await page.request.get(new URL('/api/orders?includeArchived=true', page.url()).href, {headers:{'X-PrintOps-Token':token}});
  expect((await list.json()).orders.map((entry) => entry.orderId)).not.toContain(current.sessionId);
});

test('bulk selection permanently deletes several orders with a single confirm', async ({page, appURL}) => {
  await boot(page, appURL);
  const token = await page.evaluate(() => window.__PRINTOPS_TOKEN__);
  const leftovers = await page.request.get(new URL('/api/orders?includeArchived=true&status=archived', page.url()).href, {headers:{'X-PrintOps-Token':token}});
  for (const entry of (await leftovers.json()).orders) {
    await page.request.post(new URL(`/api/orders/${entry.orderId}/purge`, page.url()).href, {headers:{'X-PrintOps-Token':token}, data:{expectedRevision: entry.revision, confirm: true}});
  }
  const a = await send(page, card);
  const empty = await writeAsOtherClient(page, '/api/session', {});
  const b = await writeAsOtherClient(page, '/api/chat', {sessionId: empty.sessionId, expectedRevision: empty.revision, text: '800张单页，210×297mm'});
  await page.locator('#settings-button').click();
  await page.locator('[data-settings-view="history"]').click();
  const row = (id) => page.locator('.settings-history-row').filter({has: page.locator(`.settings-history-view[data-order-id="${id}"]`)});
  await expect(row(a.sessionId)).toBeVisible();
  await expect(row(b.sessionId)).toBeVisible();
  const bulkDelete = page.locator('[data-history-bulk="delete"]');
  await expect(bulkDelete).toBeDisabled();
  await row(a.sessionId).locator('.settings-history-check').check();
  await row(b.sessionId).locator('.settings-history-check').check();
  await expect(page.locator('#settings-history-selection-count')).toHaveText('已选 2');
  await expect(bulkDelete).toBeEnabled();
  const dialogs = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.accept(); });
  await bulkDelete.click();
  await expect(row(a.sessionId)).toHaveCount(0);
  await expect(row(b.sessionId)).toHaveCount(0);
  await expect(page.locator('#settings-history-selection-count')).toHaveText('已选 0');
  expect(dialogs).toHaveLength(1);
  expect(dialogs[0]).toContain('2 个订单');
  await page.keyboard.press('Escape');
  const list = await page.request.get(new URL('/api/orders?includeArchived=true', page.url()).href, {headers:{'X-PrintOps-Token':token}});
  const remaining = (await list.json()).orders.map((entry) => entry.orderId);
  expect(remaining).not.toContain(a.sessionId);
  expect(remaining).not.toContain(b.sessionId);
  await expect(page.locator(`.history-item[data-session-id="${a.sessionId}"]`)).toHaveCount(0);
});

test('spec presets collapse to one row and pull out as a drawer on demand', async ({page, appURL}) => {
  await boot(page, appURL);
  const drawer = page.locator('#preset-drawer');
  await expect(drawer).toBeHidden();
  await expect(page.locator('#preset-toggle')).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('#save-preset')).toBeDisabled();
  // The compact row is docked to the sidebar's bottom edge and stays there
  // no matter how the scrolling history list above it changes.
  const dockedAtBottom = () => page.evaluate(() => {
    const panel = document.querySelector('.preset-panel').getBoundingClientRect();
    const sidebar = document.querySelector('.sidebar').getBoundingClientRect();
    const list = document.querySelector('#history-list');
    const before = list.scrollTop;
    list.scrollTop = before + 40;
    const after = document.querySelector('.preset-panel').getBoundingClientRect();
    list.scrollTop = before;
    return Math.abs(panel.bottom - sidebar.bottom) < 1.5 && Math.abs(after.bottom - panel.bottom) < 1.5;
  });
  expect(await dockedAtBottom()).toBe(true);
  await send(page, card);
  await expect(page.locator('#save-preset')).toBeEnabled();
  await page.locator('#save-preset').click();
  // Saving opens the drawer so the flash on the new entry is visible feedback.
  await expect(drawer).toBeVisible();
  // Expanding takes space inside the left column (the docked footer grows),
  // never floating outside the sidebar.
  const inColumn = await page.evaluate(() => {
    const d = document.querySelector('#preset-drawer').getBoundingClientRect();
    const s = document.querySelector('.sidebar').getBoundingClientRect();
    return d.left >= s.left - 0.5 && d.right <= s.right + 0.5 && d.top >= s.top && d.bottom <= s.bottom + 1;
  });
  expect(inColumn).toBe(true);
  await expect(page.locator('#preset-toggle')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#preset-count')).toHaveText('1');
  await expect(drawer.locator('.preset-item.is-new')).toHaveCount(1);
  const applied = page.waitForResponse(r => r.url().endsWith('/api/chat') && r.request().method() === 'POST');
  await drawer.locator('.preset-apply').click();
  await applied;
  await expect(drawer).toBeHidden();
  await page.locator('#preset-toggle').click();
  await expect(drawer).toBeVisible();
  await drawer.locator('.preset-remove').click();
  await expect(drawer.locator('.preset-item')).toHaveCount(0);
  await expect(drawer.locator('.preset-empty')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
});

for (const [action, label] of [['confirm','确认候选值'],['reject','拒绝候选']]) test(`a stale ${action} action cannot be replayed against a changed candidate`, async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, '大约1k张名片');
  await review(page);
  await writeAsOtherClient(page, '/api/chat', {sessionId:initial.sessionId,expectedRevision:initial.revision,text:'大约2k张'});
  await posted(page, '/api/fields', () => row(page,'quantity').getByRole('button',{name:label}).click(),409);
  await page.getByRole('button',{name:'读取最新状态'}).click();
  await expect(page.locator('#operation-error-message')).toContainText('已读取');
  await expect(page.getByRole('button',{name:'核对后重新应用'})).toHaveCount(0);
  const saved = await currentOrder(page);
  expect(saved.canonicalOrder.fields.quantity.value).toBeNull();
  expect(saved.review.fields.find(field => field.fieldPath === 'quantity').candidateValue).toBe('2000 张');
});

test('a stale clear action shows the latest value and proposed empty value before reapply', async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, card);
  await review(page);
  await expand(page,'.review-field[data-field-id="quantity"] .review-editor');
  await writeAsOtherClient(page, '/api/fields', {sessionId:initial.sessionId,expectedRevision:initial.revision,action:'set',values:{quantity:'650 张'}});
  await posted(page, '/api/fields', () => row(page,'quantity').getByRole('button',{name:'清空此字段'}).click(),409);
  await page.getByRole('button',{name:'读取最新状态'}).click();
  await expect(page.locator('#operation-error-diff')).toContainText('650 张');
  await expect(page.locator('#operation-error-diff')).toContainText('清空');
  expect((await currentOrder(page)).order.quantity).toBe('650 张');
});

test('credential reload preserves the failed message input without submitting it', async ({page, appURL}) => {
  await boot(page, appURL);
  const initial = await send(page, card);
  const draft = '数量改为900张，交期请保留';
  await page.route('**/api/chat', route => route.fulfill({status:403,contentType:'application/json',body:JSON.stringify({code:'FORBIDDEN',message:'本地访问凭据失效（合成故障）'})}), {times:1});
  await page.locator('#message-input').fill(draft);
  await posted(page,'/api/chat',() => page.locator('#message-form button[type="submit"]').click(),403);
  await expect(page.locator('#message-input')).toHaveValue(draft);
  await page.getByRole('button',{name:'重新加载凭据'}).click();
  await expect(page.locator('#message-input')).toBeEnabled();
  await expect(page.locator('#message-input')).toHaveValue(draft);
  const unchanged = await currentOrder(page);
  expect(unchanged.revision).toBe(initial.revision);
  expect(unchanged.order.quantity).toBe('500 张');
});

test('supplemental: a lost receipt remains blocked only on its original order until read recovery', async ({page, appURL}) => {
  await boot(page, appURL);
  const a = await send(page, card);
  const emptyB = await writeAsOtherClient(page, '/api/session', {});
  const b = await writeAsOtherClient(page, '/api/chat', {sessionId:emptyB.sessionId,expectedRevision:emptyB.revision,text:'800张单页，210×297mm'});
  await page.reload();
  await expect(page.locator(`.history-item[data-session-id="${b.sessionId}"]`)).toBeVisible();
  const received = gate(), release = gate();
  let writes = 0;
  await page.route('**/api/tools/call', async route => {
    writes++;
    await route.fetch();
    received.release();
    await release.promise;
    await route.abort('failed');
  });
  try {
    await openTools(page);
    await page.getByRole('button',{name:'PR 估算费用 生成非正式价格区间'}).click();
    await received.promise;
    await switchHistory(page,b.sessionId);
    await expect(await reviewInput(page,'quantity')).toHaveValue('800 张');
    const failed = page.waitForEvent('requestfailed', request => request.url().endsWith('/api/tools/call'));
    release.release();
    await failed;
    await settledFrame(page);
    await expect(page.locator('#operation-error')).toBeHidden();
    await expect(page.locator('#message-input')).toBeEnabled();
    const unchangedB = await currentOrder(page);
    expect(unchangedB.sessionId).toBe(b.sessionId);
    expect(unchangedB.revision).toBe(b.revision);
    await switchHistory(page,a.sessionId);
    await expect(page.locator('#operation-error-title')).toContainText('保存结果尚未确认');
    const beforeAttempt = await currentOrder(page);
    let chatRequests = 0;
    page.on('request', request => {
      if (request.url().endsWith('/api/chat') && request.method() === 'POST') chatRequests++;
    });
    const draft = '数量改为900张';
    await page.locator('#message-input').fill(draft);
    await page.locator('#message-form button[type="submit"]').click();
    await expect(page.locator('#agent-status')).toContainText('操作尚未完成');
    await expect(page.locator('#message-input')).toBeEnabled();
    await expect(page.locator('#message-input')).toHaveValue(draft);
    expect(chatRequests).toBe(0);
    expect((await currentOrder(page)).revision).toBe(beforeAttempt.revision);
    await page.getByRole('button',{name:'读取最新状态'}).click();
    await expect(page.locator('#operation-error-message')).toContainText('已读取服务器最新状态');
    await expect(page.locator('#message-input')).toBeEnabled();
    await expect(page.getByRole('button',{name:'核对后重新应用'})).toHaveCount(0);
    expect((await currentOrder(page)).sessionId).toBe(a.sessionId);
    const changed = await posted(page, '/api/chat', () => page.locator('#message-form button[type="submit"]').click());
    expect(changed.order.quantity).toBe('900 张');
    expect(chatRequests).toBe(1);
    expect(writes).toBe(1);
  } finally {
    release.release();
  }
});

test('supplemental: keyboard activation completes one product through confirmed JSON download', async ({page, appURL}, info) => {
  await boot(page, appURL);
  const activate = async (locator, key = 'Enter') => {
    await locator.focus();
    await expect(locator).toBeFocused();
    await locator.press(key);
  };
  await page.locator('#message-input').focus();
  await page.keyboard.insertText(card);
  await posted(page, '/api/chat', () => activate(page.locator('#message-form button[type="submit"]')));
  await expect(page.locator('#message-input')).toBeEnabled();
  await activate(page.locator('.order-document-links [data-view="compare"]'));
  await expect(page.locator('.option-card')).toHaveCount(3);
  const chosen = await posted(page, '/api/choose', () => activate(page.locator('.choose-option').first(), 'Space'));
  expect(chosen.selectedOption).toBeTruthy();
  await activate(page.locator('#phase-compare [data-view="intake"]'));
  await activate(page.locator('.order-document-links [data-view="review"]'));
  const generated = await posted(page, '/api/generate', () => activate(page.locator('#generate-order')));
  expect(generated.handoffDocument.exportable).toBe(true);
  const confirmed = await posted(page, '/api/confirm', () => activate(page.locator('#confirm-order'), 'Space'));
  expect(confirmed.confirmation.status).toBe('confirmed');
  const event = page.waitForEvent('download');
  await activate(page.locator('.export-option[data-format="json"]'));
  const download = await event;
  const path = info.outputPath('keyboard-confirmed-handoff.json');
  await download.saveAs(path);
  const document = JSON.parse(await readFile(path, 'utf8'));
  expect(document.confirmedRevision).toBe(confirmed.revision);
  expect(document.items).toEqual(confirmed.handoffDocument.items);
  expect(document.sent).toBe(false);
  await info.attach('keyboard-confirmed-handoff', {path, contentType:'application/json'});
});

test('csv audit: real downloads preserve authoritative handoff details and protect formula prefixes', async ({page, appURL}, info) => {
  test.setTimeout(60_000);
  await boot(page, appURL);
  await send(page, card);
  await openCompare(page);
  await posted(page, '/api/choose', () => page.locator('.choose-option').first().click());
  const pdf = await readFile(new URL('fixtures/synthetic-a4-print.pdf', import.meta.url));
  const fileNames = ['=SUM(1,2)"合成.pdf', '+SUM(1,2).pdf', '-1+2.pdf', '@SUM(1,2).pdf', ' \t=SUM(1,2).pdf'];
  for (const [index, name] of fileNames.entries()) {
    await returnToChat(page);
    await posted(page, '/api/preflight', () => page.locator('#file-input').setInputFiles({name, mimeType:'application/pdf', buffer:pdf}));
    await openHandoff(page);
    await posted(page, '/api/generate', () => page.locator('#generate-order').click());
    const confirmed = await posted(page, '/api/confirm', () => page.locator('#confirm-order').click());
    const document = confirmed.handoffDocument;
    expect(document.status).toBe('confirmed');
    expect(document.confirmedRevision).toBe(confirmed.revision);
    const event = page.waitForEvent('download');
    await page.locator('.export-option[data-format="csv"]').click();
    const download = await event;
    expect(download.suggestedFilename()).toBe(`printops-handoff-${document.orderId}-r${document.revision}.csv`);
    const path = info.outputPath(`confirmed-handoff-formula-${index}.csv`);
    await download.saveAs(path);
    expect((await readFile(path, 'utf8')).charCodeAt(0)).toBe(0xfeff);
    const rows = JSON.parse(execFileSync(process.env.PRINTOPS_TEST_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), [
      '-c', 'import csv,json,sys; print(json.dumps(list(csv.DictReader(open(sys.argv[1], encoding="utf-8-sig", newline=""))), ensure_ascii=False))', path,
    ], {encoding:'utf8', env:{...process.env, PYTHONIOENCODING:'utf-8'}}));
    expect(rows.length).toBeGreaterThan(20);
    const lookup = (fieldPath, itemId) => {
      const matches = rows.filter(row => row['字段路径'] === fieldPath && (itemId === undefined || row['产品标识'] === itemId));
      expect(matches, `one CSV row for ${fieldPath}`).toHaveLength(1);
      return matches[0];
    };
    for (const key of ['schemaVersion','documentType','orderId','revision','confirmedRevision','confirmedAt','generatedAt','unitSystem']) {
      expect(lookup(key)['内容']).toBe(String(document[key]));
    }
    expect(lookup('status')['内容']).toBe('已本地确认');
    expect(lookup('confirmation.status')['内容']).toBe('已本地确认');
    expect(lookup('confirmation.scope')['内容']).toBe(document.confirmation.scope);
    expect(lookup('sent')['内容']).toBe('尚未发送');
    for (const item of document.items) {
      expect(lookup('itemId', item.itemId)['内容']).toBe(item.itemId);
      for (const field of item.fields) {
        const row = lookup(field.key, item.itemId);
        expect(row['产品项']).toBe(item.title);
        expect(row['字段标识']).toBe(field.fieldId);
        expect(row['内容']).toBe(field.value == null ? '待补充' : String(field.value));
        expect(row['状态']).toBeTruthy();
      }
      expect(lookup('selectedOption', item.itemId)['内容']).toBe(item.selectedOption);
      for (const key of ['id','title','printMode','composition']) {
        expect(lookup(`selectedOptionDetails.${key}`, item.itemId)['内容']).toBe(String(item.selectedOptionDetails[key] ?? '未记录'));
      }
      expect(item.file.checks.length).toBeGreaterThan(0);
      for (const key of ['checkedAt','version','scope','limitations']) {
        expect(lookup(`file.${key}`, item.itemId)['内容']).toBe(String(item.file[key]));
      }
      expect(item.file.status).toBe('checked');
      expect(lookup('file.status', item.itemId)['内容']).toBe('已完成基础检查');
      expect(lookup('file.inspectionLevel', item.itemId)['内容']).toBe(item.file.scope);
      expect(lookup('file.itemId', item.itemId)['内容']).toBe(item.file.itemId ?? '未记录');
      expect(lookup('file.fileName', item.itemId)['内容']).toBe("'" + item.file.fileName);
      for (const [number, check] of item.file.checks.entries()) {
        const row = lookup(`file.checks.${number}`, item.itemId);
        expect(row['字段']).toBe(check.label);
        expect(row['内容']).toBe(String(check.detail ?? '未记录'));
        expect(row['状态']).toBeTruthy();
      }
      for (const [number, warning] of item.file.warnings.entries()) {
        expect(lookup(`file.warnings.${number}`, item.itemId)['内容']).toBe(warning);
      }
    }
    for (const [number, note] of document.notes.entries()) expect(lookup(`notes.${number}`)['内容']).toBe(note);
    for (const row of rows) for (const cell of Object.values(row)) {
      expect(typeof cell).toBe('string');
      expect(cell).not.toMatch(/^[\s\uFEFF]*[=+@-]/);
    }
    await info.attach(`confirmed-csv-${index}`, {path, contentType:'text/csv'});
  }
});

for (const handoffState of ['ready', 'pending', 'confirmed']) test(`control availability: a delayed estimate preserves the ${handoffState} handoff controls of another order`, async ({page, appURL}) => {
  await boot(page, appURL);
  const a = await send(page, card);
  const empty = await writeAsOtherClient(page, '/api/session', {});
  const b = await writeAsOtherClient(page, '/api/chat', {sessionId:empty.sessionId, expectedRevision:empty.revision, text:card});
  const chosen = await writeAsOtherClient(page, '/api/choose', {sessionId:b.sessionId, expectedRevision:b.revision, optionId:b.options[0].id});
  let prepared = chosen;
  if (handoffState !== 'ready') {
    prepared = await writeAsOtherClient(page, '/api/generate', {sessionId:b.sessionId, expectedRevision:chosen.revision});
    expect(prepared.orderGenerated).toBe(true);
    expect(prepared.confirmation.status).not.toBe('confirmed');
  }
  if (handoffState === 'confirmed') prepared = await writeAsOtherClient(page, '/api/confirm', {sessionId:b.sessionId, expectedRevision:prepared.revision});
  await page.reload();
  await expect(page.locator(`.history-item[data-session-id="${b.sessionId}"]`)).toBeVisible();
  expect((await currentOrder(page)).sessionId).toBe(a.sessionId);
  const received = gate(), release = gate();
  await page.route('**/api/tools/call', async route => {
    const response = await route.fetch();
    received.release();
    await release.promise;
    await route.fulfill({response});
  });
  const writes = [], downloads = [];
  try {
    await openTools(page);
    await page.getByRole('button', {name:'PR 估算费用 生成非正式价格区间'}).click();
    await received.promise;
    page.on('request', request => { if (request.method() === 'POST') writes.push(new URL(request.url()).pathname); });
    page.on('download', download => downloads.push(download.suggestedFilename()));
    await switchHistory(page, b.sessionId);
    await openHandoff(page);
    const arrived = page.waitForResponse(response => response.url().endsWith('/api/tools/call'));
    release.release();
    await arrived;
    await settledFrame(page);
    await expect(page.locator('#message-input')).toBeEnabled();
    await expect(page.locator('#next-task-action')).toBeEnabled();
    const current = await currentOrder(page);
    expect(current.sessionId).toBe(b.sessionId);
    expect(current.revision).toBe(prepared.revision);
    expect(current.confirmation.status).toBe(prepared.confirmation.status);
    expect(writes, 'Opening the handoff and receiving an old response must not write the current order').toEqual([]);
    expect(downloads, 'Opening a handoff must not download it').toEqual([]);
    if (handoffState === 'ready') {
      expect(current.orderGenerated).toBe(false);
      await expect(page.locator('#generate-order')).toBeEnabled();
      await expect(page.locator('#next-task-action')).toHaveText('生成交接草稿');
      const requested = page.waitForRequest(request => request.url().endsWith('/api/generate') && request.method() === 'POST');
      const generated = await posted(page, '/api/generate', () => page.locator('#generate-order').click());
      expect((await requested).postDataJSON()).toMatchObject({sessionId:b.sessionId, expectedRevision:prepared.revision});
      expect(generated.orderGenerated).toBe(true);
      expect(generated.handoffDocument.exportable).toBe(true);
      expect(generated.handoffDocument.status).not.toBe('confirmed');
      await expect(page.locator('#confirm-order')).toBeEnabled();
      expect(writes).toEqual(['/api/generate']);
    } else if (handoffState === 'confirmed') {
      await expect(page.locator('#confirm-order')).toBeDisabled();
      await expect(page.locator('#confirm-order')).toHaveText('已确认交接单');
      await expect(page.locator('#next-task-action')).toHaveText('导出交接单');
      expect(current.handoffDocument.confirmedRevision).toBe(prepared.handoffDocument.confirmedRevision);
    } else {
      await expect(page.locator('#confirm-order')).toBeEnabled();
      await expect(page.locator('#next-task-action')).toHaveText('确认交接单');
      const requested = page.waitForRequest(request => request.url().endsWith('/api/confirm') && request.method() === 'POST');
      const result = await posted(page, '/api/confirm', () => page.locator('#next-task-action').click());
      expect((await requested).postDataJSON()).toMatchObject({sessionId:b.sessionId, expectedRevision:prepared.revision});
      expect(result.confirmation.status).toBe('confirmed');
      expect(result.handoffDocument.confirmedRevision).toBe(result.revision);
      await expect(page.locator('#confirm-order')).toBeDisabled();
      expect(writes).toEqual(['/api/confirm']);
    }
  } finally { release.release(); }
});

test('a stopped chat turn frees the composer and still lands with a fresh revision', async ({page, appURL}) => {
  await boot(page, appURL);
  await expect(page.locator('#chat-cancel')).toBeHidden();
  const received = gate(), release = gate();
  await page.route('**/api/chat', async route => {
    const response = await route.fetch();
    received.release();
    await release.promise;
    await route.fulfill({response});
  }, {times: 1});
  try {
    await page.locator('#message-input').fill(card);
    await page.locator('#message-form button[type="submit"]').click();
    await received.promise;
    await expect(page.locator('#chat-cancel')).toBeVisible();
    await expect(page.locator('.pending-message')).toBeVisible();
    await expect(page.locator('#message-input')).toBeDisabled();
    await page.locator('#chat-cancel').click();
    await expect(page.locator('#message-input')).toBeEnabled();
    await expect(page.locator('#message-input')).toBeFocused();
    await expect(page.locator('.pending-message')).toHaveCount(0);
    await expect(page.locator('#chat-cancel')).toBeHidden();
    await expect(page.locator('#operation-error')).toBeHidden();
    await expect(page.locator('#toast')).toContainText('已停止等待');
    // The server committed the held turn while the page stopped waiting.
    const stopped = await currentOrder(page);
    expect(stopped.order.productType).toContain('名片');
    // Releasing the held response renders it late: the reply appears and the
    // local revision catches up, so the next turn carries a fresh
    // expectedRevision instead of failing the server's CAS check.
    release.release();
    await expect(page.locator('#chat-feed .message.assistant')).toHaveCount(2);
    const next = await send(page, '数量改成600张');
    expect(next.order.quantity).toBe('600 张');
    expect(next.revision).toBeGreaterThan(stopped.revision);
  } finally {
    release.release();
  }
});
