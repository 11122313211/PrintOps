/** End-to-end browser tests for #1 Spec Blueprint + Workcard, #2 Native FlateDecode PDF Preflight, and #3 Pricing Simulator. */
import {expect} from '@playwright/test';
import zlib from 'node:zlib';
import {test, boot, send, currentOrder, openOrder, openCompare, openHandoff} from './journey.fixture.mjs';

const fullCard = '500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内';

/**
 * Build a minimal real PDF containing a FlateDecode /Type /ObjStm compressed object stream
 * with configurable page count, TrimBox, BleedBox, and optional low-res image.
 */
function buildCompressedPdf({pages = 2, trimWmm = 210, trimHmm = 297, bleedMm = 3} = {}) {
  const mm2pt = (mm) => (mm * 72 / 25.4).toFixed(2);
  const bPt = Number(mm2pt(bleedMm));
  const wPt = Number(mm2pt(trimWmm));
  const hPt = Number(mm2pt(trimHmm));
  const mediaBox = `[0 0 ${(wPt + bPt * 2).toFixed(2)} ${(hPt + bPt * 2).toFixed(2)}]`;
  const trimBox = `[${bPt.toFixed(2)} ${bPt.toFixed(2)} ${(wPt + bPt).toFixed(2)} ${(hPt + bPt).toFixed(2)}]`;
  const bleedBox = mediaBox;

  const pageIds = [];
  const objBodies = [];
  const firstPageId = 10;
  for (let i = 0; i < pages; i += 1) {
    const id = firstPageId + i;
    pageIds.push(`${id} 0 R`);
    objBodies.push([
      id,
      `<< /Type /Page /Parent 2 0 R /MediaBox ${mediaBox} /TrimBox ${trimBox} /BleedBox ${bleedBox} /Resources << /ColorSpace << /CS1 /DeviceCMYK >> >> >>`,
    ]);
  }
  const pagesObj = [2, `<< /Type /Pages /Count ${pages} /Kids [${pageIds.join(' ')}] >>`];
  const compressedObjects = [pagesObj, ...objBodies];

  let headerIdx = '';
  let bodyConcat = '';
  let offset = 0;
  for (const [objNum, body] of compressedObjects) {
    headerIdx += `${objNum} ${offset} `;
    bodyConcat += `${body}\n`;
    offset = Buffer.byteLength(bodyConcat, 'latin1');
  }
  const firstOffset = Buffer.byteLength(headerIdx, 'latin1');
  const rawStream = Buffer.from(headerIdx + bodyConcat, 'latin1');
  const deflated = zlib.deflateSync(rawStream);

  const head = Buffer.from(
    `%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n3 0 obj\n<< /Type /ObjStm /N ${compressedObjects.length} /First ${firstOffset} /Filter /FlateDecode /Length ${deflated.length} >>\nstream\n`,
    'latin1',
  );
  const tail = Buffer.from('\nendstream\nendobj\n%%EOF\n', 'latin1');
  return Buffer.concat([head, deflated, tail]);
}

