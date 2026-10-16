/** Position changes must preserve navigation, unsent input, and order semantics. */
import {expect} from '@playwright/test';
import {test, boot, send, currentOrder, openSidebar, openTools, openCompare, returnToChat} from './journey.fixture.mjs';

const card = '500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内';
async function showOrder(page) {
  if (await page.locator('#settings-dialog[open]').count()) await page.keyboard.press('Escape');
  if (!await page.locator('.order-section').isVisible()) await page.locator('#order-toggle').click();
  await expect(page.locator('.order-section')).toBeVisible();
}
async function inViewport(locator, height) {
  const box = await locator.boundingBox();
  expect(box).toBeTruthy();
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(height);
}

for (const width of [390, 768, 1280, 1440]) {
  test(`fixed entries are reachable and view-only at ${width}px`, async ({page, appURL}, info) => {
    await page.setViewportSize({width, height:900});
    await boot(page, appURL);
    const initial = await send(page, card);
    await expect(page.locator('.task-bar')).toBeHidden();
    const draft = '待发送：数量再考虑一下，先查看已有方案。';
    await page.locator('#message-input').fill(draft);
    const posts = [], downloads = [];
    page.on('request', request => { if (request.method() === 'POST') posts.push(request.url()); });
    page.on('download', download => downloads.push(download.suggestedFilename()));
    for (const phase of ['compare', 'review']) {
      await showOrder(page);
      const control = page.locator(`.order-document-links [data-view="${phase}"]`);
      await inViewport(control, 900);
      await control.click();
      await expect(page.locator(`#phase-${phase}`)).toBeVisible();
      await expect(page.locator('.task-bar')).toBeVisible();
      await page.locator(`#phase-${phase} [data-view="intake"]`).click();
      await expect(page.locator('.task-bar')).toBeHidden();
      await expect(page.locator('#message-input')).toHaveValue(draft);
    }
    const after = await currentOrder(page);
    expect(after.revision).toBe(initial.revision);
    expect(after.order).toEqual(initial.order);
    expect(posts).toEqual([]);
    expect(downloads).toEqual([]);
    const geometry = await page.evaluate(() => {
      const rect = id => document.getElementById(id).getBoundingClientRect().toJSON();
      return {left:rect('sidebar-toggle'), title:rect('workspace-title'), right:rect('order-toggle'), width:innerWidth, scrollWidth:document.documentElement.scrollWidth};
    });
    expect(geometry.left.right).toBeLessThanOrEqual(geometry.title.left);
    expect(geometry.title.right).toBeLessThanOrEqual(geometry.right.left);
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width);
    await info.attach('navigation.png', {body:await page.screenshot({fullPage:true}),contentType:'image/png'});
  });
}

for (const width of [390, 1440]) {
  test(`cancel and Escape retain the order and restore new-order focus at ${width}px`, async ({page, appURL}) => {
    await page.setViewportSize({width,height:844});
    await boot(page, appURL);
    await send(page, card);
    const before = await currentOrder(page);
    await page.locator('#message-input').fill('这段还没有发送');
    await openSidebar(page);
    for (const cancel of ['button', 'escape']) {
      await page.locator('#reset-order').click();
      await expect(page.locator('#confirm-reset-dialog')).toBeVisible();
      for (let index=0; index<4; index++) {
        await page.keyboard.press('Tab');
        // Native dialogs may tab through browser chrome (reported as body),
        // but must never focus any background page control.
        expect(await page.evaluate(() => document.activeElement === document.body || Boolean(document.activeElement.closest('#confirm-reset-dialog')))).toBe(true);
      }
      if (cancel === 'button') await page.locator('#confirm-reset-cancel').click();
      else await page.keyboard.press('Escape');
      await expect(page.locator('#confirm-reset-dialog')).toBeHidden();
      await expect(page.locator('#sidebar-toggle')).toHaveAttribute('aria-expanded','true');
      await expect(page.locator('#reset-order')).toBeFocused();
      const after = await currentOrder(page);
      expect(after.sessionId).toBe(before.sessionId);
      expect(after.revision).toBe(before.revision);
      expect(after.messages).toEqual(before.messages);
    }
    await returnToChat(page);
    await expect(page.locator('#message-input')).toHaveValue('这段还没有发送');
  });
}

