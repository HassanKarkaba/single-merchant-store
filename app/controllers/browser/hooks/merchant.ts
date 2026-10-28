import { useState, useEffect } from 'react';
import { MERCHANT_ROLES } from '../constants';
import { Dict, Product } from '../contracts';
import { errorMessage } from '../errors';
import { MERCHANT_NAV } from '../../../views/presentation/constants';
import { titleCase } from '../../../views/presentation/format';
export function useMerchantController(props: Dict) {
    const { path, client, navigate, session, notify, products = [] } = props;
    const section = path.split('/')[2] || 'overview', resource = section === 'pricing' ? 'prices' : section, title = MERCHANT_NAV.find(([id]) => id === section)?.[1] || 'Merchant workspace';
    const [data, setData] = useState<Dict | null>(null), [error, setError] = useState(''), [filter, setFilter] = useState(''), [dialog, setDialog] = useState<Dict | null>(null), [revision, setRevision] = useState(0), [movements, setMovements] = useState<Dict | null>(null);
    const roles: string[] = session?.actor?.roles || [], can = (...required: string[]) => roles.includes('merchant_admin') || required.some(role => roles.includes(role));
    useEffect(() => {
        let active = true;
        const controller = new AbortController();
        setData(null);
        setError('');
        client.query(`/merchant/${resource}`, {}, controller.signal).then((result: Dict) => {
            if (active)
                setData(result);
        }).catch((e: Error) => {
            if (active && e.name !== 'AbortError')
                setError(errorMessage(e));
        });
        return () => { active = false; controller.abort(); };
    }, [client, resource, revision]);
    const rows: Dict[] = Array.isArray(data) ? data : data?.items || data?.products || data?.prices || data?.promotions || data?.orders || data?.stock || data?.returns || data?.payments || data?.reconciliation_items || [];
    const shown = rows.filter(row => JSON.stringify(row).toLowerCase().includes(filter.toLowerCase()));
    const command = async (operation: string, input: Dict, endpoint = section, version?: number) => { const result = await client.mutate(`/merchant/${endpoint}/commands`, operation, input, version); setRevision(value => value + 1); notify(`Command recorded · ${titleCase(result.state || 'complete')}`); return result; };
    const productFields = (row?: Dict) => [{ key: 'name', label: 'Product name', default: row?.name || '' }, { key: 'description', label: 'Plain-text description', type: 'textarea', default: typeof row?.description === 'string' ? row.description : '' }, { key: 'slug', label: 'Unique lowercase URL slug', default: row?.slug || '' }, { key: 'category', label: 'Category', default: row?.category || row?.attributes?.category || '' }];
    const assets = data?.assets || [];
    function openAction() {
        if (section === 'catalog')
            setDialog({ title: 'Create a draft product', operation: 'create_product', endpoint: 'catalog', fields: productFields() });
        else if (section === 'promotions')
            setDialog({ title: 'Create a promotion draft', operation: 'create_promotion', endpoint: 'promotions', fields: [{ key: 'kind', label: 'Discount rule', options: ['fixed', 'percentage', 'free_shipping'] }, { key: 'value', label: 'Value: cents or integer basis points', type: 'number', min: 0, default: 0 }, { key: 'eligible_variants', label: 'Eligible product variants (leave empty for all eligible variants)', type: 'variant_list', default: [], required: false, options: products.flatMap((product: Product) => product.variants.map(variant => ({ value: variant.id, label: product.name + ' · ' + variant.sku }))) }, { key: 'min_subtotal', label: 'Minimum subtotal, USD cents', type: 'number', min: 0, default: 0 }, { key: 'start_at', label: 'UTC start, milliseconds since epoch', type: 'number', default: Date.now() }, { key: 'end_at', label: 'UTC end, milliseconds since epoch', type: 'number', default: Date.now() + 86400000 }, { key: 'max_redemptions', label: 'Global redemption cap', type: 'number', min: 1, required: false }, { key: 'per_customer_limit', label: 'Per-customer cap', type: 'number', min: 1, required: false }, { key: 'coupon_code', label: 'Coupon code (optional)', required: false }] });
    }
    async function history(row: Dict) {
        setError('');
        try {
            const result = await client.query('/merchant/movements', { variant_id: row.variant_id || row.id });
            setMovements(result);
        }
        catch (e) {
            setError(errorMessage(e));
        }
    }
    function productAction(row: Dict, action: string) {
        if (action === 'edit')
            setDialog({ title: `Edit ${row.name}`, operation: 'update_product', endpoint: 'catalog', input: { product_id: row.id }, version: row.version, changes: true, fields: productFields(row) });
        else if (action === 'media')
            setDialog({ title: `Choose curated image · ${row.name}`, operation: 'select_media', endpoint: 'catalog', input: { product_id: row.id }, version: row.version, fields: [{ key: 'asset_id', label: 'Curated immutable asset', options: assets.map((asset: Dict) => ({ value: asset.id, label: asset.name || asset.id })) }, { key: 'alt_text', label: 'Descriptive alternative text' }, { key: 'position', label: 'Gallery position (0–9)', type: 'number', min: 0, max: 9, default: 0 }] });
        else if (action === 'variant')
            setDialog({ title: `Add variant · ${row.name}`, operation: 'upsert_variant', endpoint: 'catalog', input: { product_id: row.id }, version: row.version, variant: true, fields: [{ key: 'sku', label: 'Unique SKU' }, { key: 'options', label: 'Option names and values (JSON object)', type: 'json', default: '{}' }, { key: 'weight_g', label: 'Weight in grams', type: 'number', min: 1, default: 100 }, { key: 'length_mm', label: 'Length in millimeters', type: 'number', min: 1, default: 100 }, { key: 'width_mm', label: 'Width in millimeters', type: 'number', min: 1, default: 100 }, { key: 'height_mm', label: 'Height in millimeters', type: 'number', min: 1, default: 100 }] });
        else
            setDialog({ title: `${action === 'publish' ? 'Publish' : 'Unpublish'} ${row.name}?`, operation: action, endpoint: 'catalog', input: { product_id: row.id }, version: row.version, fields: [] });
    }
    const merchant = roles.some(role => MERCHANT_ROLES.includes(role));
    const refreshMerchant = () => (() => setRevision((value: any) => value + 1));
    const changeFilter = () => ((event: any) => setFilter(event.target.value));
    const editProduct = (row: any) => (() => productAction(row, 'edit'));
    const addVariant = (row: any) => (() => productAction(row, 'variant'));
    const chooseImage = (row: any) => (() => productAction(row, 'media'));
    const changePublication = (row: any) => (() => productAction(row, row.state === 'published' ? 'unpublish' : 'publish'));
    const loadNextPage = () => (async () => {
        if (!data?.next_cursor)
            return;
        try {
            const next = await client.query(`/merchant/${resource}`, { cursor: data.next_cursor });
            setData({ ...next, items: [...rows, ...(next.items || next.products || next.orders || next.stock || [])] });
        }
        catch (e) {
            setError(errorMessage(e));
        }
    });
    const closeMovements = () => (() => setMovements(null));
    const closeDialog = () => (() => setDialog(null));
    const submitCommand = () => ((input: any) => {
        if (!dialog)
            throw new Error('This dialog is no longer active.');
        let values = input;
        if (dialog.variant) {
            const { length_mm, width_mm, height_mm, ...rest } = input;
            values = { ...rest, dimensions_mm: [length_mm, width_mm, height_mm] };
        }
        if (dialog.operation === 'create_promotion')
            values = { ...values, max_redemptions: values.max_redemptions ?? null, per_customer_limit: values.per_customer_limit ?? null, coupon_code: values.coupon_code ?? null };
        return command(dialog.operation, { ...dialog.input, ...(dialog.changes ? { changes: values } : values) }, dialog.endpoint, dialog.version).then(() => { });
    });
    return { navigate, section, title, data, error, filter, dialog, setDialog, movements, can, rows, shown, command, openAction, history, merchant, refreshMerchant, changeFilter, editProduct, addVariant, chooseImage, changePublication, loadNextPage, closeMovements, closeDialog, submitCommand };
}