test('blueprint renders flat/booklet-spine/box-3D diagrams, toggles layers view-only, and integrates with workcard', async ({page, appURL}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await boot(page, appURL);

  // 1. Flat card blueprint in spec draft & review + workcard canvas
  await send(page, fullCard);
  await openOrder(page);
  const specHost = page.locator('#spec-blueprint-host');
  await expect(specHost).toBeVisible();
  await expect(specHost.locator('svg.blueprint-svg')).toBeVisible();
  await expect(specHost).toContainText('90');
  await expect(specHost).toContainText('54');

  // Layer toggles are pure client-side (zero POST requests)
  const posts = [];
  page.on('request', (req) => { if (req.method() === 'POST') posts.push(req.url()); });
  const bleedToggle = specHost.locator('button.blueprint-layer-btn[data-layer-toggle="bleed"]');
  const wasPressed = await bleedToggle.getAttribute('aria-pressed');
  await bleedToggle.click();
  await expect(bleedToggle).toHaveAttribute('aria-pressed', wasPressed === 'true' ? 'false' : 'true');
  expect(posts).toEqual([]);

  // Select option, generate draft, verify review blueprint + workcard preview canvas
  await openCompare(page);
  await page.locator('.choose-option').first().click();
  await openHandoff(page);
  const reviewHost = page.locator('#review-blueprint-host');
  await expect(reviewHost).toBeVisible();
  await expect(reviewHost.locator('svg.blueprint-svg')).toBeVisible();

  await page.locator('#generate-order').click();
  await expect(page.locator('#order-export')).toBeVisible();
  await page.locator('.export-option[data-format="card"]').click();
  await expect(page.locator('#workcard-dialog')).toBeVisible();
  const canvasBox = await page.locator('#workcard-preview canvas').boundingBox();
  expect(canvasBox?.width).toBeGreaterThan(200);
  expect(canvasBox?.height).toBeGreaterThan(300);
  await page.locator('#workcard-close').click();

  // 2. Booklet with spine calculation & thick saddle-stitch warning
  await page.locator('#reset-order').click();
  await page.locator('#confirm-reset-ok').click();
  await send(page, '200本宣传册，成品210×297mm，64页，157g铜版纸，双面四色，骑马钉，下周内');
  await openHandoff(page);
  await expect(reviewHost).toContainText('书脊');
  await expect(reviewHost).toContainText('mm');
  await expect(reviewHost).toContainText('骑马钉');

  // 3. Packaging box 3D isometric wireframe & 390px mobile zero overflow
  await page.locator('#reset-order').click();
  await page.locator('#confirm-reset-ok').click();
  await send(page, '500个包装盒，天地盖，外尺寸120×80×60mm，350g白卡纸，双面四色，下周内');
  await openHandoff(page);
  await expect(reviewHost).toContainText('120');
  await expect(reviewHost).toContainText('80');
  await expect(reviewHost).toContainText('60');

  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('browser decompresses FlateDecode ObjStm PDF, catches page & bleed mismatch, overlays blueprint, and isolates multi-item previews', async ({page, appURL}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await boot(page, appURL);
  await send(page, '200本宣传册，成品210×297mm，16页，157g铜版纸，双面四色，骑马钉，下周内');

  const pdfBuffer = buildCompressedPdf({pages: 14, trimWmm: 210, trimHmm: 297, bleedMm: 1});
  const preflightResp = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/preflight' && r.request().method() === 'POST');
  await page.locator('#file-input').setInputFiles({
    name: 'compressed-brochure.pdf',
    mimeType: 'application/pdf',
    buffer: pdfBuffer,
  });
  const preflightData = await (await preflightResp).json();

  expect(preflightData.toolResult.ok).toBe(true);
  expect(preflightData.toolResult.pageCount).toBe(14);
  expect(preflightData.toolResult.expectedPagesMatch).toBe(false);
  expect(preflightData.toolResult.measuredBleedMm).toBeCloseTo(1.0, 1);

  const fileState = page.locator('#file-state');
  await expect(fileState).toBeVisible();
  await expect(fileState).toContainText('PDF 对象流');
  await expect(fileState).toContainText('实测单边出血');
  await expect(fileState).toContainText('订单页数一致性');

  // Blueprint automatically shows PDF overlay comparison
  await openHandoff(page);
  const reviewHost = page.locator('#review-blueprint-host');
  await expect(reviewHost.locator('button.blueprint-layer-btn[data-layer-toggle="pdf"]')).toBeVisible();
  await expect(reviewHost.locator('.blueprint-pdf-bar')).toBeVisible();

  // Multi-product local preview isolation across item switches
  await page.locator('#reset-order').click();
  await page.locator('#confirm-reset-ok').click();
  await send(page, '我要印500张名片和200张单页，下周内');

  // Focus item 1 and upload card.pdf
  await openOrder(page);
  const itemFocusBtns = page.locator('#multi-product-list .multi-product-item .multi-product-focus');
  await expect(itemFocusBtns).toHaveCount(2);

  const focus1 = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/focus');
  await itemFocusBtns.nth(0).click();
  await focus1;
  const up1 = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/preflight');
  await page.locator('#file-input').setInputFiles({
    name: 'item1-card.pdf',
    mimeType: 'application/pdf',
    buffer: buildCompressedPdf({pages: 2, trimWmm: 90, trimHmm: 54, bleedMm: 3}),
  });
  await up1;
  await expect(fileState).toContainText('item1-card.pdf');

  // Focus item 2 and upload flyer.pdf
  await openOrder(page);
  const focus2 = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/focus');
  await itemFocusBtns.nth(1).click();
  await focus2;
  const up2 = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/preflight');
  await page.locator('#file-input').setInputFiles({
    name: 'item2-flyer.pdf',
    mimeType: 'application/pdf',
    buffer: buildCompressedPdf({pages: 2, trimWmm: 210, trimHmm: 297, bleedMm: 3}),
  });
  await up2;
  await expect(fileState).toContainText('item2-flyer.pdf');

  // Switch back to item 1: its local PDF preview & iframe survive intact
  await openOrder(page);
  const focusBack = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/focus');
  await itemFocusBtns.nth(0).click();
  await focusBack;
  await expect(fileState).toContainText('item1-card.pdf');
  await expect(fileState.locator('iframe.pdf-viewer')).toBeVisible();
});

test('pricing simulator loads via GET-only on compare view, runs read-only What-If, and writes back on explicit apply', async ({page, appURL}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await boot(page, appURL);
  const initial = await send(page, fullCard);

  const postPaths = [];
  page.on('request', (req) => {
    if (req.method() === 'POST') postPaths.push(new URL(req.url()).pathname);
  });

  // Switching to compare loads simulator strictly via GET (zero POST!)
  const getSim = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/pricing/simulate' && r.request().method() === 'GET');
  await openCompare(page);
  expect((await getSim).status()).toBe(200);
  expect(postPaths).toEqual([]);

  const simHost = page.locator('#pricing-simulator-host');
  await expect(simHost.locator('.pricing-sim-tier-card').first()).toBeVisible();
  await expect(simHost).toContainText('性价比拐点');
  await expect(simHost).toContainText('不构成供应商正式报价');

  // Click a higher tier (1000 张) & toggle 烫金 -> read-only POST, revision unchanged
  const simPost1 = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/pricing/simulate' && r.request().method() === 'POST');
  await simHost.locator('.pricing-sim-tier-card', {hasText: '1,000'}).click();
  expect((await simPost1).status()).toBe(200);

  const simPost2 = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/pricing/simulate' && r.request().method() === 'POST');
  await simHost.locator('.pricing-sim-pill', {hasText: '烫金'}).click();
  expect((await simPost2).status()).toBe(200);

  // Order must still be untouched before explicit Apply
  const midOrder = await currentOrder(page);
  expect(midOrder.revision).toBe(initial.revision);
  expect(midOrder.order.quantityValue).toBe(500);

  // Explicit Apply writes to /api/fields and persists
  const applyBtn = simHost.locator('.pricing-sim-apply-btn');
  await expect(applyBtn).toBeEnabled();
  const fieldsResp = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/fields' && r.request().method() === 'POST');
  await applyBtn.click();
  const applied = await (await fieldsResp).json();
  expect(applied.revision).toBeGreaterThan(initial.revision);
  expect(applied.order.quantityValue).toBe(1000);
  expect(applied.order.finishing).toContain('烫金');

  await page.reload();
  const persisted = await currentOrder(page);
  expect(persisted.order.quantityValue).toBe(1000);
  expect(persisted.order.finishing).toContain('烫金');

  // 390px mobile zero overflow on compare view with simulator expanded
  await page.setViewportSize({width: 390, height: 844});
  await openCompare(page);
  await expect(simHost.locator('.pricing-sim-tier-card').first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