test('confirming a new order on mobile returns to a usable chat and keeps history', async ({page,appURL}) => {
  await page.setViewportSize({width:390,height:844});
  await boot(page,appURL);
  const original = await send(page,card);
  await openSidebar(page);
  await page.locator('#reset-order').click();
  const created = page.waitForResponse(response => new URL(response.url()).pathname === '/api/session' && response.request().method() === 'POST');
  await page.locator('#confirm-reset-ok').click();
  expect((await created).status()).toBe(200);
  await expect(page.locator('#sidebar-toggle')).toHaveAttribute('aria-expanded','false');
  await expect(page.locator('#message-input')).toBeFocused();
  expect((await currentOrder(page)).sessionId).not.toBe(original.sessionId);
  await openSidebar(page);
  await expect(page.locator(`.history-item[data-session-id="${original.sessionId}"]`)).toBeVisible();
  const fresh = await currentOrder(page);
  await page.locator('#reset-order').click();
  await page.keyboard.press('Escape');
  await expect(page.locator('#confirm-reset-dialog')).toBeHidden();
  await expect(page.locator('#reset-order')).toBeFocused();
  expect((await currentOrder(page)).sessionId).toBe(fresh.sessionId);
});

for (const width of [390,1440]) {
  test(`platform is labelled, fits, and changes only after selection at ${width}px`, async ({page,appURL}) => {
    await page.setViewportSize({width,height:844});
    await boot(page,appURL);
    await send(page,card);
    const original = await currentOrder(page);
    const mutations=[];
    page.on('request',request=>{if(new URL(request.url()).pathname==='/api/platform' && request.method()==='POST')mutations.push(request.postDataJSON());});
    await showOrder(page);
    const provider=page.locator('.provider-status');
    const platform=provider.locator('#platform-select');
    await platform.scrollIntoViewIfNeeded();
    await expect(provider.locator('label')).toContainText('目标平台');
    expect(await provider.locator('label').evaluate(node=>parseFloat(getComputedStyle(node).fontSize))).toBeGreaterThanOrEqual(12);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(mutations).toEqual([]);
    const next=await platform.locator('option').evaluateAll((nodes,current)=>nodes.find(node=>node.value!==current && !node.disabled).value,await platform.inputValue());
    const changed=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/platform' && response.request().method()==='POST');
    await platform.selectOption(next);
    expect((await changed).status()).toBe(200);
    expect(mutations).toHaveLength(1);
    const saved=await currentOrder(page);
    expect(saved.sessionId).toBe(original.sessionId);
    expect(saved.order.platform).toBe(next);
    await page.reload();
    await expect(page.locator('#order-number')).not.toContainText('----');
    await expect(page.locator('#platform-select')).toHaveValue(next);
    expect(mutations).toHaveLength(1);
  });
}

for (const width of [320,390]) {
  test(`short portrait keeps the composer clear with examples closed or open at ${width}px`, async ({page,appURL}, info) => {
    await page.setViewportSize({width,height:568});
    await boot(page,appURL);
    const initial=await currentOrder(page);
    const posts=[];
    page.on('request',request=>{if(request.method()==='POST')posts.push(request.url());});
    const clearComposer=async()=>{
      await inViewport(page.locator('.send-button'),568);
      expect(await page.locator('.send-button').evaluate(node=>{
        const box=node.getBoundingClientRect();
        return node.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
      })).toBe(true);
      const composer=await page.locator('#message-form').boundingBox();
      await expect(page.locator('.task-bar')).toBeHidden();
      expect(composer.y+composer.height).toBeLessThanOrEqual(568);
    };
    await clearComposer();
    await page.locator('#starter-panel > summary').click();
    await clearComposer();
    const example=page.locator('[data-example]').first();
    const text=await example.getAttribute('data-example');
    await example.click();
    await expect(page.locator('#message-input')).toHaveValue(text);
    expect(posts).toEqual([]);
    expect((await currentOrder(page)).revision).toBe(initial.revision);
    await page.locator('#starter-panel > summary').click();
    await page.locator('#message-input').fill('数量与工艺还需要确认，'.repeat(30));
    await clearComposer();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await info.attach('short-screen.png',{body:await page.screenshot({fullPage:true}),contentType:'image/png'});
  });
}

test('short landscape allows scrolling to the send action without overlapping the task bar',async({page,appURL})=>{
  await page.setViewportSize({width:844,height:390});
  await boot(page,appURL);
  await page.locator('.send-button').scrollIntoViewIfNeeded();
  await inViewport(page.locator('.send-button'),390);
  expect(await page.locator('.send-button').evaluate(node=>{
    const box=node.getBoundingClientRect();
    return node.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
  })).toBe(true);
});

