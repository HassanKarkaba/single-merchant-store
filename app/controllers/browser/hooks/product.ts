import { useState, useEffect } from 'react';
import { Dict, Product, Variant } from '../contracts';
import { errorMessage } from '../errors';
export function useProductController(props: Dict) {
    const { slug, variantId, products, client, navigate, onAdd } = props;
    const [product, setProduct] = useState<Product | null>(products.find((item: Product) => item.slug === slug) || null), [error, setError] = useState(''), [selected, setSelected] = useState(() => variantId ? (products.find((item: Product) => item.slug === slug)?.variants.findIndex((item: Variant) => item.id === variantId) ?? -1) : 0), [quantity, setQuantity] = useState(1), [busy, setBusy] = useState(false);
    useEffect(() => {
        let active = true;
        setProduct(current => current?.slug === slug ? current : null);
        setSelected(variantId ? -1 : 0);
        setQuantity(1);
        setError('');
        const controller = new AbortController();
        client.query(`/catalog/${encodeURIComponent(slug)}`, {}, controller.signal).then((data: Product) => {
            if (active) {
                if ((data as any).redirect) {
                    navigate('/products/' + (data as any).redirect);
                    return;
                }
                const position = variantId ? data.variants.findIndex(item => item.id === variantId) : 0;
                if (position < 0) {
                    setError('The selected variant is unavailable.');
                    return;
                }
                setSelected(position);
                setProduct(data);
            }
        }).catch((e: Error) => {
            if (active && e.name !== 'AbortError')
                setError(errorMessage(e));
        });
        return () => { active = false; controller.abort(); };
    }, [slug, variantId, client]);
    const variant = product?.variants[selected];
    const available = variant?.available ?? 0;
    const purchasable = Boolean(variant && available >= quantity && Number.isSafeInteger(variant.unit_minor));
    const stockMessage = available > 0 ? `${available} available · Confirmed again at checkout` : 'This object is currently unavailable';
    const selectVariant = (index: any) => (() => setSelected(index));
    const addToCart = (variant: any) => (async () => { setBusy(true); await onAdd(variant, quantity); setBusy(false); });
    return { available, purchasable, stockMessage, products, navigate, product, error, selected, quantity, setQuantity, busy, variant, selectVariant, addToCart };
}
