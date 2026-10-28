import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { ApiError, BrowserApiClient, BrowserRequestStore, money, renderPage, validateMoney, normalizeProducts, validateSessionDTO, parseAccountActionResult, validateAccountActionInput, parseAccountLink, validateAccountFields, ResetIntentIdentity, accountFieldErrorsFromApi, isAdministrator } from '../../dist/server/browser.mjs';


const sessionFixture=(csrf='test-csrf',id='00000000-0000-4000-8000-00000000000a')=>({profile:'demo-test',csrf_token:csrf,actor:{id,kind:'customer',roles:['customer']},epoch:'fixture-epoch',session_generation:1,challenge:null});
const prime=(client,csrf='test-csrf')=>client.apply_session(sessionFixture(csrf));

const originalFetch = globalThis.fetch;
const originalStorage = globalThis.sessionStorage;
afterEach(() => { globalThis.fetch = originalFetch; if (originalStorage) globalThis.sessionStorage = originalStorage; else delete globalThis.sessionStorage; });

// AC45/49: a resource generation and session epoch must both match before publication.
test('AC49 rejects stale resource responses and session epochs', () => {
  const store = new BrowserRequestStore();
  const old = store.begin_request('/orders');
  const current = store.begin_request('/orders');
  assert.equal(store.can_publish(old), false);
  assert.equal(store.can_publish(current), true);
  store.on_session_changed('new-session');
  assert.equal(store.can_publish(current), false);
  const fresh = store.begin_request('/orders');
  store.invalidate('/orders');
  assert.equal(store.can_publish(fresh), false);
});

// AC07/19: fractional and coerced monetary/quantity output cannot reach a purchase decision.
test('AC07 rejects noninteger exact amounts and excessive structures', () => {
  for (const input of [{ total_minor: 2.4 }, { tax_minor: '200' }, { quantity: true }, { total_minor: Number.MAX_SAFE_INTEGER + 1 }, { total_minor: -1 }, { total_minor: 100000000 }]) assert.throws(() => validateMoney(input), ApiError);
  assert.doesNotThrow(() => validateMoney({ total_minor: 1050, lines: [{ quantity: 2, unit_minor: 500 }], tax_minor: 50 }));
  assert.equal(money(1050), '$10.50');
  assert.equal(money(3.3), 'Unavailable');
  let nested = {}; for (let i = 0; i < 32; i += 1) nested = { nested };
  assert.throws(() => validateMoney(nested), ApiError);
});

// AC07/45: transport failures are allowed to have no JSON error body.
test('AC45 empty-body service error has a bounded safe message', async () => {
  globalThis.fetch = async () => new Response(null, { status: 503 });
  const client = new BrowserApiClient(); prime(client);
  await assert.rejects(client.query('/catalog'), error => error instanceof ApiError && error.status === 503 && error.message.includes('503'));
});

// AC26/29: a lost reply preserves the original command identity, never private input in storage.
test('AC29 uncertain mutation reuses its original command and stores only safe references', async () => {
  const writes = new Map();
  globalThis.sessionStorage = { setItem: (key, value) => writes.set(key, value), removeItem: key => writes.delete(key) };
  const client = new BrowserApiClient(); prime(client,'csrf-not-an-auth-credential');
  const bodies = [];
  globalThis.fetch = async (_url, options) => { bodies.push(JSON.parse(options.body)); throw new TypeError('lost response'); };
  const input = { checkout_id: 'checkout-opaque', accepted_quote_id: 'quote-opaque', email: 'private@example.test' };
  await assert.rejects(client.mutate('/purchases/commands', 'submit', input), error => error.code === 'request_unknown');
  const retained = [...writes.values()].join('');
  assert.equal(retained.includes('private@example.test'), false);
  assert.equal(retained.includes('csrf-not-an-auth-credential'), false);
  globalThis.fetch = async (_url, options) => { bodies.push(JSON.parse(options.body)); return Response.json({ data: { id: 'purchase-opaque', state: 'PAID' }, meta: {} }); };
  await client.mutate('/purchases/commands', 'submit', input);
  assert.equal(bodies[0].command_id, bodies[1].command_id);
  assert.equal(writes.size, 0);
});

