import { useState, useEffect } from 'react';
import { Dict, Product } from '../contracts';
import { errorMessage } from '../errors';
import { normalizeProducts } from '../normalization';
export function useCatalogController(props: Dict) {
    const { products, categories, client, navigate, onAdd, path, loading, error: initialError } = props;
    const parameters = new URLSearchParams(path.split('?')[1] || ''), category = path.startsWith('/categories/') ? decodeURIComponent(path.split('/')[2].split('?')[0]) : parameters.get('category') || '', query = parameters.get('query') || '', sort = parameters.get('sort') || 'newest', min = parameters.get('min_price') || '', max = parameters.get('max_price') || '', inStock = parameters.get('in_stock') === 'true';
    const [items, setItems] = useState<Product[]>(products), [search, setSearch] = useState(query), [minInput, setMinInput] = useState(min ? `${Math.floor(Number(min) / 100)}.${String(Number(min) % 100).padStart(2, '0')}` : ''), [maxInput, setMaxInput] = useState(max ? `${Math.floor(Number(max) / 100)}.${String(Number(max) % 100).padStart(2, '0')}` : ''), [nextCursor, setNextCursor] = useState<string | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState(initialError || '');
    const filters = { query, category, sort, min_price: min, max_price: max, in_stock: inStock ? 'true' : '' };
    useEffect(() => {
        const controller = new AbortController();
        let active = true;
        setBusy(true);
        client.query('/catalog', filters, controller.signal).then((result: Dict) => {
            if (active) {
                setItems(normalizeProducts(result));
                setNextCursor(result.next_cursor || null);
                setError('');
                setBusy(false);
            }
        }).catch((e: Error) => {
            if (active && e.name !== 'AbortError') {
                setError(errorMessage(e));
                setBusy(false);
            }
        });
        return () => { active = false; controller.abort(); };
    }, [client, query, category, sort, min, max, inStock]);
    function update(values: Dict) {
        const next = new URLSearchParams();
        Object.entries({ ...filters, ...values }).forEach(([key, value]) => {
            if (value !== '' && value !== undefined && value !== null)
                next.set(key, String(value));
        });
        navigate('/search?' + next);
    }
    function price(value: string): string {
        if (value === '')
            return '';
        if (!/^\d{1,6}(?:\.\d{1,2})?$/.test(value))
            throw new Error('Enter a USD price with no more than two decimal places.');
        const [whole, fraction = ''] = value.split('.');
        return String(Number(whole) * 100 + Number(fraction.padEnd(2, '0')));
    }
    async function more() {
        if (!nextCursor)
            return;
        setBusy(true);
        try {
            const result = await client.query('/catalog', { ...filters, cursor: nextCursor });
            setItems(previous => [...previous, ...normalizeProducts(result).filter(item => !previous.some(old => old.id === item.id))]);
            setNextCursor(result.next_cursor || null);
        }
        catch (e) {
            setError(errorMessage(e));
        }
        finally {
            setBusy(false);
        }
    }
    const submitSearch = () => ((event: any) => { event.preventDefault(); update({ query: search }); });
    const changeSearch = () => ((event: any) => setSearch(event.target.value));
    const changeSort = () => ((event: any) => update({ sort: event.target.value }));
    const submitPrices = () => ((event: any) => {
        event.preventDefault();
        try {
            update({ min_price: price(minInput), max_price: price(maxInput) });
        }
        catch (e) {
            setError(errorMessage(e));
        }
    });
    const changeMinPrice = () => ((event: any) => setMinInput(event.target.value));
    const changeMaxPrice = () => ((event: any) => setMaxInput(event.target.value));
    const changeStockFilter = () => ((event: any) => update({ in_stock: event.target.checked ? 'true' : '' }));
    const retryCatalog = () => (() => update({}));
    return { categories, navigate, onAdd, loading, category, query, sort, inStock, items, search, minInput, maxInput, nextCursor, busy, error, more, submitSearch, changeSearch, changeSort, submitPrices, changeMinPrice, changeMaxPrice, changeStockFilter, retryCatalog };
}
