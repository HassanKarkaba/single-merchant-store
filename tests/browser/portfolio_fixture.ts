/** A local Firefox CA profile can supplement the canonical Chromium gate.
 * The profile holds a CA trust anchor only. System trust and certificate validation stay unchanged.
 */
import {test as base, expect} from '@playwright/test';
import {lstatSync} from 'node:fs';
process.env.PLAYWRIGHT_NO_COPY_PROMPT='1';
const profile=process.env.SME_FIREFOX_TRUST_PROFILE;
export const test=profile ? base.extend({
  browser:[async({playwright},use)=>{
    const info=lstatSync(profile);
    if(!info.isDirectory()||info.isSymbolicLink()||(info.mode&0o077)!==0||info.uid!==process.getuid?.())throw new Error('Private current-user CA profile required');
    const root=await playwright.firefox.launchPersistentContext(profile,{headless:true,ignoreHTTPSErrors:false,firefoxUserPrefs:{'network.dns.localDomains':'store.test','accessibility.tabfocus':7}});
    const browser=root.browser();if(!browser)throw new Error('Firefox browser unavailable');
    try{await use(browser);}finally{await root.close();}
  },{scope:'worker'}]
}) : base;
export {expect};
