/** Harness UI browser checks: turn capsules, inline candidate gates, interactive stepper, slash commands, Ctrl+K palette, calm onboarding & inline locate. */
import {expect} from '@playwright/test';
import {test, boot, send, currentOrder, openSidebar} from './journey.fixture.mjs';

const fullCard = '500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内';

test('assistant turn renders collapsible run capsule and inline candidate approval gate', async ({page, appURL}) => {
  await boot(page, appURL);
  const pending = await send(page, '大约1k张名片');
  expect(pending.canonicalOrder.fields.quantity.status).toBe('pending_confirmation');

  const capsule = page.locator('#chat-feed .harness-capsule').last();
  await expect(capsule).toBeVisible();
  await expect(capsule).not.toHaveAttribute('open', '');
  await capsule.locator('summary').click();
  await expect(capsule).toHaveAttribute('open', '');
  await expect(capsule.locator('.harness-trace-item').first()).toBeVisible();
  await expect(capsule.locator('.harness-capsule-foot code')).toContainText('run:');

  const gate = page.locator('#chat-feed .harness-gate-row[data-gate-field-id="quantity"]').last();
  await expect(gate).toBeVisible();
  await expect(gate).toContainText('1000 张');

  const confirmedResp = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/fields' && r.request().method() === 'POST');
  await gate.locator('.harness-gate-btn.is-confirm').click();
  const after = await (await confirmedResp).json();
  expect(after.canonicalOrder.fields.quantity.status).toBe('confirmed');
  expect(after.canonicalOrder.fields.quantity.value).toBe('1000 张');
  await expect(gate).toHaveClass(/is-resolved/);
  await expect(gate).toContainText('已写入规格');

  await page.reload();
  expect((await currentOrder(page)).canonicalOrder.fields.quantity.status).toBe('confirmed');
});

test('interactive sidebar stepper switches phases view-only, preserves draft, and closes mobile drawer', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, fullCard);
  const draft = '未发送草稿：等我对比完方案再决定';
  await page.locator('#message-input').fill(draft);

  const posts = [];
  page.on('request', (req) => { if (req.method() === 'POST') posts.push(req.url()); });

  await page.locator('#step-list .step[data-view="compare"]').click();
  await expect(page.locator('#phase-compare')).toBeVisible();
  await expect(page.locator('#step-list .step[data-view="compare"]')).toHaveAttribute('aria-current', 'page');

  await page.locator('#step-list .step[data-view="review"]').click();
  await expect(page.locator('#phase-review')).toBeVisible();
  await expect(page.locator('#step-list .step[data-view="review"]')).toHaveAttribute('aria-current', 'page');

  await page.locator('#step-list .step[data-view="intake"]').click();
  await expect(page.locator('#phase-intake')).toBeVisible();
  await expect(page.locator('#message-input')).toHaveValue(draft);
  expect(posts).toEqual([]);

  // Mobile drawer auto-closes after picking a stage from sidebar
  await page.setViewportSize({width: 390, height: 844});
  await openSidebar(page);
  await page.locator('#step-list .step[data-view="compare"]').click();
  await expect(page.locator('#phase-compare')).toBeVisible();
  await expect(page.locator('#workspace-backdrop')).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('composer slash command popover executes tools without posting raw slash text to chat', async ({page, appURL}) => {
  await boot(page, appURL);
  await send(page, fullCard);

  let chatPosts = 0;
  page.on('request', (req) => {
    if (new URL(req.url()).pathname === '/api/chat' && req.method() === 'POST') chatPosts += 1;
  });

  const input = page.locator('#message-input');
  await input.fill('/估');
  const menu = page.locator('#slash-menu');
  await expect(menu).toBeVisible();
  await expect(menu.locator('.slash-menu-item.is-active')).toContainText('/估价');

  // IME composing Enter must not trigger command execution
  await input.dispatchEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true});
  await expect(menu).toBeVisible();

  const toolCall = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/tools/call' && r.request().method() === 'POST');
  await input.press('Enter');
  const result = await (await toolCall).json();
  expect(result.toolResult.type).toBe('estimate');
  await expect(menu).toBeHidden();
  await expect(input).toHaveValue('');
  expect(chatPosts).toBe(0);

  // Escape dismisses slash menu cleanly
  await input.fill('/');
  await expect(menu).toBeVisible();
  await input.press('Escape');
  await expect(menu).toBeHidden();
});

test('Ctrl+K command palette filters and switches orders and workspace toggles', async ({page, appURL}) => {
  await boot(page, appURL);
  const first = await send(page, fullCard);

  // Create a second distinct order
  await page.locator('#reset-order').click();
  const created = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/session' && r.request().method() === 'POST');
  await page.locator('#confirm-reset-ok').click();
  await created;
  const second = await send(page, '200张单页，210×297mm，157g铜版纸');
  expect(second.sessionId).not.toBe(first.sessionId);

  // Open command palette and jump back to first order
  await page.keyboard.press('Control+k');
  const palette = page.locator('#command-palette');
  await expect(palette).toBeVisible();
  const search = page.locator('#command-palette-input');
  await search.fill(first.sessionId.slice(0, 6));
  const switched = page.waitForResponse((r) => new URL(r.url()).pathname === `/api/orders/${first.sessionId}` && r.request().method() === 'GET');
  await search.press('Enter');
  expect((await switched).status()).toBe(200);
  await expect(palette).toBeHidden();
  expect((await currentOrder(page)).sessionId).toBe(first.sessionId);

  // Toggle high contrast via palette
  await page.keyboard.press('Control+k');
  await expect(palette).toBeVisible();
  await search.fill('高对比');
  await search.press('Enter');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'high-contrast');
});

test('blank order shows calm onboarding and desktop locate focuses inline draft with rich undo banner', async ({page, appURL}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await boot(page, appURL);

  const riskPanel = page.locator('#risk-panel');
  await expect(riskPanel).toBeVisible();
  await expect(riskPanel).toHaveClass(/is-onboarding/);
  await expect(riskPanel.locator('.risk-missing')).toHaveCount(0);
  await expect(page.locator('#risk-count')).toHaveText('准备就绪');

  // After sending partial requirements, risk panel becomes standard active checklist
  await send(page, '做500张名片');
  await expect(riskPanel).not.toHaveClass(/is-onboarding/);
  await expect(riskPanel.locator('.risk-section-heading').first()).toBeVisible();

  // Clicking [去处理] on a simple missing field keeps user in intake and focuses inline draft input
  const firstAction = riskPanel.locator('.risk-missing .issue-action').first();
  await firstAction.click();
  await expect(page.locator('#phase-intake')).toBeVisible();
  const focusedInOrder = await page.evaluate(() => Boolean(document.activeElement?.closest('#order-panel')));
  expect(focusedInOrder).toBe(true);

  // Saving an inline edit renders the structured Diff & Undo banner and undo restores value
  const qtyInput = page.locator('#order-panel .field-input[data-field-path="quantity"]');
  const saved = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/fields' && r.request().method() === 'POST');
  await qtyInput.fill('800 张');
  await qtyInput.press('Tab');
  expect((await saved).status()).toBe(200);

  const notice = page.locator('#change-notice');
  await expect(notice).toBeVisible();
  await expect(notice).toHaveClass(/is-undo-banner/);
  await expect(notice).toContainText('500 张 → 800 张');

  const undone = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/fields' && r.request().method() === 'POST');
  await notice.getByRole('button', {name: '撤销本次修改'}).click();
  const undoneBody = await (await undone).json();
  expect(undoneBody.order.quantity).toBe('500 张');
});
