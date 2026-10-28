import { useState, useEffect, useRef, useCallback } from 'react';
import { flushSync } from 'react-dom';
import { BrowserApiClient } from '../api-client';
import { EMPTY_CART } from '../constants';
import { Cart, Dict, InitialData, Product, Session, Variant } from '../contracts';
import { errorMessage } from '../errors';
import { createCommandId } from '../ids';
import { normalizeCart, normalizeProducts } from '../normalization';
export function useStorefrontController(initialPath: string, initialData: InitialData) {
    const [path, setPath] = useState(initialPath), [products, setProducts] = useState<Product[]>(normalizeProducts(initialData.catalog || [])), [categories, setCategories] = useState<string[]>(initialData.categories || (!Array.isArray(initialData.catalog) ? initialData.catalog?.categories : []) || []), [cart, setCart] = useState<Cart>(EMPTY_CART), [session, setSession] = useState<Session | null>(null), [notice, setNotice] = useState(''), [cartError, setCartError] = useState(''), [catalogError, setCatalogError] = useState(''), [catalogLoading, setCatalogLoading] = useState(!initialData.catalog);
    const [cartPending, setCartPending] = useState(false);
    const navigationMounted = useRef(false);
    const clientRef = useRef<BrowserApiClient | null>(null);
    if (!clientRef.current)
        clientRef.current = new BrowserApiClient();
    const client = clientRef.current;
    const notify = useCallback((text: string) => setNotice(text), []);
    const navigate = useCallback((target: string) => { window.history.pushState({}, '', target); setPath(target); window.scrollTo(0, 0); }, []);
    const refreshCart = useCallback(async () => {
        if (!client.session?.actor || !['guest','customer'].includes(client.session.actor.kind)) {
            setCart(EMPTY_CART);
            setCartError('');
            return EMPTY_CART;
        }
        try {
            const value = await client.query('/cart');
            setCart(normalizeCart(value));
            setCartError('');
            return value;
        }
        catch (e) {
            setCartError(errorMessage(e));
            return null;
        }
    }, [client]);
    const refreshSession = useCallback(() => client.query('/session'), [client]);
    useEffect(() => {
        const off = client.observe_session(value => {
            flushSync(() => { setSession(value); setCart(EMPTY_CART); setCartError(''); setNotice(''); });
            if (value?.actor)
                void refreshCart();
        });
        const controller = new AbortController();
        let active = true;
        refreshSession().catch(() => null).then(() => client.query('/catalog', {}, controller.signal)).then(value => {
            if (active) {
                setProducts(normalizeProducts(value));
                setCategories(value.categories || []);
                setCatalogLoading(false);
            }
        }).catch(e => {
            if (active && e.name !== 'AbortError') {
                setCatalogError(errorMessage(e));
                setCatalogLoading(false);
            }
        });
        const pop = () => setPath(window.location.pathname + window.location.search);
        window.addEventListener('popstate', pop);
        const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('sme-session') : null;
        if (channel)
            channel.onmessage = event => { void client.receive_session_notice(event.data).catch(() => { }); };
        let lastTouch = 0;
        const activity = () => {
            if (!client.session?.actor || Date.now() - lastTouch < 60000)
                return;
            lastTouch = Date.now();
            void client.authenticate('touch', { activity_id: createCommandId() }).catch(() => { });
        };
        window.addEventListener('pointerdown', activity);
        window.addEventListener('keydown', activity);
        return () => { active = false; controller.abort(); off(); channel?.close(); window.removeEventListener('popstate', pop); window.removeEventListener('pointerdown', activity); window.removeEventListener('keydown', activity); };
    }, [client, refreshCart, refreshSession]);
    useEffect(() => {
        if (!notice)
            return;
        const timer = setTimeout(() => setNotice(''), 6000);
        return () => clearTimeout(timer);
    }, [notice]);
    useEffect(() => {
        document.title = path.startsWith('/merchant') ? 'Merchant workspace · Common & Goods' : path.startsWith('/cart') ? 'Your bag · Common & Goods' : 'Common & Goods — Everyday, considered.';
        if (!navigationMounted.current) {
            navigationMounted.current = true;
            return;
        }
        const heading = document.querySelector<HTMLElement>('main h1');
        if (heading) {
            heading.tabIndex = -1;
            heading.focus({ preventScroll: true });
        }
    }, [path]);
    async function add(variant: Variant, quantity = 1) {
        setCartPending(true);
        setCartError('');
        try {
            if (!client.session)
                await refreshSession();
            if (!client.session?.actor)
                await client.authenticate('guest', {});
            const current = await client.query('/cart');
            const line = current.lines?.find((item: Dict) => item.variant_id === variant.id);
            await client.mutate('/cart/commands', 'set_line', { variant_id: variant.id, quantity: (line?.quantity || 0) + quantity }, current.version);
            await refreshCart();
            notify('Added to your demo bag.');
        }
        catch (e) {
            setCartError(errorMessage(e));
        }
        finally {
            setCartPending(false);
        }
    }
    return { path, products, categories, cart, session, notice, setNotice, cartError, catalogError, catalogLoading, cartPending, client, notify, navigate, refreshCart, refreshSession, add };
}