// AC49/53: a private response arriving after logout must not publish.
test('AC49 private response cannot publish after a session change', async () => {
  let release;
  globalThis.fetch = () => new Promise(resolve => { release = resolve; });
  const client = new BrowserApiClient(); prime(client);
  const pending = client.query('/orders');
  client.on_session_changed('logged-out');
  release(Response.json({ data: { orders: [{ id: 'private-order' }] } }));
  await assert.rejects(pending, error => error.code === 'stale_session');
});

// AC07/45: a successful HTTP status cannot turn malformed totals into usable commerce data.
test('AC45 malformed totals in a successful response fail closed', async () => {
  globalThis.fetch = async () => Response.json({ data: { id: 'checkout', quote: { total_minor: 12.25 } } });
  const client = new BrowserApiClient(); prime(client);
  await assert.rejects(client.query('/checkout/opaque'), error => error.code === 'service_contract_error');
});

// AC50/SSR: bootstrap data cannot terminate its script or create executable markup.
test('AC50 SSR escapes dangerous bootstrap values and preserves nonce', () => {
  const document = renderPage('/', { catalog: { products: [{ id: 'p', slug: 'safe', name: '</script><script>alert(1)</script>', description: 'Safe', variants: [{ id: 'v', sku: 'SKU', unit_minor: 100, available: 1 }] }], categories: [] } }, { nonce: 'testnonce' });
  assert.equal(document.startsWith('<!DOCTYPE html>'), true);
  assert.equal(document.includes('id="sme-initial-data" type="application/json"'), true);
  const embedded=document.match(/<script id="sme-initial-data"[^>]*>(.*?)<\/script>/s)[1];
  assert.equal(JSON.parse(embedded).catalog.products[0].name, '</script><script>alert(1)</script>');
  assert.equal(document.includes('nonce="testnonce"'), true);
  assert.equal(document.includes('</script><script>alert(1)</script>'), false);
  assert.equal(document.includes('\\u003c/script\\u003e'), true);
  assert.equal(document.includes('No real card'), false);
  assert.equal((document.match(/<h1/g) || []).length, 1);
  assert.equal(/\sstyle=/.test(document),false);
});

// AC02/45 regression: one first-party bootstrap establishes the cookie/CSRF pair before parallel reads.
test('AC02 initial parallel reads share one session bootstrap', async () => {
  const calls = []; let establish;
  globalThis.fetch = async (url) => {
    calls.push(url);
    if(url.endsWith('/session')) return new Promise(resolve => { establish = () => resolve(Response.json({data:sessionFixture('paired-token')})); });
    return Response.json({data:{items:[]}});
  };
  const client = new BrowserApiClient();
  const catalog = client.query('/orders'); const cart = client.query('/cart');
  assert.deepEqual(calls, ['/api/v1/session']);
  establish(); await Promise.all([catalog,cart]);
  assert.equal(calls.filter(url=>url.endsWith('/session')).length,1);
  assert.equal(client.csrf,'paired-token');
});

// AC26: a syntactically successful but empty mutation response is still an unknown operation.
test('AC26 empty successful mutation output preserves its operation key', async () => {
  const client=new BrowserApiClient();prime(client,'csrf');
  globalThis.fetch=async()=>new Response(null,{status:200});
  await assert.rejects(client.mutate('/purchases/commands','submit',{checkout_id:'c'}),error=>error.code==='service_contract_error');
  assert.equal(client.store.pending.size,1);
});

