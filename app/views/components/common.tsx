import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { Dict } from '../../controllers/browser/contracts';
import { money, shortId, titleCase } from '../presentation/format';
export function EmptyState({ title, message, action }: {
    title: string;
    message: string;
    action?: ReactNode;
}) { return <div className="empty-state"><h2>{title}</h2><p>{message}</p>{action}</div>; }
export function ErrorSummary({ message, retry }: {
    message: string;
    retry?: () => void;
}) { const ref = useRef<HTMLDivElement | null>(null); useEffect(() => { ref.current?.focus(); }, [message]); return <div className="error-summary" role="alert" tabIndex={-1} ref={ref}>{message}{retry && <button onClick={retry} className="pf-style-22">Try again</button>}</div>; }
export function Icon({ name, size = 18 }: {
    name: string;
    size?: number;
}) { const paths: Record<string, ReactNode> = { arrow: <><path d="M3 12h18m-6-6 6 6-6 6"/></>, search: <><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/></>, bag: <><path d="M5 7h14l1 14H4L5 7Z"/><path d="M8 8V6a4 4 0 0 1 8 0v2"/></>, user: <><circle cx="12" cy="8" r="3.5"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></>, menu: <><path d="M3 7h18M3 12h18M3 17h18"/></>, plus: <><path d="M12 4v16M4 12h16"/></>, minus: <path d="M4 12h16"/>, close: <><path d="m5 5 14 14M5 19 19 5"/></>, check: <path d="m4 12 5 5L20 6"/>, leaf: <><path d="M20 3c1 10-2 17-10 17C1 20 1 9 8 6c4-2 8-1 12-3Z"/><path d="m5 20 11-11"/></>, box: <><path d="m12 2 9 5v10l-9 5-9-5V7l9-5Z"/><path d="m3 7 9 5 9-5M12 12v10M8 4l9 5"/></>, heart: <path d="M20.5 4.5a5 5 0 0 0-7 0L12 6l-1.5-1.5a5 5 0 0 0-7 7L12 20l8.5-8.5a5 5 0 0 0 0-7Z"/>, lock: <><rect x="5" y="10" width="14" height="11" rx="1"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/></>, grid: <><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></>, chart: <><path d="M4 20V4m0 16h17M8 16v-5m5 5V7m5 9v-8"/></>, list: <><path d="M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1"/></>, refresh: <><path d="M20 8a8 8 0 0 0-14-3L3 8m0-6v6h6M4 16a8 8 0 0 0 14 3l3-3m0 6v-6h-6"/></>, settings: <><path d="m9 3-1 3-3 1-2 4 2 3v4l4 2 3-1 3 1 4-2v-4l2-3-2-4-3-1-1-3H9Z"/><circle cx="12" cy="12" r="3"/></>, clock: <><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></> }; return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.list}</svg>; }
export function Loading({ label = 'Loading' }: {
    label?: string;
}) { return <div className="loader" role="status"><span>{label}…</span></div>; }
export function NavLink({ href, navigate, children, className, style, ...rest }: Dict) {
    return <a href={href} className={className} {...rest} onClick={event => {
            if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0 && href.startsWith('/')) {
                event.preventDefault();
                navigate(href);
            }
        }}>{children}</a>;
}
export function PageHead({ title, subtitle }: {
    title: string;
    subtitle?: string;
}) { return <header className="page-head"><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</header>; }
export function Quantity({ value, onChange, disabled = false }: {
    value: number;
    onChange: (value: number) => void;
    disabled?: boolean;
}) { return <div className="quantity" role="group" aria-label="Quantity"><button aria-label="Decrease quantity" onClick={() => onChange(value - 1)} disabled={disabled || value <= 1} type="button"><Icon name="minus" size={10}/></button><span aria-live="polite">{value}</span><button aria-label="Increase quantity" onClick={() => onChange(value + 1)} disabled={disabled || value >= 99} type="button"><Icon name="plus" size={10}/></button></div>; }
export function SafeRecord({ record }: {
    record: Dict;
}) { const entries = Object.entries(record).filter(([key, value]) => !/(secret|token|password|signature|raw|credential)/i.test(key) && ['string', 'number', 'boolean'].includes(typeof value)); return <div className="pf-style-23">{entries.map(([key, value]) => <div className="summary-row" key={key}><span className="pf-style-32">{titleCase(key)}</span><span className="pf-style-33">{String(value)}</span></div>)}</div>; }
export function StatusBadge({ state }: {
    state?: string;
}) { const value = state || 'PENDING'; return <span className={`badge ${/PENDING|UNKNOWN|REVIEW|REQUEST|ACTION/.test(value) ? 'pending' : /FAIL|DECLIN|CANCEL|REJECT|EXPIRE/.test(value) ? 'denied' : ''}`}>{titleCase(value)}</span>; }
export function UnitSelector({ records, selected, onChange, disabled = false }: {
    records: Dict[];
    selected: string[];
    onChange: (value: string[]) => void;
    disabled?: boolean;
}) { return <fieldset className="unit-selector"><legend className="fine">Explicit original-unit selection</legend>{records.length ? records.map((unit: Dict) => <label className="unit-row" key={unit.unit_id}><input type="checkbox" checked={selected.includes(unit.unit_id)} disabled={disabled} onChange={event => onChange(event.target.checked ? [...selected, unit.unit_id] : selected.filter(value => value !== unit.unit_id))}/><span>Unit {shortId(unit.unit_id)}<span className="fine"> · {unit.sku || shortId(unit.variant_id)} · {unit.canceled ? 'Canceled' : unit.returned ? 'Returned' : unit.delivered_at ? 'Delivered' : unit.shipped ? 'Shipped' : 'Unshipped'}</span></span><span className="fine">{Number.isSafeInteger(unit.unit_net_minor) && Number.isSafeInteger(unit.unit_tax_minor) ? money(unit.unit_net_minor + unit.unit_tax_minor) : 'Amount in owner plan'}</span></label>) : <p className="fine">No original unit records are currently available.</p>}</fieldset>; }
