/** PF real-browser acceptance. No fetch mocks, response interception, public fault/reset control or certificate bypass.
 * Authentication capture stays off. All durable attachments are explicit safe fields; raw credentials remain private fixture input.
 */
import { Page, TestInfo, Locator } from '@playwright/test';
import {test,expect} from './portfolio_fixture';
import AxeBuilder from '@axe-core/playwright';
import { createHmac, randomUUID } from 'node:crypto';
import {dirname} from 'node:path';
import { lstatSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';

type Account = { username: string; password: string; totp_secret?: string };
type Fixture = { profile: string; run_id: string; customerA: Account; customerB: Account; merchant: Account; mfaReviewer: Account };
const baseURL = process.env.SME_TEST_BASE_URL || '';
const runId = process.env.SME_TEST_RUN_ID || '';
let accounts: Fixture;
let purchasedOrder='';
let registeredCustomer:{email:string;password:string;full_name:string}|null=null;

test.describe.configure({ mode: 'default' });
test.use({ trace: 'off', screenshot: 'off', video: 'off' });
test.setTimeout(90000);

test.beforeAll(async ({ browser }) => {
  if (!/^https:\/\/(?:store\.test(?::8443)?|localhost:(?:8443|9443))\/?$/.test(baseURL) || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(runId)) throw new Error('BLOCKED: real browser gates require the validated HTTPS local demo-test fixture and UUID run ID.');
  const path=process.env.SME_TEST_ACCOUNTS_FILE;if(!path)throw new Error('BLOCKED: private operator-provisioned test accounts are required.');
  const info=lstatSync(path);if(!info.isFile()||info.isSymbolicLink()||(info.mode&0o077)!==0)throw new Error('BLOCKED: account fixture must be a private0600 regular nonsymlink file.');
  accounts=JSON.parse(readFileSync(path,'utf8'));
  if(accounts.profile!=='demo-test'||accounts.run_id!==runId)throw new Error('BLOCKED: account fixture environment/run identity mismatch.');
  const context=await browser.newContext();try{const page=await context.newPage();await page.goto(`${baseURL}/account`);const body=await page.evaluate(async()=>{const response=await fetch('/api/v1/session');return {status:response.status,body:await response.json()};});expect(body.status).toBe(200);expect(body.body.data.profile).toBe('demo-test');expect(body.body.data.actor).toBeNull();}finally{await context.close();}
});

test.afterEach(async({page},info)=>{
  if(info.status!==info.expectedStatus){const alerts=await page.getByRole('alert').allTextContents();await info.attach('safe-failure-alerts',{body:Buffer.from(JSON.stringify({alerts:alerts.map(text=>text.slice(0,400))})),contentType:'application/json'});}
});

async function visible(locator:Locator) { await expect.poll(()=>locator.isVisible()).toBe(true); }

async function signIn(page:Page,account:Account) {
  await page.goto(`${baseURL}/account`);
  const signOut=page.getByRole('button',{name:'Sign out',exact:true});if(await signOut.isVisible())await signOut.click();
  await page.getByLabel('Email or reviewer username',{exact:true}).fill(account.username);
  await page.getByLabel('Password',{exact:true}).fill(account.password);
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  if(account.totp_secret){await visible(page.getByLabel('Authenticator code',{exact:true}));await page.getByLabel('Authenticator code',{exact:true}).fill(totp(account.totp_secret));await page.getByRole('button',{name:'Verify & sign in',exact:true}).click();}
  await visible(page.getByRole('heading',{name:account.totp_secret?'Overview':'Your current identity',exact:true}));
}

function totp(secret:string,stepOffset=0):string {
  const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';let bits='';for(const char of secret.toUpperCase().replace(/=+$/,'')){const value=alphabet.indexOf(char);if(value<0)throw new Error('Invalid private TOTP fixture encoding.');bits+=value.toString(2).padStart(5,'0');}
  const key=Buffer.from((bits.match(/.{8}/g)||[]).map(byte=>parseInt(byte,2))),counter=Buffer.alloc(8);counter.writeBigUInt64BE(BigInt(Math.floor(Date.now()/30000)+stepOffset));
  const digest=createHmac('sha1',key).update(counter).digest(),offset=digest[digest.length-1]&15;
  return String((digest.readUInt32BE(offset)&0x7fffffff)%1000000).padStart(6,'0');
}

async function purchase(page:Page,instrument='Simulated success'):Promise<string> {
  await page.goto(`${baseURL}/search`);await expect(page.locator('.product-card').first()).toBeVisible();
  const count=Math.min(await page.locator('.product-card').count(),20);let selected=false;
  for(let index=0;index<count;index++){
    await page.locator('.product-card').nth(index).locator('a').first().click();
    await expect(page.getByRole('button',{name:'Increase quantity',exact:true})).toBeVisible();
    await page.getByRole('button',{name:'Increase quantity',exact:true}).click();
    const add=page.getByRole('button',{name:'Add to your bag',exact:true});
    if(await add.isVisible()&&await add.isEnabled()){await add.click();selected=true;break;}
    await page.goto(`${baseURL}/search`);await expect(page.locator('.product-card').first()).toBeVisible();
  }
  if(!selected)throw new Error('BLOCKED: no currently available two-unit product fixture.');
  await page.getByRole('link',{name:/Your bag, /}).click();
  await page.getByRole('button',{name:'Continue to checkout',exact:true}).click();await expect(page.getByRole('heading',{name:'A considered demo purchase.'})).toBeVisible();
  await page.getByRole('button',{name:'Use demo address · MI',exact:true}).click();await page.getByRole('button',{name:'Save destination & get delivery offers',exact:true}).click();
  const delivery=page.getByRole('radio').first();await expect(delivery).toBeVisible();await delivery.check();await page.getByRole('button',{name:'Save delivery & calculate exact total',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Review the saved quote',exact:true})).toBeVisible();await page.getByLabel('Safe demo payment scenario',{exact:true}).selectOption({label:instrument});
  await page.getByRole('checkbox',{name:/I accept this exact saved total/}).check();await page.getByRole('button',{name:'Submit simulated purchase',exact:true}).click();
  await expect(page).toHaveURL(/\/purchases\//);const attempt=page.url();await expect(page.getByRole('heading',{name:'Good things are on their way.'})).toBeVisible({timeout:30000});
  await page.reload();await expect(page.getByRole('heading',{name:'Good things are on their way.'})).toBeVisible();expect(page.url()).toBe(attempt);
  await page.getByRole('link',{name:'View your order',exact:true}).click();await expect(page).toHaveURL(/\/orders\//);return page.url();
}

async function safeEvidence(info:TestInfo,caseId:string,observations:Record<string,unknown>) {
  await info.attach('safe-case-observations',{body:Buffer.from(JSON.stringify({case:caseId,run_id:runId,profile:'demo-test',release_id:process.env.SME_RELEASE_ID||'UNRESOLVED',boundary:'actual trusted-HTTPS browser/application/PostgreSQL and deterministic providers',observations,limitation:'No vendor API or hosted-production qualification; exact backend durability receipts are separate owner evidence.'})),contentType:'application/json'});
}

test('customer · PF-UI01 paid purchase and durable order refresh',async({page},info)=>{
  await signIn(page,accounts.customerA);const order=await purchase(page);purchasedOrder=order;await page.reload();await expect(page.getByRole('heading',{name:/^Order /})).toBeVisible();
  await safeEvidence(info,'PF-UI01',{order_path:new URL(order).pathname,refresh:'current authorized order visible'});
});

test('merchant · PF-UI02 draft variant price asset publish and stock history',async({page},info)=>{
  const signals:{catalog:any[];touch:any[];scope_changes:number;dialog:any[]}={catalog:[],touch:[],scope_changes:0,dialog:[]};let lastScope:string|null=null;
  const observe=async(response:any)=>{
    try{const path=new URL(response.url()).pathname;if(!['/api/v1/session','/api/v1/merchant/catalog'].includes(path)&&!path.startsWith('/api/v1/auth/'))return;const envelope=await response.json();
      const code=typeof envelope.error?.code==='string'&&/^[a-z0-9_]{1,80}$/.test(envelope.error.code)?envelope.error.code:null;
      if(path==='/api/v1/merchant/catalog')signals.catalog.push({status:response.status(),error_code:code});
      if(path==='/api/v1/session'||path.startsWith('/api/v1/auth/')){const dto=envelope.data;const scope=dto&&Object.hasOwn(dto,'actor')?JSON.stringify([dto.actor?.id||null,dto.epoch,dto.session_generation]):null;const changed=Boolean(lastScope&&scope&&lastScope!==scope);if(changed)signals.scope_changes+=1;if(scope)lastScope=scope;if(path==='/api/v1/auth/touch')signals.touch.push({status:response.status(),error_code:code,scope_changed:changed});}
    }catch{}
  };
  page.on('response',observe);
  const dialogSnapshot=async()=>{try{signals.dialog.push({path:new URL(page.url()).pathname,count:await page.getByRole('dialog').count(),open:await page.locator('dialog[open]').count(),product_name_fields:await page.getByLabel('Product name',{exact:true}).count()});}catch{}};
  try{
  await signIn(page,accounts.merchant);signals.scope_changes=0;signals.touch=[];await page.getByRole('link',{name:'Catalog',exact:true}).click();const suffix=runId.slice(0,8)+'-'+randomUUID().slice(0,8),name=`Portfolio object ${suffix}`,slug=`portfolio-${suffix}`,sku=`PF-${suffix}`;
  await page.getByRole('button',{name:'Create draft product',exact:true}).click();await dialogSnapshot();let dialog=page.getByRole('dialog');await expect.poll(()=>dialog.isVisible(),{timeout:10000}).toBe(true);await dialog.getByLabel('Product name',{exact:true}).fill(name);await dialog.getByLabel('Plain-text description',{exact:true}).fill('Synthetic reviewer-created object.');await dialog.getByLabel('Unique lowercase URL slug',{exact:true}).fill(slug);await dialog.getByLabel('Category',{exact:true}).fill('home');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  let product=page.locator('article').filter({has:page.getByRole('heading',{name,exact:true})});await expect(product).toBeVisible();await product.getByRole('button',{name:'Add variant',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('Unique SKU',{exact:true}).fill(sku);await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  product=page.locator('article').filter({has:page.getByRole('heading',{name,exact:true})});await product.getByRole('button',{name:'Set price',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('USD cents',{exact:true}).fill('1200');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  await product.getByRole('button',{name:'Choose image',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('Curated immutable asset',{exact:true}).selectOption({index:1});await dialog.getByLabel('Descriptive alternative text',{exact:true}).fill('Synthetic natural ceramic object');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  await product.getByRole('button',{name:'Choose image',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('Curated immutable asset',{exact:true}).selectOption({index:2});await dialog.getByLabel('Descriptive alternative text',{exact:true}).fill('Synthetic second gallery view');await dialog.getByLabel('Gallery position (0–9)',{exact:true}).fill('1');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  await product.getByRole('button',{name:'Publish draft',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'Confirm & save',exact:true}).click();await expect(product.getByText('Published',{exact:true})).toBeVisible();
  await page.getByRole('link',{name:'Inventory',exact:true}).click();const row=page.getByRole('row').filter({hasText:name});await expect(row).toBeVisible();await row.getByRole('button',{name:'Adjust',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('Signed nonzero quantity adjustment',{exact:true}).fill('3');await dialog.getByLabel('Adjustment reason',{exact:true}).fill('Synthetic portfolio fixture receipt');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();await row.getByRole('button',{name:'History',exact:true}).click();await expect(page.getByRole('heading',{name:'Inventory movement history',exact:true})).toBeVisible();
  await page.getByRole('link',{name:'Promotions',exact:true}).click();await page.getByRole('button',{name:'Create promotion',exact:true}).click();dialog=page.getByRole('dialog');
  await dialog.getByLabel('Discount rule',{exact:true}).selectOption('fixed');await dialog.getByLabel('Value: cents or integer basis points',{exact:true}).fill('100');await dialog.getByLabel('Coupon code (optional)',{exact:true}).fill('PF-'+suffix);await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  const promotion=page.getByRole('row').filter({hasText:'PF-'+suffix});await expect(promotion).toBeVisible();await promotion.getByRole('button',{name:'Activate rule',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'Confirm & save',exact:true}).click();await expect(promotion.getByText('Active',{exact:true})).toBeVisible();await promotion.getByRole('button',{name:'End rule',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'Confirm & save',exact:true}).click();await expect(promotion.getByText('Ended',{exact:true})).toBeVisible();
  if(!purchasedOrder)throw new Error('BLOCKED: durable customer order fixture was not produced by the preceding real browser case.');
  await page.goto(purchasedOrder);await page.locator('.unit-selector').getByRole('checkbox').first().check();const quoteReply=page.waitForResponse(value=>value.request().method()==='POST'&&new URL(value.url()).pathname==='/api/v1/merchant/fulfillment/commands'&&value.request().postDataJSON()?.operation==='quote_parcel');await page.getByRole('button',{name:'Get parcel offers',exact:true}).click();const quoteHttp=await quoteReply;const quoteEnvelope=await quoteHttp.json();const quoteInput=quoteHttp.request().postDataJSON();const safeQuote={http_status:quoteHttp.status(),operation:'quote_parcel',command_id:quoteInput.command_id,order_id:quoteInput.input?.order_id,unit_ids:quoteInput.input?.unit_ids,expected_version:quoteInput.expected_version,error_code:typeof quoteEnvelope.error?.code==='string'&&/^[a-z0-9_]{1,80}$/.test(quoteEnvelope.error.code)?quoteEnvelope.error.code:null,effect_certainty:['none','unknown'].includes(quoteEnvelope.meta?.effect_certainty)?quoteEnvelope.meta.effect_certainty:null,request_id:typeof quoteEnvelope.meta?.request_id==='string'&&/^[a-z0-9-]{1,80}$/.test(quoteEnvelope.meta.request_id)?quoteEnvelope.meta.request_id:null,offers_count:(quoteEnvelope.data?.offers||quoteEnvelope.data?.delivery_offers||[]).length};await info.attach('safe-parcel-quote-http',{body:Buffer.from(JSON.stringify(safeQuote)),contentType:'application/json'});expect(quoteHttp.status()).toBe(200);await page.getByLabel('Explicit simulated parcel service',{exact:true}).selectOption({index:1});await page.getByRole('button',{name:'Create parcel for selected units',exact:true}).click();
  await page.getByRole('button',{name:'Get simulated label',exact:true}).first().click();const downloadEvent=page.waitForEvent('download');await page.getByRole('button',{name:'Download simulated label',exact:true}).first().click();const download=await downloadEvent;const stream=await download.createReadStream();if(!stream)throw new Error('Protected label download unavailable');let text='';for await(const chunk of stream)text+=chunk.toString();expect(text.startsWith('SIMULATED LABEL')).toBe(true);await page.getByRole('button',{name:'Reserve handover',exact:true}).first().click();await page.getByRole('dialog').getByRole('button',{name:'Confirm & save',exact:true}).click();await page.getByRole('button',{name:'Simulate handover',exact:true}).first().click();await page.getByRole('dialog').getByRole('button',{name:'Confirm & save',exact:true}).click();await page.getByRole('button',{name:'Simulate tracking / delivery',exact:true}).first().click();dialog=page.getByRole('dialog');await dialog.getByLabel('Tracking milestone',{exact:true}).selectOption('DELIVERED');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  await page.locator('.unit-selector').getByRole('checkbox').nth(1).check();await page.getByRole('button',{name:'Request cancellation',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('Cancellation reason',{exact:true}).fill('Synthetic unshipped unit cancellation');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  await page.locator('.unit-selector').getByRole('checkbox').first().check();await page.getByRole('button',{name:'Request return / exchange',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('Return reason',{exact:true}).fill('Synthetic reviewer exchange');await dialog.getByLabel('Preferred resolution',{exact:true}).selectOption('exchange');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();await expect(page).toHaveURL(/\/returns\//);
  await page.getByRole('button',{name:'Review approval',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByLabel('Return decision',{exact:true}).selectOption('approved');await dialog.getByLabel('Decision reason',{exact:true}).fill('Synthetic inspected return policy');await dialog.getByRole('button',{name:'Confirm & save',exact:true}).click();
  await page.locator('.unit-selector').getByRole('checkbox').first().check();await page.getByRole('button',{name:'Record selected units received',exact:true}).click();await page.getByRole('combobox',{name:/Unit /}).first().selectOption('sellable');await page.getByRole('button',{name:'Record explicit inspection results',exact:true}).click();
  await page.getByRole('button',{name:'Request approved simulated refund',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'Confirm & save',exact:true}).click();await page.getByLabel('Published replacement variant',{exact:true}).selectOption({index:1});await page.getByRole('button',{name:'Create linked replacement checkout',exact:true}).click();await expect(page.getByRole('status').filter({hasText:'Replacement checkout linked'})).toBeVisible({timeout:30000});await page.reload();await expect(page.getByRole('status').filter({hasText:'Replacement checkout linked'})).toBeVisible();
  await page.goto(`${baseURL}/products/${slug}`);const secondImage=page.getByRole('button',{name:'Show product image 2',exact:true});await expect(secondImage).toBeEnabled();await secondImage.focus();await page.keyboard.press('Enter');await expect(secondImage).toHaveAttribute('aria-pressed','true');await expect(page.getByRole('img',{name:'Synthetic second gallery view',exact:true})).toBeVisible();
  await safeEvidence(info,'PF-UI02',{gallery:'two published curated assets; keyboard activated second image',draft_variant_price_media_publication:'actual UI commands completed',stock:'explicit adjustment/history',partial_fulfillment:'one of two selected original units labeled/handed over/delivered',cancellation:'other unshipped unit requested',return:'approved/received/inspected sellable',refund:'original-unit request only; settlement validated by owner evidence',exchange:'independent replacement linked',synthetic_sku:sku});
  }finally{await dialogSnapshot();page.off('response',observe);await info.attach('safe-merchant-dialog-signals',{body:Buffer.from(JSON.stringify(signals)),contentType:'application/json'});}
});

test('auth · PF-AUTH01 staff MFA rejects invalid and replayed code; shortcuts404',async({page},info)=>{
 const account=accounts.mfaReviewer;if(!account.totp_secret)throw new Error('BLOCKED: separately provisioned MFA reviewer fixture is required.');
 await page.goto(`${baseURL}/account`);await page.getByLabel('Email or reviewer username',{exact:true}).fill(account.username);await page.getByLabel('Password',{exact:true}).fill(account.password);await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await visible(page.getByLabel('Authenticator code',{exact:true}));const allowed=new Set([-1,0,1].map(offset=>totp(account.totp_secret!,offset)));let invalid='000000';while(allowed.has(invalid))invalid=String(Number(invalid)+1).padStart(6,'0');
 await page.getByLabel('Authenticator code',{exact:true}).fill(invalid);await page.getByRole('button',{name:'Verify & sign in',exact:true}).click();await visible(page.getByRole('alert'));
 const acceptedCode=totp(account.totp_secret);await page.getByLabel('Authenticator code',{exact:true}).fill(acceptedCode);await page.getByRole('button',{name:'Verify & sign in',exact:true}).click();await visible(page.getByRole('heading',{name:'Overview',exact:true}));
 const removed=await page.evaluate(async()=>{const response=await fetch('/api/v1/sandbox/staff',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});return response.status;});expect(removed).toBe(404);
 await page.goto(`${baseURL}/account`);await page.getByRole('button',{name:'Sign out',exact:true}).click();await page.getByLabel('Email or reviewer username',{exact:true}).fill(account.username);await page.getByLabel('Password',{exact:true}).fill(account.password);await page.getByRole('button',{name:'Sign in',exact:true}).click();await page.getByLabel('Authenticator code',{exact:true}).fill(acceptedCode);await page.getByRole('button',{name:'Verify & sign in',exact:true}).click();await visible(page.getByRole('alert'));await expect.poll(()=>page.getByRole('heading',{name:'Overview',exact:true}).isVisible()).toBe(false);
 await safeEvidence(info,'PF-AUTH01',{invalid_code:'denied',successful_mfa:'published merchant',replayed_counter:'denied',removed_staff_elevation:404});
});

test('auth · PF-UI05 account switch clears private DOM across tabs',async({page,context},info)=>{
  await signIn(page,accounts.customerA);const order=await purchase(page);const second=await context.newPage();await second.goto(order);await expect(second.getByRole('heading',{name:/^Order /})).toBeVisible();
  await page.goto(`${baseURL}/account`);await page.getByRole('button',{name:'Sign out',exact:true}).click();await expect(second.getByRole('heading',{name:/^Order /})).toBeHidden();
  await signIn(page,accounts.customerB);await second.goto(order);await expect(second.getByRole('alert')).toBeVisible();await expect(second.getByText('Original purchase',{exact:true})).toBeHidden();
  await safeEvidence(info,'PF-UI05',{cross_tab_logout:'old private heading removed',foreign_order:'denied after account switch'});
});

test('recovery · PF-UI04 processing instrument resolves without a public completion hook',async({page},info)=>{
  await signIn(page,accounts.customerA);const order=await purchase(page,'Simulated processing · Five-second phases');await expect(page.getByRole('heading',{name:/^Order /})).toBeVisible();
  await safeEvidence(info,'PF-UI04',{order_path:new URL(order).pathname,instrument:'demo_pending',public_fault_or_complete_action:'none invoked'});
});

test('accessibility · PF-UI06 responsive public navigation and automated serious findings',async({page},info)=>{
  const results=[];for(const width of[320,768,1440]){await page.setViewportSize({width,height:900});await page.goto(`${baseURL}/search`);await expect(page.getByRole('heading',{name:'Good things, for every day.'})).toBeVisible();const dimensions=await page.evaluate(()=>({viewport:window.innerWidth,scroll:document.documentElement.scrollWidth}));expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.viewport);const scan=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa','wcag22aa']).analyze();expect(scan.violations.filter(item=>['serious','critical'].includes(item.impact||''))).toEqual([]);results.push({width,serious_or_critical:0});}
  await page.keyboard.press('Tab');await expect(page.getByRole('link',{name:'Skip to content',exact:true})).toBeFocused();await page.keyboard.press('Enter');
  await safeEvidence(info,'PF-UI06',{viewports:results,keyboard:'skip link activated',limitation:'Full customer/merchant keyboard and screen-reader tasks require separate manual evidence; this case does not close them.'});
});


async function fillPrivate(locator:Locator,value:string) {
  try { await locator.fill(value,{timeout:10000}); } catch { throw new Error('The private account input is unavailable.'); }
}

function capturedAccountMail(recipient:string,route:'/verify-email'|'/reset-password'):{link:string;receivedAt:number}|null {
  const directory=process.env.SME_TEST_ACCOUNT_MAIL_CAPTURE_DIR;
  if(!directory)throw new Error('BLOCKED: a private local account-mail capture directory is required.');
  const info=lstatSync(directory);
  if(!info.isDirectory()||info.isSymbolicLink()||(info.mode&0o077)!==0||info.uid!==process.getuid?.())throw new Error('BLOCKED: private current-user account-mail capture required.');
  const folder=directory+'/messages';let names:string[];
  try{names=readdirSync(folder);}catch{return null;}
  const records=[];
  for(const name of names.filter(item=>/^[0-9a-f]{64}\.json$/.test(item))){
    const path=folder+'/'+name,metadata=lstatSync(path);
    if(!metadata.isFile()||metadata.isSymbolicLink()||(metadata.mode&0o077)!==0)throw new Error('Private account-mail record required.');
    let record;try{record=JSON.parse(readFileSync(path,'utf8'));}catch{throw new Error('Invalid private account-mail record.');}
    if(Array.isArray(record.envelope_to)&&record.envelope_to.includes(recipient)&&typeof record.text==='string'&&record.text.includes(route+'#token='))records.push(record);
  }
  records.sort((a,b)=>String(b.received_at).localeCompare(String(a.received_at)));
  const link=records[0]?.text.match(/https:\/\/[^\s<>]+\/(?:verify-email|reset-password)#token=[A-Za-z0-9_-]{43}/)?.[0];
  if(!link)return null;
  const url=new URL(link);
  if(url.origin!==new URL(baseURL).origin||url.pathname!==route||url.search||!/^#token=[A-Za-z0-9_-]{43}$/.test(url.hash))throw new Error('Private account-mail route binding mismatch.');
  const receivedAt=Date.parse(records[0].received_at);if(!Number.isFinite(receivedAt))throw new Error('Invalid private account-mail timestamp.');
  return {link,receivedAt};
}
function capturedAccountLink(recipient:string,route:'/verify-email'|'/reset-password'):string|null {return capturedAccountMail(recipient,route)?.link || null;}

async function waitForAccountLink(recipient:string,route:'/verify-email'|'/reset-password') {
  await expect.poll(()=>Boolean(capturedAccountLink(recipient,route)),{timeout:30000}).toBe(true);
  return capturedAccountLink(recipient,route)!;
}

async function openAccountLink(page:Page,link:string) {
  try{await page.goto(link,{timeout:10000});}catch{throw new Error('The private account link could not be opened.');}
  await expect.poll(()=>!new URL(page.url()).hash).toBe(true);
}

function savePrivateCustomer(value:{email:string;password:string;full_name:string},candidate=false) {
  const output=process.env.SME_TEST_REGISTERED_CUSTOMER_FILE;if(!output)return;
  const file=candidate ? output+'.candidate' : output,parent=lstatSync(dirname(file));
  if(!parent.isDirectory()||parent.isSymbolicLink()||(parent.mode&0o077)!==0||parent.uid!==process.getuid?.())throw new Error('Private credential oracle directory required.');
  try{const info=lstatSync(file);if(!info.isFile()||info.isSymbolicLink()||(info.mode&0o077)!==0||info.uid!==process.getuid?.())throw new Error('Private credential oracle file required.');}catch(error:any){if(error.code!=='ENOENT')throw error;}
  writeFileSync(file,JSON.stringify({profile:'demo-test',run_id:runId,...value,username:value.email,observation:candidate?'candidate_before_request':'backend_completion_observed'}),{mode:0o600});
}
async function freshResetLink(page:Page,email:string) {
  const before=capturedAccountMail(email,'/reset-password');
  if(before){const notBefore=before.receivedAt+61000;if(notBefore-Date.now()>65000)throw new Error('Private capture clock mismatch.');await expect.poll(()=>Date.now()>=notBefore,{timeout:65000,intervals:[1000]}).toBe(true);}
  await page.goto(baseURL+'/forgot-password');await fillPrivate(page.getByLabel('Email address',{exact:true}),email);await page.getByRole('button',{name:'Send reset instructions',exact:true}).click();
  await expect.poll(()=>{const value=capturedAccountLink(email,'/reset-password');return Boolean(value&&value!==before?.link);},{timeout:30000}).toBe(true);
  return capturedAccountLink(email,'/reset-password')!;
}

async function customerSignIn(page:Page,email:string,password:string) {
  await page.goto(`${baseURL}/account`);
  await fillPrivate(page.getByLabel('Email or reviewer username',{exact:true}),email);
  await fillPrivate(page.getByLabel('Password',{exact:true}),password);
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await visible(page.getByRole('heading',{name:'Your current identity',exact:true}));
}

test('account access · registration verification guest handoff and password recovery',async({page,browser},info)=>{
  test.setTimeout(180000);page.setDefaultTimeout(15000);
  let checkpoint='navigation';
  try{
  const email='customer-'+randomUUID()+'@example.invalid',fullName='Sample Customer';
  const password='A considered password '+randomUUID(),newPassword='A different password '+randomUUID();
  await page.setViewportSize({width:320,height:900});await page.goto(baseURL+'/');
  const mobileAccess=page.getByRole('navigation',{name:'Mobile account access',exact:true});
  await visible(mobileAccess.getByRole('link',{name:'Sign up',exact:true}));await visible(mobileAccess.getByRole('link',{name:'Sign in',exact:true}));
  await mobileAccess.getByRole('link',{name:'Sign up',exact:true}).click();await visible(page.getByRole('heading',{name:'Create your account',exact:true}));
  await page.reload();await visible(page.getByRole('heading',{name:'Create your account',exact:true}));
  await page.getByRole('link',{name:'Already have an account? Sign in',exact:true}).click();await visible(page.getByRole('heading',{name:'Your account',exact:true}));
  await page.goBack();await visible(page.getByRole('heading',{name:'Create your account',exact:true}));await page.goForward();await visible(page.getByRole('heading',{name:'Your account',exact:true}));
  checkpoint='security session refresh';
  await page.getByRole('link',{name:'Forgot your password?',exact:true}).click();
  await fillPrivate(page.getByLabel('Email address',{exact:true}),'csrf-check-'+randomUUID()+'@example.invalid');
  const carrierContext=await browser.newContext();
  try{
    const carrierPage=await carrierContext.newPage();await carrierPage.goto(baseURL+'/account');
    await expect.poll(()=>carrierPage.getByRole('button',{name:'Sign in',exact:true}).isEnabled()).toBe(true);
    const carrier=(await carrierContext.cookies(baseURL)).find(cookie=>cookie.name==='__Host-sme_demo_pre');
    if(!carrier)throw new Error('A current native preauth cookie is required.');
    await page.context().addCookies([{name:carrier.name,value:carrier.value,url:baseURL+'/',secure:true,httpOnly:true,sameSite:'Lax'}]);
  }finally{await carrierContext.close();}
  let deniedPosts=0,refreshReads=0;
  const countDenied=(request:any)=>{const path=new URL(request.url()).pathname;if(request.method()==='POST'&&path==='/api/v1/auth/forgot-password')deniedPosts+=1;if(request.method()==='GET'&&path==='/api/v1/session')refreshReads+=1;};
  page.on('request',countDenied);await page.getByRole('button',{name:'Send reset instructions',exact:true}).click();
  await visible(page.getByRole('alert'));await expect.poll(()=>page.getByText('Your security session was refreshed. Please submit the form again.',{exact:true}).isVisible()).toBe(true);
  page.off('request',countDenied);expect(deniedPosts).toBe(1);expect(refreshReads).toBe(1);
  await page.goto(baseURL+'/account');
  checkpoint='guest cart';
  await page.getByRole('button',{name:'Start guest shopping',exact:true}).click();
  await expect.poll(()=>page.getByText('You’re shopping as a guest. Sign in or create an account to keep your orders together.',{exact:true}).isVisible()).toBe(true);
  await page.goto(baseURL+'/search');await expect(page.locator('.product-card').first()).toBeVisible();
  const productCount=Math.min(await page.locator('.product-card').count(),20);let added=false;
  for(let index=0;index<productCount;index++){
    await page.locator('.product-card').nth(index).locator('a').first().click();
    await visible(page.getByRole('button',{name:'Increase quantity',exact:true}));
    const add=page.getByRole('button',{name:'Add to your bag',exact:true});
    if(await add.isVisible()&&await add.isEnabled()){await add.click();added=true;break;}
    await page.goto(baseURL+'/search');await expect(page.locator('.product-card').first()).toBeVisible();
  }
  if(!added)throw new Error('BLOCKED: an available guest-cart product fixture is required.');
  await expect.poll(async()=>page.evaluate(async()=>{const cart=(await(await fetch('/api/v1/cart')).json()).data;return cart.lines.reduce((n:number,line:any)=>n+line.quantity,0)>0;})).toBe(true);
  const guest=await page.evaluate(async()=>{const session=(await(await fetch('/api/v1/session')).json()).data;const cart=(await(await fetch('/api/v1/cart')).json()).data;return {id:session.actor?.id,kind:session.actor?.kind,quantity:cart.lines.reduce((n:number,line:any)=>n+line.quantity,0)};});
  expect(guest.kind).toBe('guest');expect(guest.quantity>0).toBe(true);
  checkpoint='registration form';
  await page.setViewportSize({width:1280,height:900});
  const desktopAccess=page.getByRole('navigation',{name:'Account access',exact:true});await visible(desktopAccess.getByRole('link',{name:'Sign up',exact:true}));await visible(desktopAccess.getByRole('link',{name:'Sign in',exact:true}));
  await desktopAccess.getByRole('link',{name:'Sign up',exact:true}).click();
  await fillPrivate(page.getByLabel('Full name',{exact:true}),fullName);await fillPrivate(page.getByLabel('Email address',{exact:true}),email);
  await fillPrivate(page.getByLabel('Password',{exact:true}),'short');await fillPrivate(page.getByLabel('Confirm password',{exact:true}),'different');
  await page.getByRole('button',{name:'Create account',exact:true}).click();await expect.poll(()=>page.getByLabel('Confirm password',{exact:true}).getAttribute('aria-invalid')).toBe('true');
  await fillPrivate(page.getByLabel('Password',{exact:true}),password);await fillPrivate(page.getByLabel('Confirm password',{exact:true}),password);
  await page.getByRole('button',{name:'Create account',exact:true}).click();await visible(page.getByRole('heading',{name:'Check your email.',exact:true}));
  const pending=await page.evaluate(async()=>{const value=(await(await fetch('/api/v1/session')).json()).data;return {id:value.actor?.id,kind:value.actor?.kind};});expect(pending.kind).toBe('guest');expect(pending.id===guest.id).toBe(true);
  checkpoint='verification email';
  await page.getByRole('button',{name:'Resend verification email',exact:true}).click();await visible(page.getByRole('heading',{name:'Check your email.',exact:true}));
  const verification=await waitForAccountLink(email,'/verify-email');
  checkpoint='unverified sign in';
  await page.goto(baseURL+'/account');await fillPrivate(page.getByLabel('Email or reviewer username',{exact:true}),email);await fillPrivate(page.getByLabel('Password',{exact:true}),password);await page.getByRole('button',{name:'Sign in',exact:true}).click();await visible(page.getByRole('link',{name:'Request a fresh verification email',exact:true}));
  checkpoint='email verification';
  await openAccountLink(page,verification);await page.getByRole('button',{name:'Verify email',exact:true}).click();await visible(page.getByRole('heading',{name:'Your email is verified.',exact:true}));
  const verifiedSession=await page.evaluate(async()=>{const value=(await(await fetch('/api/v1/session')).json()).data;return {kind:value.actor?.kind,id:value.actor?.id};});expect(verifiedSession.kind).toBe('guest');expect(verifiedSession.id===guest.id).toBe(true);
  await openAccountLink(page,verification);await page.getByRole('button',{name:'Verify email',exact:true}).click();await visible(page.getByRole('heading',{name:'A fresh verification link is needed.',exact:true}));
  checkpoint='customer sign in and handoff';
  await customerSignIn(page,email,password);await visible(page.getByText(fullName,{exact:false}).first());
  const merged=await page.evaluate(async()=>{const session=(await(await fetch('/api/v1/session')).json()).data;const cart=(await(await fetch('/api/v1/cart')).json()).data;return {kind:session.actor?.kind,quantity:cart.lines.reduce((n:number,line:any)=>n+line.quantity,0)};});expect(merged.kind).toBe('customer');expect(merged.quantity).toBe(guest.quantity);
  await page.reload();await visible(page.getByRole('heading',{name:'Your current identity',exact:true}));
  const refreshedQuantity=await page.evaluate(async()=>{const cart=(await(await fetch('/api/v1/cart')).json()).data;return cart.lines.reduce((n:number,line:any)=>n+line.quantity,0);});expect(refreshedQuantity).toBe(guest.quantity);
  const privilege=await page.evaluate(async()=>{const session=(await(await fetch('/api/v1/session')).json()).data;const merchant=await fetch('/api/v1/merchant/catalog');return {roles:session.actor?.roles,status:merchant.status};});expect(privilege.roles).toEqual(['customer']);expect(privilege.status).toBe(403);
  await page.getByRole('link',{name:'Your orders',exact:true}).click();await page.reload();await expect.poll(()=>page.getByRole('heading',{name:'Your orders',exact:true}).isVisible()).toBe(true);
  const otherContext=await browser.newContext();
  try{
    const other=await otherContext.newPage();other.setDefaultTimeout(15000);await customerSignIn(other,email,password);
    await page.goto(baseURL+'/account');await page.getByRole('button',{name:'Sign out',exact:true}).click();
    await page.getByRole('link',{name:'Forgot your password?',exact:true}).click();await fillPrivate(page.getByLabel('Email address',{exact:true}),email);await page.getByRole('button',{name:'Send reset instructions',exact:true}).click();
    checkpoint='password reset email';
    const reset=await waitForAccountLink(email,'/reset-password');await openAccountLink(page,reset);
    checkpoint='password reset';
    await fillPrivate(page.getByLabel('New password',{exact:true}),newPassword);await fillPrivate(page.getByLabel('Confirm new password',{exact:true}),newPassword);await page.getByRole('button',{name:'Reset password',exact:true}).click();await visible(page.getByRole('heading',{name:'Your password has been reset.',exact:true}));
    checkpoint='previous session closure';
    await other.reload();const denied=await other.evaluate(async()=>{const first=await fetch('/api/v1/session'),a=await first.json();const second=await fetch('/api/v1/session'),b=await second.json();return {firstStatus:first.status,firstAnonymous:a.data?.actor===null,secondStatus:second.status,secondAnonymous:b.data?.actor===null};});expect(denied.firstStatus===401 || denied.firstStatus===200&&denied.firstAnonymous).toBe(true);expect(denied.secondStatus).toBe(200);expect(denied.secondAnonymous).toBe(true);await expect.poll(()=>other.getByRole('heading',{name:'Your current identity',exact:true}).isVisible()).toBe(false);
    checkpoint='used reset link';
    await openAccountLink(page,reset);await fillPrivate(page.getByLabel('New password',{exact:true}),newPassword);await fillPrivate(page.getByLabel('Confirm new password',{exact:true}),newPassword);await page.getByRole('button',{name:'Reset password',exact:true}).click();await visible(page.getByRole('heading',{name:'A fresh reset link is needed.',exact:true}));
    checkpoint='unverified sign in';
  await page.goto(baseURL+'/account');await fillPrivate(page.getByLabel('Email or reviewer username',{exact:true}),email);await fillPrivate(page.getByLabel('Password',{exact:true}),password);await page.getByRole('button',{name:'Sign in',exact:true}).click();await visible(page.getByRole('alert'));await expect.poll(()=>page.getByRole('heading',{name:'Your current identity',exact:true}).isVisible()).toBe(false);
    checkpoint='new password sign in';
    await customerSignIn(page,email,newPassword);await page.reload();await visible(page.getByRole('heading',{name:'Your current identity',exact:true}));
    const retainedQuantity=await page.evaluate(async()=>{const cart=(await(await fetch('/api/v1/cart')).json()).data;return cart.lines.reduce((n:number,line:any)=>n+line.quantity,0);});expect(retainedQuantity).toBe(guest.quantity);
    checkpoint='storage privacy';
    const storage=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));
    expect([password,newPassword,new URL(verification).hash.slice(7),new URL(reset).hash.slice(7)].some(value=>storage.includes(value))).toBe(false);
    await page.getByRole('button',{name:'Sign out',exact:true}).click();await visible(page.getByRole('button',{name:'Sign in',exact:true}));
  }finally{await otherContext.close();}
  const credentialFile=process.env.SME_TEST_REGISTERED_CUSTOMER_FILE;
  if(credentialFile){
    const parent=lstatSync(dirname(credentialFile));
    if(!parent.isDirectory()||parent.isSymbolicLink()||(parent.mode&0o077)!==0||parent.uid!==process.getuid?.())throw new Error('Private current-user credential fixture directory required.');
    writeFileSync(credentialFile,JSON.stringify({profile:'demo-test',run_id:runId,email,username:email,full_name:fullName,password:newPassword}),{mode:0o600,flag:'wx'});
  }
  registeredCustomer={email,password:newPassword,full_name:fullName};
  await safeEvidence(info,'ACCOUNT-ACCESS',{registration:'verified email activates customer only',guest_cart:'retained then merged by normal sign-in',recovery:'one-use link; old sessions denied; new password signs in',privacy:'no password or link token in browser storage; fragments removed',navigation:'mobile entry/direct refresh/back/forward'});
  }catch{throw new Error(`Account access failed at ${checkpoint}. Private input and link details are withheld.`);}
});

test('auth · pending merchant verification safely exits for a customer account form',async({page},info)=>{
  test.setTimeout(60000);page.setDefaultTimeout(10000);let checkpoint='merchant password';
  try{
    await page.goto(baseURL+'/account');await fillPrivate(page.getByLabel('Email or reviewer username',{exact:true}),accounts.merchant.username);await fillPrivate(page.getByLabel('Password',{exact:true}),accounts.merchant.password);await page.getByRole('button',{name:'Sign in',exact:true}).click();await visible(page.getByLabel('Authenticator code',{exact:true}));
    checkpoint='customer form under pending verification';
    await page.goto(baseURL+'/forgot-password');await visible(page.getByRole('button',{name:'Send reset instructions',exact:true}));
    const read=await page.evaluate(async()=>{
      const session=(await(await fetch('/api/v1/session')).json()).data;
      const response=await fetch('/api/v1/auth/action-status?operation_id='+crypto.randomUUID(),{headers:{'X-CSRF-Token':session.csrf_token}});
      const current=(await(await fetch('/api/v1/session')).json()).data;
      return {status:response.status,before:Boolean(session.challenge),after:Boolean(current.challenge),anonymous:current.actor===null};
    });
    expect(read.status).toBe(409);expect(read.before).toBe(true);expect(read.after).toBe(true);expect(read.anonymous).toBe(true);
    const email='no-mail-'+randomUUID()+'@example.invalid';await fillPrivate(page.getByLabel('Email address',{exact:true}),email);
    let posts=0;const count=(request:any)=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/v1/auth/forgot-password')posts+=1;};page.on('request',count);
    const response=page.waitForResponse(value=>value.request().method()==='POST'&&new URL(value.url()).pathname==='/api/v1/auth/forgot-password');
    await page.getByRole('button',{name:'Send reset instructions',exact:true}).click();const result=await response;const envelope=await result.json();
    expect(result.status()).toBe(401);expect(envelope.error?.code).toBe('customer_preauth_required');await visible(page.getByRole('alert'));
    await expect.poll(()=>page.getByText('Merchant verification was canceled. Submit this customer account form again.',{exact:true}).isVisible()).toBe(true);
    page.off('request',count);expect(posts).toBe(1);
    checkpoint='native challenge closure';
    const state=await page.evaluate(async()=>{const response=await fetch('/api/v1/session');const data=(await response.json()).data;return {status:response.status,anonymous:data?.actor===null,challenge:data?.challenge===null};});expect(state).toEqual({status:200,anonymous:true,challenge:true});
    expect(Boolean(capturedAccountLink(email,'/reset-password'))).toBe(false);
    expect((await page.context().cookies()).some(cookie=>cookie.name==='__Secure-sme_demo_challenge')).toBe(false);
    await safeEvidence(info,'ACCOUNT-MFA-TRANSITION',{read_status:409,read_preserves_challenge:true,first_post:401,automatic_mutation_retry:false,challenge_cookie_cleared:true,native_session:'anonymous fresh preauth',mail_for_denied_request:false});
  }catch{throw new Error(`Customer account transition failed at ${checkpoint}. Private authentication details are withheld.`);}
});


test('account access · a committed reset with lost reply reuses its key after session renewal',async({page},info)=>{
  test.setTimeout(180000);page.setDefaultTimeout(15000);let checkpoint='registered customer fixture';
  try{
    let customer=registeredCustomer;
    if(!customer){
      const file=process.env.SME_TEST_REGISTERED_CUSTOMER_INPUT_FILE;if(!file)throw new Error('A private registered-customer fixture is required.');
      const metadata=lstatSync(file);if(!metadata.isFile()||metadata.isSymbolicLink()||(metadata.mode&0o077)!==0||metadata.uid!==process.getuid?.())throw new Error('Private current-user customer fixture required.');
      const value=JSON.parse(readFileSync(file,'utf8'));if(value.profile!=='demo-test'||value.run_id!==runId||typeof value.email!=='string'||typeof value.password!=='string')throw new Error('Registered customer fixture binding mismatch.');
      customer={email:value.email,password:value.password,full_name:value.full_name};
    }
    if(process.env.SME_TEST_PUBLIC_RECOVERY==='1') {
      checkpoint='public recovery of stale test credential';
      const link=await freshResetLink(page,customer.email),password='A recovered password '+randomUUID();
      savePrivateCustomer({...customer,password},true);await openAccountLink(page,link);
      await fillPrivate(page.getByLabel('New password',{exact:true}),password);await fillPrivate(page.getByLabel('Confirm new password',{exact:true}),password);
      await page.getByRole('button',{name:'Reset password',exact:true}).click();await visible(page.getByRole('heading',{name:'Your password has been reset.',exact:true}));
      customer={...customer,password};savePrivateCustomer(customer);registeredCustomer=customer;
    }
    const previous=capturedAccountMail(customer.email,'/reset-password');
    if(previous){const notBefore=previous.receivedAt+61000;if(notBefore-Date.now()>65000)throw new Error('Private capture clock mismatch.');await expect.poll(()=>Date.now()>=notBefore,{timeout:65000,intervals:[1000]}).toBe(true);}
    checkpoint='signed-in reset request';
    await customerSignIn(page,customer.email,customer.password);
    const oldAuth=(await page.context().cookies(baseURL)).find(cookie=>cookie.name==='__Host-sme_demo');if(!oldAuth)throw new Error('Current native customer cookie required.');
    await page.goto(baseURL+'/forgot-password');await fillPrivate(page.getByLabel('Email address',{exact:true}),customer.email);await page.getByRole('button',{name:'Send reset instructions',exact:true}).click();
    await expect.poll(()=>{const value=capturedAccountLink(customer!.email,'/reset-password');return Boolean(value&&value!==previous?.link);},{timeout:30000}).toBe(true);
    const reset=capturedAccountLink(customer.email,'/reset-password')!;await openAccountLink(page,reset);
    const touch=page.waitForResponse(value=>value.request().method()==='POST'&&new URL(value.url()).pathname==='/api/v1/auth/touch');
    await page.getByLabel('New password',{exact:true}).click();expect((await touch).status()).toBe(200);await page.waitForLoadState('networkidle');
    const password='A recovered password '+randomUUID();savePrivateCustomer({...customer,password},true);
    const keys:string[]=[];let committedStatus:number|null=null,committedOperation:string|null=null,committedState:string|null=null;
    const count=(request:any)=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/v1/auth/reset-password')keys.push(request.headers()['idempotency-key']);};page.on('request',count);
    const drop=async(route:any)=>{
      try{
        const response=await route.fetch();committedStatus=response.status();const envelope=await response.json();committedOperation=envelope.data?.operation_id||null;committedState=envelope.data?.state||null;
        if(committedStatus===200&&committedState==='password_reset'){savePrivateCustomer({...customer!,password});registeredCustomer={...customer!,password};}
        // Model a response lost before its cookie headers reach the browser. The
        // restored credential is the actual now-revoked native session, never a grant.
        await page.context().addCookies([{name:oldAuth.name,value:oldAuth.value,url:baseURL+'/',secure:true,httpOnly:true,sameSite:'Lax'}]);
      }catch{committedStatus=committedStatus||0;}finally{await route.abort('failed');}
    };
    await page.route('**/api/v1/auth/reset-password',drop);
    checkpoint='real committed response loss';
    await fillPrivate(page.getByLabel('New password',{exact:true}),password);await fillPrivate(page.getByLabel('Confirm new password',{exact:true}),password);await page.getByRole('button',{name:'Reset password',exact:true}).click();
    await expect.poll(()=>committedStatus!==null).toBe(true);expect(committedStatus).toBe(200);expect(committedState).toBe('password_reset');await visible(page.getByRole('alert'));
    await page.unroute('**/api/v1/auth/reset-password',drop);expect(keys.length).toBe(1);
    checkpoint='revoked carrier renewal';
    const renewedReply=page.waitForResponse(value=>value.request().method()==='POST'&&new URL(value.url()).pathname==='/api/v1/auth/reset-password');
    await page.getByRole('button',{name:'Reset password',exact:true}).click();const renewedHttp=await renewedReply;const renewedBody=await renewedHttp.json();const retryAfter=renewedHttp.headers()['retry-after'];const renewalDiagnostic={status:renewedHttp.status(),error_code:typeof renewedBody.error?.code==='string'&&/^[a-z0-9_]{1,80}$/.test(renewedBody.error.code)?renewedBody.error.code:null,retry_after:retryAfter&&/^\d{1,6}$/.test(retryAfter)?Number(retryAfter):null,same_original_key:keys.length===2&&keys[0]===keys[1]};await info.attach('safe-lost-reset-carrier-renewal',{body:Buffer.from(JSON.stringify(renewalDiagnostic)),contentType:'application/json'});expect(renewedHttp.status()).toBe(401);expect(keys.length).toBe(2);
    await expect.poll(async()=>{const input=page.getByLabel('New password',{exact:true});return await input.isVisible()&&(await input.inputValue())==='';}).toBe(true);
    checkpoint='same-intent password reentry';
    await fillPrivate(page.getByLabel('New password',{exact:true}),password);await fillPrivate(page.getByLabel('Confirm new password',{exact:true}),password);
    const receipt=page.waitForResponse(value=>value.request().method()==='POST'&&new URL(value.url()).pathname==='/api/v1/auth/reset-password');await page.getByRole('button',{name:'Reset password',exact:true}).click();const response=await receipt;const envelope=await response.json();
    const scopedOperation=envelope.data?.operation_id;const diagnostic={status:response.status(),state:envelope.data?.state==='password_reset'?'password_reset':'other',same_key:keys.every(key=>key===keys[0]),posts:keys.length,fresh_receipt:typeof scopedOperation==='string'&&/^[0-9a-f-]{36}$/.test(scopedOperation)&&scopedOperation!==committedOperation,error_code:typeof envelope.error?.code==='string'&&/^[a-z0-9_]{1,80}$/.test(envelope.error.code)?envelope.error.code:null};await info.attach('safe-lost-reset-reentry',{body:Buffer.from(JSON.stringify(diagnostic)),contentType:'application/json'});expect(response.status()).toBe(200);expect(envelope.data?.state).toBe('password_reset');expect(diagnostic.fresh_receipt).toBe(true);await visible(page.getByRole('heading',{name:'Your password has been reset.',exact:true}));
    const scopes=await page.evaluate(async({current,previous})=>{const session=(await(await fetch('/api/v1/session')).json()).data;const headers={'X-CSRF-Token':session.csrf_token};const currentReply=await fetch('/api/v1/auth/action-status?operation_id='+current,{headers}),currentBody=await currentReply.json();const oldReply=await fetch('/api/v1/auth/action-status?operation_id='+previous,{headers});return {current:currentReply.status,completed:currentBody.data?.state==='password_reset',old:oldReply.status};},{current:scopedOperation,previous:committedOperation});expect(scopes).toEqual({current:200,completed:true,old:404});
    page.off('request',count);expect(keys.length).toBe(3);expect(keys.every(key=>key===keys[0])).toBe(true);
    checkpoint='native new password sign in';await customerSignIn(page,customer.email,password);
    const storage=await page.evaluate(()=>JSON.stringify({local:{...localStorage},session:{...sessionStorage}}));expect(storage.includes(password)||storage.includes(new URL(reset).hash.slice(7))).toBe(false);
    registeredCustomer={...customer,password};const file=process.env.SME_TEST_REGISTERED_CUSTOMER_FILE;
    if(file){const parent=lstatSync(dirname(file));if(!parent.isDirectory()||parent.isSymbolicLink()||(parent.mode&0o077)!==0||parent.uid!==process.getuid?.())throw new Error('Private fixture output required.');try{const existing=lstatSync(file);if(!existing.isFile()||existing.isSymbolicLink()||(existing.mode&0o077)!==0||existing.uid!==process.getuid?.())throw new Error('Private fixture output required.');}catch(error:any){if(error.code!=='ENOENT')throw error;}writeFileSync(file,JSON.stringify({profile:'demo-test',run_id:runId,...registeredCustomer,username:customer.email}),{mode:0o600});}
    await safeEvidence(info,'ACCOUNT-LOST-RESET',{transport:'actual backend completed200 then response aborted',old_carrier:'native401 renewed to fresh preauth',explicit_posts:3,same_original_key:true,new_carrier_scoped_receipt:true,original_carrier_receipt_denied:true,new_password_signin:true,browser_secret_storage:false});
  }catch{throw new Error(`Lost reset recovery failed at ${checkpoint}. Private inputs and response details are withheld.`);}
});
