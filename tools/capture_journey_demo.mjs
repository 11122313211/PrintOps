/** Development-only synthetic screenshots and print QA. No customer data or external services. */
import {chromium, expect} from '@playwright/test';
import {mkdir, writeFile, copyFile} from 'node:fs/promises';
const base=process.argv[2];
if(!base || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base)) throw new Error('Pass the isolated local browser fixture URL');
const output='reports/ui-redesign/capture';
await mkdir(output,{recursive:true});
const browser=await chromium.launch();
async function openOrder(page) {
 if(await page.locator('#order-toggle').getAttribute('aria-expanded')!=='true') await page.locator('#order-toggle').click();
 const back=page.locator('.order-view:not([hidden]) [data-order-view="order"]');
 if(await back.isVisible()) await back.click();
 await expect(page.locator('#phase-order')).toBeVisible();
}
async function verifyPrint(page,suffix,orderId) {
 await page.emulateMedia({media:'print'});
 await expect(page.locator('#handoff-preview')).toBeVisible();
 await expect(page.locator('#handoff-preview')).toContainText(orderId);
 for(const selector of ['.topbar','.sidebar','.chat-section','.order-pane-footer','#order-pane > .pane-header','#phase-order','#phase-compare','#mobile-back']) await expect(page.locator(selector)).toBeHidden();
 await page.pdf({path:`${output}/handoff-${suffix}.pdf`,format:'A4',printBackground:true,preferCSSPageSize:true});
 await page.emulateMedia({media:'screen'});
 return {sample:suffix,handoffVisible:true,applicationChromeHidden:true};
}
try {
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
 const page=await context.newPage();
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto(base);await expect(page.locator('#message-input')).toBeEnabled();
 await page.evaluate(()=>document.fonts.ready);
 await expect(page.locator('#order-toggle')).toHaveAttribute('aria-expanded','false');
 await expect(page.locator('#quick-replies button')).toHaveCount(0);
 await expect(page.locator('#message-input')).toHaveCount(1);
 await page.screenshot({path:`${output}/first-use.png`});
 const records=[],printChecks=[];
 const seeds=[
  {productType:'名片',quantity:'500张',size:'90×54mm',paper:'300g白卡纸',printing:'双面四色',finishing:'无特殊工艺',binding:'无需装订',deadline:'下周内'},
  {productType:'单页',quantity:'800张',size:'210×297mm',paper:'157g铜版纸',printing:'双面四色',finishing:'无特殊工艺',binding:'无需装订',deadline:'下周内'},
  {productType:'宣传册',quantity:'100份',size:'210×297mm',paper:'157g铜版纸',printing:'双面四色',pages:'16页',finishing:'无特殊工艺',binding:'骑马钉',deadline:'下周内'}
 ];
 for(const [count,suffix] of [[1,'1-items'],[2,'2-items'],[3,'3-items'],[1,'long-fields']]) {
  const result=await page.evaluate(async ({count,seeds,suffix})=>{
   const headers={'Content-Type':'application/json','X-PrintOps-Token':window.__PRINTOPS_TOKEN__};
   let current=await (await fetch('/api/session',{method:'POST',headers,body:'{}'})).json();
   async function post(path,body){const r=await fetch(path,{method:'POST',headers,body:JSON.stringify({sessionId:current.sessionId,expectedRevision:current.revision,operationId:crypto.randomUUID(),...body})});if(!r.ok)throw new Error(`${path}: ${r.status} ${await r.text()}`);current=await r.json();return current;}
   const order=count===1?{...seeds[0]}:{items:seeds.slice(0,count).map((item,index)=>({...item,itemId:`demo-${index+1}`}))};
   if(suffix==='long-fields') order.deadline='下周内；'+ '具体交付地点、包装批次及交付时间需人工与供应商复核；此段为人工合成长字段说明，不代表供应商已确认。'.repeat(8);
   await post('/api/chat',{text:'人工合成的展示订单，仅用于界面与打印验收。',patch:order});
   if(count===1){await post('/api/tools/call',{toolName:'recommend_processes',payload:{}});}
   else for(const item of current.order.items){await post('/api/choose',{itemId:item.itemId,optionId:'economy'});}
   localStorage.setItem('printops_session',current.sessionId);
   return current;
  },{count,seeds,suffix});
  await page.reload();await page.locator('#order-number').filter({hasText:result.sessionId.toUpperCase()}).waitFor();
  await expect(page.locator('#message-input')).toBeEnabled();
  await openOrder(page);
  await page.locator('#order-pane-body').evaluate(n=>n.scrollTop=0);
  if(suffix==='1-items') await page.screenshot({path:`${output}/workbench.png`});
  if(count===1){
   await page.locator('#open-options').click();
   await page.locator('.choose-option').first().waitFor();
   await page.locator('#order-pane-body').evaluate(n=>n.scrollTop=0);
   if(suffix==='1-items') await page.screenshot({path:`${output}/plan-comparison.png`});
   const choice=page.waitForResponse(r=>r.url().endsWith('/api/choose'));
   await page.locator('.choose-option').first().click();
   if((await choice).status()!==200)throw new Error('Option selection failed');
   await openOrder(page);
  }
  await page.locator('#open-handoff').click();
  const generate=page.waitForResponse(r=>r.url().endsWith('/api/generate'));
  await page.locator('#generate-order').click();
  if((await generate).status()!==200)throw new Error('Handoff generation failed');
  const confirm=page.waitForResponse(r=>r.url().endsWith('/api/confirm'));
  await page.locator('#confirm-order').click();const confirmation=await confirm;
  if(confirmation.status()!==200)throw new Error('Handoff confirmation failed');
  const confirmed=await confirmation.json();
  await page.locator('#handoff-preview').getByText('状态：已本地确认',{exact:false}).waitFor();
  if(!confirmed.handoffDocument?.exportable)throw new Error('Expected exportable confirmed document');
  await writeFile(`${output}/handoff-${suffix}.json`,JSON.stringify(confirmed.handoffDocument,null,2));
  await page.locator('#order-pane-body').evaluate(n=>n.scrollTop=0);
  await page.screenshot({path:`${output}/review-${suffix}.png`});
  printChecks.push(await verifyPrint(page,suffix,confirmed.sessionId));
  records.push({items:count,sample:suffix,orderId:confirmed.sessionId,revision:confirmed.revision,confirmedRevision:confirmed.handoffDocument.confirmedRevision,exportable:true});
 }
 await page.evaluate(id=>localStorage.setItem('printops_session',id),records.find(record=>record.items===3).orderId);
 await page.reload();
 await page.locator('#order-number').filter({hasText:records.find(record=>record.items===3).orderId.toUpperCase()}).waitFor();
 await expect(page.locator('#message-input')).toBeEnabled();
 await page.setViewportSize({width:390,height:844});
 await expect(page.locator('#workspace')).toHaveAttribute('data-layout','narrow');
 await expect(page.locator('#workspace')).toHaveAttribute('data-drawer','');
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw new Error('Narrow screenshot has horizontal page overflow');
 await page.screenshot({path:`${output}/mobile-390.png`,fullPage:true});
 if(errors.length)throw new Error(errors.join('\n'));
 await writeFile(`${output}/capture-summary.json`,JSON.stringify({synthetic:true,customerData:false,records,printChecks,browser:`Chromium ${browser.version()}`,viewportChecks:['1440x1000','390x844'],humanUsabilityValidated:false,fpsMeasured:false},null,2));
 for(const file of ['first-use.png','workbench.png','plan-comparison.png','mobile-390.png'])await copyFile(`${output}/${file}`,`docs/screenshots/${file}`);
 console.log(JSON.stringify(records));
 await context.close();
} finally { await browser.close(); }