// AC26: reload may reconstruct safe operation references without retaining private command input.
test('AC26 restored unresolved operation reference keeps the same key', async () => {
  const key='00000000-0000-4000-8000-000000000001';
  const records=new Map([['sme-operation:00000000-0000-4000-8000-00000000000a:'+key,JSON.stringify({id:key,principal_scope:'00000000-0000-4000-8000-00000000000a',resource:'/purchases/commands',operation:'submit',started_at:1})]]);
  globalThis.sessionStorage={length:1,key:index=>index===0?'sme-operation:00000000-0000-4000-8000-00000000000a:'+key:null,getItem:name=>records.get(name),setItem:(name,value)=>records.set(name,value),removeItem:name=>records.delete(name)};
  let body;globalThis.fetch=async(_url,options)=>{body=JSON.parse(options.body);return Response.json({data:{id:'p',state:'PAID'}});};
  const client=new BrowserApiClient();prime(client,'csrf');
  await client.mutate('/purchases/commands','submit',{checkout_id:'c'});
  assert.equal(body.command_id,key);
});

// PF-AUTH01/05: authentication uses dedicated schemas and never enters commerce replay storage.
test('PF auth exact dedicated body never stores credentials',async()=>{
 const records=new Map();globalThis.sessionStorage={length:0,key:()=>null,getItem:()=>null,setItem:(k,v)=>records.set(k,v),removeItem:k=>records.delete(k)};
 const client=new BrowserApiClient();client.apply_session({...sessionFixture('preauth-csrf'),actor:null});const calls=[];
 globalThis.fetch=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return Response.json({data:{...sessionFixture('challenge-csrf'),actor:null,challenge:{id:'challenge',state:'mfa_required',expires_at:1}}});};
 await client.authenticate('login',{username:'reviewer',password:'test-only-password-canary'});
 assert.deepEqual(calls,[{url:'/api/v1/auth/login',body:{username:'reviewer',password:'test-only-password-canary'}}]);assert.equal(client.store.pending.size,0);assert.equal(records.size,0);assert.equal(client.session.actor,null);assert.equal(client.session.challenge.state,'mfa_required');
});
// PF-UI05: a common401 transition invalidates private-state subscribers before another action.
test('PF401 clears old session and accepts only restricted preauth',async()=>{
 const client=new BrowserApiClient();prime(client);const transitions=[];client.observe_session(value=>transitions.push(value?.actor||null));client.store.remember('old-operation',{resource:'/orders/commands',operation:'cancel',started_at:1});
 globalThis.fetch=async()=>Response.json({error:{code:'session_expired',message:'Session expired',details:{preauth:{csrf_token:'fresh-preauth',epoch:'fresh',session_generation:2,expires_at:100}}},meta:{}},{status:401});
 await assert.rejects(client.query('/orders'),error=>error.status===401);assert.equal(client.session.actor,null);assert.equal(client.csrf,'fresh-preauth');assert.equal(client.store.pending.size,0);assert.ok(transitions.every(value=>value===null));
});
// PF-C02/PF-P03: public discovery does not silently create a guest account.
test('PF public catalog needs no session or guest request',async()=>{
 const urls=[];globalThis.fetch=async url=>{urls.push(url);return Response.json({data:{products:[],categories:[]}});};await new BrowserApiClient().query('/catalog');assert.deepEqual(urls,['/api/v1/catalog']);
});
// PF-UI05: safe unresolved references are partitioned by server-bound principal.
test('PF pending references never cross principal scopes',()=>{
 const values=new Map();globalThis.sessionStorage={get length(){return values.size;},key:i=>[...values.keys()][i]||null,getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
 const store=new BrowserRequestStore();store.bind_scope('principal-a');store.remember('op-a',{resource:'/purchases/commands',operation:'submit',started_at:1});store.on_session_changed('new-epoch');store.bind_scope('principal-b');assert.equal(store.pending.size,0);store.bind_scope('principal-a');assert.equal(store.pending.has('op-a'),true);
});
// PF-AUTH01: uncertain login observes the cookie once without credential resubmission.
test('PF lost login response observes current session once',async()=>{
 const client=new BrowserApiClient();client.apply_session({...sessionFixture('preauth'),actor:null});let posts=0,reads=0;globalThis.fetch=async(_url,options)=>{if(options.method==='POST'){posts+=1;throw new TypeError('lost reply');}reads+=1;return Response.json({data:sessionFixture('observed')});};
 const observed=await client.authenticate('login',{username:'reviewer',password:'transient-only'});assert.equal(observed.actor.kind,'customer');assert.equal(posts,1);assert.equal(reads,1);assert.equal(client.store.pending.size,0);
});

// PF-P01: missing public prices/availability cannot silently become a purchasable default.
test('PF incomplete catalog and unknown session fields fail closed',()=>{
 assert.throws(()=>normalizeProducts({products:[{id:'p',slug:'object',name:'Object',variants:[{id:'v',sku:'SKU',unit_minor:100}]}]}),error=>error.code==='service_contract_error');
 assert.throws(()=>validateSessionDTO({...sessionFixture(),is_admin:true}),error=>error.code==='service_contract_error');
 assert.throws(()=>validateSessionDTO({...sessionFixture(),actor:{id:'p',kind:'staff',roles:['superuser']}}),error=>error.code==='service_contract_error');
});

// PF-UI05: an older session bootstrap cannot replace a newly bound identity.
test('PF delayed old session cannot overwrite newer login',async()=>{
 const client=new BrowserApiClient();prime(client,'old-csrf');let release;
 globalThis.fetch=()=>new Promise(resolve=>{release=resolve;});
 const old=client.query('/session');
 client.apply_session({...sessionFixture('new-csrf','00000000-0000-4000-8000-00000000000b'),session_generation:2});
 release(Response.json({data:sessionFixture('old-csrf')}));
 await assert.rejects(old,error=>error.code==='stale_session');
 assert.equal(client.csrf,'new-csrf');assert.equal(client.session.actor.id,'00000000-0000-4000-8000-00000000000b');
});
// PF-UI05: a late expired-session response must not clear a newer login.
test('PF stale401 cannot invalidate current identity',async()=>{
 const client=new BrowserApiClient();prime(client,'old-csrf');let release;
 globalThis.fetch=()=>new Promise(resolve=>{release=resolve;});
 const old=client.query('/orders');
 client.apply_session({...sessionFixture('new-csrf','00000000-0000-4000-8000-00000000000b'),session_generation:2});
 release(Response.json({error:{code:'session_expired',details:{preauth:{csrf_token:'stale-preauth',epoch:'expired',session_generation:3,expires_at:100}}}},{status:401}));
 await assert.rejects(old,error=>error.code==='stale_session');
 assert.equal(client.csrf,'new-csrf');assert.equal(client.session.actor.id,'00000000-0000-4000-8000-00000000000b');
});
// PF-UI05: an unmounted private route waiting on bootstrap never starts a later read.
test('PF aborted private bootstrap cannot start an orphan read',async()=>{
 const client=new BrowserApiClient(),controller=new AbortController(),urls=[];let establish;
 globalThis.fetch=async url=>{urls.push(url);if(url.endsWith('/session'))return new Promise(resolve=>{establish=resolve;});return Response.json({data:{orders:[]}});};
 const pending=client.query('/orders',{},controller.signal);controller.abort();establish(Response.json({data:sessionFixture('current')}));
 await assert.rejects(pending,error=>error.name==='AbortError');assert.deepEqual(urls,['/api/v1/session']);
});
// PF-UI05: metadata emitted by this page cannot clear its newly published identity.
test('PF own session broadcast keeps the mounted private identity',async()=>{
 const client=new BrowserApiClient();prime(client,'current-csrf');let reads=0;globalThis.fetch=async()=>{reads++;throw new Error('No self-notice read allowed');};
 await client.receive_session_notice({invalidate:true,sender:client.instanceId});assert.equal(reads,0);assert.equal(client.csrf,'current-csrf');
});
// PF-UI05: a different page still clears private data synchronously before reading its current identity.
test('PF remote session broadcast clears private state before observation',async()=>{
 const client=new BrowserApiClient();prime(client,'old-csrf');let release;
 globalThis.fetch=()=>new Promise(resolve=>{release=resolve;});
 const observed=client.receive_session_notice({invalidate:true,sender:'another-page'});assert.equal(client.session,null);assert.equal(client.csrf,'');
 release(Response.json({data:sessionFixture('new-csrf','00000000-0000-4000-8000-00000000000b')}));await observed;assert.equal(client.csrf,'new-csrf');
});


test('customer account actions accept only their explicit result contract', () => {
  assert.deepEqual(parseAccountActionResult('register', {state:'verification_pending',message:'Check your email.',delivery_status:'queued'}), {state:'verification_pending',message:'Check your email.',delivery_status:'queued'});
  assert.throws(()=>parseAccountActionResult('register',sessionFixture()), error=>error.code==='service_contract_error');
  assert.throws(()=>parseAccountActionResult('verify-email',{state:'verification_pending',message:'Wait'}), error=>error.code==='service_contract_error');
  assert.throws(()=>parseAccountActionResult('forgot-password',{state:'reset_requested',message:'Check email',token:'never-public'}), error=>error.code==='service_contract_error');
  assert.throws(()=>parseAccountActionResult('register',{state:'verification_pending',message:'Check email',delivery_status:'sent'}), error=>error.code==='service_contract_error');
  assert.throws(()=>validateAccountActionInput('register',{full_name:'Customer',email:'customer@example.invalid',password:'synthetic password',password_confirmation:'synthetic password',roles:['merchant_admin']}), error=>error.code==='invalid_input');
});

test('email account links accept a single fragment token for its dedicated route',()=>{
  const token='a'.repeat(43);
  assert.deepEqual(parseAccountLink('/verify-email','#token='+token),{kind:'verify-email',token});
  assert.equal(parseAccountLink('/verify-email','?token='+token),null);
  assert.equal(parseAccountLink('/account','#token='+token),null);
  assert.equal(parseAccountLink('/verify-email','#token='+token+'&token='+token),null);
  assert.equal(parseAccountLink('/reset-password','#token='+token+'&redirect=https://other.invalid'),null);
});

test('registration and recovery field validation catches invalid and mismatched values',()=>{
  const valid={full_name:'Sample Customer',email:'customer@example.invalid',password:'A long sample password',password_confirmation:'A long sample password'};
  assert.deepEqual(validateAccountFields(valid,'register'),{});
  const unicode='😀'.repeat(128);assert.deepEqual(validateAccountFields({...valid,full_name:'😀'.repeat(120),password:unicode,password_confirmation:unicode},'register'),{});
  const errors=validateAccountFields({...valid,full_name:'A',email:'invalid',password:'short',password_confirmation:'different'},'register');
  assert.deepEqual(Object.keys(errors),['full_name','email','password','password_confirmation']);
  assert.deepEqual(validateAccountFields({...valid,password:''},'forgot-password'),{});
});


test('uncertain reset reentry reuses only its exact transient intent key',async()=>{
  let count=0;const identity=new ResetIntentIdentity(()=>`intent-${++count}`),token='t'.repeat(43),password='A private test password';
  const first=await identity.identityFor(token,password);
  const known=await identity.identityFor(token,password);assert.notEqual(first.id,known.id);
  identity.markUncertain();
  const same=await identity.identityFor(token,password);assert.equal(same.id,known.id);assert.equal(same.reused,true);
  assert.equal(JSON.stringify(identity).includes(password),false);assert.equal(JSON.stringify(identity).includes(token),false);
  const changed=await identity.identityFor(token,password+' changed');assert.notEqual(changed.id,same.id);assert.equal(changed.reused,false);
  identity.markUncertain();const freshToken=await identity.identityFor('n'.repeat(43),password+' changed');assert.notEqual(freshToken.id,changed.id);
  identity.markUncertain();identity.clear();const cleared=await identity.identityFor('n'.repeat(43),password+' changed');assert.notEqual(cleared.id,freshToken.id);
});

test('canonical IDNA email length stays authoritative on the server',()=>{
  const email='a'.repeat(64)+'@'+Array(3).fill('é'.repeat(50)).join('.');
  assert.equal(new TextEncoder().encode(email).length>254,true);
  assert.deepEqual(validateAccountFields({full_name:'Test Customer',email,password:'A long test password',password_confirmation:'A long test password'},'register'),{});
});

test('server field errors retain only safe named account fields',()=>{
  assert.deepEqual(accountFieldErrorsFromApi({field_errors:{email:'Enter a valid email.',roles:'Administrator',password:'do not echo private-value'}},['private-value']),{email:'Enter a valid email.'});
  assert.deepEqual(accountFieldErrorsFromApi({errors:['unexpected']}),{});
});


test('clearing a reset link fences an in-flight digest before key allocation',async()=>{
  let count=0;const identity=new ResetIntentIdentity(()=>`intent-${++count}`);
  const pending=identity.identityFor('old'.repeat(15),'A private test password');identity.clear();
  await assert.rejects(pending,error=>error.code==='stale_account_intent');assert.equal(count,0);
  const next=await identity.identityFor('new'.repeat(15),'A private test password');assert.equal(next.id,'intent-1');
});

test('known reset completion survives a failed following session read without publishing a session',async()=>{
  const client=new BrowserApiClient();prime(client);let posts=0,reads=0;
  globalThis.fetch=async(_url,options)=>{if(options.method==='POST'){posts++;return Response.json({data:{state:'password_reset',message:'Password updated.',delivery_status:'NOT_APPLICABLE'}});}reads++;throw new TypeError('read interrupted');};
  const result=await client.accountAction('reset-password',{token:'t'.repeat(43),password:'A test password',password_confirmation:'A test password'},'00000000-0000-4000-8000-000000000001');
  assert.equal(result.state,'password_reset');assert.equal(posts,1);assert.equal(reads,1);assert.equal(client.session,null);assert.equal(client.csrf,'');
});


test('administrator navigation requires an authenticated staff administrator role',()=>{
  assert.equal(isAdministrator(null),false);
  assert.equal(isAdministrator({...sessionFixture(),actor:{id:'c',kind:'customer',roles:['customer']}}),false);
  assert.equal(isAdministrator({...sessionFixture(),actor:{id:'c',kind:'customer',roles:['merchant_admin']}}),false);
  assert.equal(isAdministrator({...sessionFixture(),actor:{id:'s',kind:'staff',roles:['support']}}),false);
  assert.equal(isAdministrator({...sessionFixture(),actor:{id:'s',kind:'staff',roles:['merchant_admin']}}),true);
});

// Server-rendered gallery controls cannot accept keyboard input before session bootstrap remounts finish.
test('SSR gallery controls wait for hydrated current-session readiness', () => {
  const document=renderPage('/products/gallery', {catalog:{products:[{id:'gallery',slug:'gallery',name:'Gallery object',variants:[{id:'variant',sku:'GALLERY',unit_minor:100,available:1}],media:[{url:'/assets/demo/one.svg',alt_text:'First view'},{url:'/assets/demo/two.svg',alt_text:'Second view'}]}]}});
  const controls=[...document.matchAll(/<button\b[^>]*aria-label="Show product image [12]"[^>]*>/g)].map(match=>match[0]);
  assert.equal(controls.length,2);
  assert.equal(controls.every(control=>/\sdisabled(?:="")?(?:\s|>)/.test(control)),true);
});
