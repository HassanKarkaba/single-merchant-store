import { createRoot, hydrateRoot } from 'react-dom/client';
import type { Root } from 'react-dom/client';
import { InitialData } from './contracts';
import {captureAccountLink} from './account-links';
import { App } from '../../views/App';
export function bootstrapBrowser(initialData?: InitialData): {
    unmount: () => void;
    root: Root;
} {
    captureAccountLink();
    const embedded = document.getElementById('sme-initial-data')?.textContent;
    const data = initialData || (embedded ? JSON.parse(embedded) : null) || (globalThis as any).__SME_DATA__ || (globalThis as any).__SME_INITIAL__ || {}, element = document.getElementById('root');
    if (!element)
        throw new Error('Missing application root.');
    const app = <App path={window.location.pathname + window.location.search} initialData={data}/>;
    const root = element.hasChildNodes() ? hydrateRoot(element, app) : createRoot(element);
    if (!element.hasChildNodes())
        root.render(app);
    return { root, unmount: () => { root.unmount(); } };
}
