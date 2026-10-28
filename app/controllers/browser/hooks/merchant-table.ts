import { Dict } from '../contracts';
export function useMerchantTableController(props: Dict) {
    const { rows, section, navigate, command, openDialog, can = () => false, history, product } = props;
    const openPriceDialog = (variant: any) => (() => openDialog?.({ title: `Set price · ${variant.sku || 'variant'}`, operation: 'set_price', endpoint: 'prices', input: { variant_id: variant.variant_id || variant.id }, version: variant.price_version ?? variant.price_revision ?? variant.version ?? 0, fields: [{ key: 'unit_minor', label: 'USD cents', type: 'number', min: 0, default: variant.unit_minor ?? variant.price_minor ?? 0 }] }));
    const openStockDialog = (row: any) => (() => openDialog?.({ title: 'Record a stock adjustment', operation: 'adjust_stock', endpoint: 'stock', input: { variant_id: row.variant_id || row.id }, version: row.version ?? 0, fields: [{ key: 'delta', label: 'Signed nonzero quantity adjustment', type: 'number' }, { key: 'reason', label: 'Adjustment reason' }] }));
    const openStockHistory = (row: any) => (() => history?.(row));
    const openPromotionDialog = (row: any) => (() => openDialog?.({ title: `${row.state === 'active' ? 'End' : 'Activate'} promotion?`, operation: row.state === 'active' ? 'end_promotion' : 'activate_promotion', endpoint: 'promotions', input: { promotion_id: row.id }, version: row.version, fields: [] }));
    return { rows, section, navigate, can, product, openPriceDialog, openStockDialog, openStockHistory, openPromotionDialog };
}
