export type Dict = Record<string, any>;
export type Product = {
    id: string;
    slug: string;
    name: string;
    description: string;
    category?: string;
    variants: Variant[];
    images?: (string | Dict)[];
    media?: Dict[];
};
export type Variant = {
    id: string;
    sku: string;
    unit_minor: number;
    available: number;
    options?: Record<string, string>;
};
export type Cart = {
    id: string;
    version: number;
    lines: Dict[];
    subtotal_minor: number;
    coupon_code?: string;
};
export type Session = {
    profile: string;
    csrf_token: string;
    actor: null | {
        kind: string;
        roles: string[];
        id: string;
        full_name?: string;
        email?: string;
    };
    epoch: string | number;
    session_generation: string | number;
    idle_expires_at?: number;
    absolute_expires_at?: number;
    challenge: null | {
        id: string;
        expires_at: number;
        state: string;
    };
    sandbox?: boolean;
};
export type InitialData = {
    catalog?: Product[] | {
        products: Product[];
        categories?: string[];
    };
    sandbox?: boolean;
    categories?: string[];
    session?: Session;
    cart?: Cart;
    path?: string;
    nonce?: string;
    uiBundle?: Dict;
};
export type NativeUI = {
    createScope: (options: Dict) => Dict;
    destroy: () => Promise<Dict>;
};