test('composition Enter does not submit; Shift+Enter adds a line and Enter sends once',async({page,appURL})=>{
  await boot(page,appURL);
  const before=await currentOrder(page);
  const posts=[];
  page.on('request',request=>{if(new URL(request.url()).pathname==='/api/chat' && request.method()==='POST')posts.push(request);});
  const input=page.locator('#message-input');
  await input.fill('500张名片');
  await input.dispatchEvent('keydown',{key:'Enter',code:'Enter',isComposing:true,bubbles:true});
  await input.dispatchEvent('keydown',{key:'Enter',code:'Enter',keyCode:229,bubbles:true});
  await expect(input).toBeEnabled();
  await expect(input).toHaveValue('500张名片');
  expect((await currentOrder(page)).revision).toBe(before.revision);
  expect(posts).toHaveLength(0);
  await input.press('Shift+Enter');
  await expect(input).toHaveValue('500张名片\n');
  const sent=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/chat' && response.request().method()==='POST');
  await input.press('Enter');
  expect((await sent).status()).toBe(200);
  await expect(input).toBeEnabled();
  await expect(input).toHaveValue('');
  expect(posts).toHaveLength(1);
});

test('a local quote can be cancelled after a pending tool response; cancelled stays disabled',async({page,appURL})=>{
  await boot(page,appURL);
  await send(page,card);
  await openCompare(page);
  const chosen=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/choose');
  await page.locator('.choose-option').first().click();
  expect((await chosen).status()).toBe(200);
  await openTools(page);
  const prepared=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/tools/call');
  await page.getByRole('button',{name:'QT 准备询价 生成待人工确认的询价请求'}).click();
  const snapshot=await (await prepared).json();
  expect(snapshot.quoteRequest.status).toBe('awaiting_human_confirmation');
  let release, received;
  const held=new Promise(resolve=>release=resolve);
  const ready=new Promise(resolve=>received=resolve);
  await page.route('**/api/tools/call',async route=>{
    const response=await route.fetch();
    received();
    await held;
    await route.fulfill({response});
  });
  try {
    await page.getByRole('button',{name:'PR 估算费用 生成非正式价格区间'}).click();
    await ready;
    await showOrder(page);
    await page.locator('.order-document-links [data-view="review"]').click();
    await expect(page.locator('#quote-cancel')).toBeDisabled();
    const arrived=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/tools/call');
    release();
    await arrived;
    await expect(page.locator('#quote-cancel')).toBeEnabled();
    await showOrder(page);
    const cancelled=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/quote/cancel');
    await page.locator('#quote-cancel').click();
    const saved=await (await cancelled).json();
    expect(saved.quoteRequest.status).toBe('cancelled');
    await expect(page.locator('#quote-cancel')).toBeDisabled();
  } finally {release();}
});

test('outer page stays fixed while the three work areas own their scroll', async ({page, appURL}) => {
  await boot(page, appURL);
  const layout = await page.evaluate(() => {
    const workspace = document.querySelector('#workspace').getBoundingClientRect();
    return {
      viewportHeight: window.innerHeight,
      documentHeight: document.documentElement.scrollHeight,
      bodyHeight: document.body.scrollHeight,
      bodyOverflow: getComputedStyle(document.body).overflow,
      workspaceBottom: workspace.bottom,
    };
  });
  expect(layout.documentHeight).toBeLessThanOrEqual(layout.viewportHeight);
  expect(layout.bodyHeight).toBeLessThanOrEqual(layout.viewportHeight);
  expect(layout.bodyOverflow).toBe('hidden');
  expect(layout.workspaceBottom).toBeCloseTo(layout.viewportHeight, 0);
});

test('high contrast keeps user message text, links, and code readable',async({page,appURL},info)=>{
  await boot(page,appURL);
  await send(page,'合成显示检查：普通文字 [链接](https://example.invalid) 和 `90×54mm`。\n\n## 显示检查标题\n\n| 项目 | 数值 |\n| --- | --- |\n| 成品尺寸 | 90×54mm |\n\n```text\n500张名片\n```');
  await page.locator('#settings-button').click();
  await page.locator('[data-settings-view="appearance"]').click();
  await page.locator('#theme-toggle').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme','high-contrast');
  const ratios=await page.locator('.message.user .bubble').last().evaluate(bubble=>{
    const luminance=color=>{
      const rgb=color.match(/[\d.]+/g).slice(0,3).map(value=>{const channel=Number(value)/255;return channel<=0.04045?channel/12.92:((channel+0.055)/1.055)**2.4;});
      return rgb[0]*0.2126+rgb[1]*0.7152+rgb[2]*0.0722;
    };
    return [bubble,...bubble.querySelectorAll('a,code,h2,th,td')].map(node=>{
      const style=getComputedStyle(node);
      const background=style.backgroundColor==='rgba(0, 0, 0, 0)'?getComputedStyle(bubble).backgroundColor:style.backgroundColor;
      const a=luminance(style.color), b=luminance(background);
      return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05);
    });
  });
  expect(ratios.length).toBeGreaterThanOrEqual(8);
  for(const ratio of ratios)expect(ratio).toBeGreaterThanOrEqual(7);
  await info.attach('high-contrast.png',{body:await page.screenshot({fullPage:true,animations:'disabled'}),contentType:'image/png'});
});
