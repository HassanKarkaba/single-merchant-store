import { Cart } from './contracts';
export const EMPTY_CART: Cart = { id: '', version: 0, lines: [], subtotal_minor: 0 };
export const API_ROOT = '/api/v1';
export const MERCHANT_ROLES = ['catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'];
