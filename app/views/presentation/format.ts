export function money(value: unknown): string {
    if (value === undefined || value === null)
        return '—';
    if (typeof value !== 'number' || !Number.isSafeInteger(value))
        return 'Unavailable';
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value / 100);
}
export function safeDate(value: unknown): string {
    if (!value)
        return 'Current sandbox record';
    const date = new Date(typeof value === 'number' ? value : String(value));
    return Number.isNaN(date.getTime()) ? 'Current record' : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}
export function shortId(value: unknown): string { const text = String(value || 'Pending'); return text.length > 13 ? `…${text.slice(-10)}` : text; }
export function titleCase(value: unknown): string { return String(value || '').replace(/[_-]/g, ' ').toLowerCase().replace(/\b\w/g, letter => letter.toUpperCase()); }
