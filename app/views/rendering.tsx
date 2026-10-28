import { renderToString } from 'react-dom/server';
import { InitialData } from '../controllers/browser/contracts';
import { App } from './App';
export function renderApp(path = '/', initialData: InitialData = {}): string { return renderToString(<App path={path} initialData={initialData}/>); }
export function renderPage(path = '/', initialData: InitialData = {}, options: {
    nonce?: string;
    assetPath?: string;
} = {}): string {
    const nonce = options.nonce || initialData.nonce;
    const data = { ...initialData, ...(nonce ? { nonce } : {}) };
    const escaped = JSON.stringify(data).replace(/</g, '\\u003c').replace(/>/g, '\\u003e').replace(/&/g, '\\u0026').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
    const safeNonce = nonce ? ` nonce="${nonce.replace(/[^a-zA-Z0-9+/_=-]/g, '')}"` : '';
    const asset = (options.assetPath || '/assets/browser.js').replace(/[^a-zA-Z0-9/_.-]/g, '');
    return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Thoughtfully considered objects for the everyday. A clearly labeled synthetic commerce sandbox."><meta name="color-scheme" content="light"><link rel="stylesheet" href="/assets/browser.css"><title>Common &amp; Goods — Everyday, considered.</title></head><body><div id="root">${renderApp(path, data)}</div><script id="sme-initial-data" type="application/json"${safeNonce}>${escaped}</script><script type="module"${safeNonce} src="${asset}"></script></body></html>`;
}
