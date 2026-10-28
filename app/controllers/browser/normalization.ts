import { Cart, Dict, Product } from './contracts';
import { ApiError } from './errors';
export function normalizeCart(value: Dict): Cart { return { id: value.id || '', version: value.version || 0, lines: Array.isArray(value.lines) ? value.lines : [], subtotal_minor: value.subtotal_minor || 0, coupon_code: value.coupon_code ?? value.coupon }; }
export function normalizeProducts(value: any): Product[] {
    const items = Array.isArray(value) ? value : value?.products || value?.items || [];
    if (!Array.isArray(items))
        throw new ApiError('Invalid catalog response.', 502, 'service_contract_error');
    for (const item of items) {
        if (!item || typeof item.id !== 'string' || typeof item.slug !== 'string' || typeof item.name !== 'string' || !Array.isArray(item.variants) || item.variants.some((variant: Dict) => typeof variant.id !== 'string' || typeof variant.sku !== 'string' || !Number.isSafeInteger(variant.unit_minor) || variant.unit_minor < 0 || !Number.isSafeInteger(variant.available) || variant.available < 0))
            throw new ApiError('Catalog price or availability is incomplete. Please retry discovery.', 502, 'service_contract_error');
    }
    return items;
}
