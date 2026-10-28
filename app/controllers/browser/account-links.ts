export type AccountLinkKind = 'verify-email' | 'reset-password';
let captured: {kind: AccountLinkKind; token: string} | null = null;
export function parseAccountLink(pathname: string, fragment: string): {kind: AccountLinkKind; token: string} | null {
    const kind = pathname === '/verify-email' ? 'verify-email' : pathname === '/reset-password' ? 'reset-password' : null;
    if (!kind || !fragment.startsWith('#')) return null;
    const parameters = new URLSearchParams(fragment.slice(1));
    const values = parameters.getAll('token');
    if ([...parameters.keys()].some(key => key !== 'token') || values.length !== 1 ||
        !/^[A-Za-z0-9_-]{32,512}$/.test(values[0])) return null;
    return {kind, token: values[0]};
}
export function captureAccountLink(): void {
    if (typeof window === 'undefined' || !['/verify-email', '/reset-password'].includes(window.location.pathname)) return;
    captured = parseAccountLink(window.location.pathname, window.location.hash);
    if (window.location.hash) window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
}
export function consumeAccountLink(kind: AccountLinkKind): string {
    if (!captured && typeof window !== 'undefined' && window.location.hash) captureAccountLink();
    if (captured?.kind !== kind) return '';
    const token = captured.token;
    captured = null;
    return token;
}
