import { useState } from 'react';
import { Dict } from '../contracts';
import { errorMessage } from '../errors';
export function useCartController(props: Dict) {
    const { cart, cartPending, client, refreshCart, navigate, notify } = props;
    const [busy, setBusy] = useState(''), [error, setError] = useState(''), [coupon, setCoupon] = useState(cart.coupon_code || '');
    async function change(line: Dict, quantity: number) {
        if (quantity < 1 || quantity > 99)
            return;
        setBusy(line.variant_id);
        setError('');
        try {
            await client.mutate('/cart/commands', 'set_line', { variant_id: line.variant_id, quantity }, cart.version);
            await refreshCart();
        }
        catch (e) {
            setError(errorMessage(e));
            await refreshCart();
        }
        finally {
            setBusy('');
        }
    }
    async function checkout() {
        setBusy('checkout');
        setError('');
        try {
            const result = await client.mutate('/checkout/commands', 'begin', { cart_revision: cart.version });
            navigate(`/checkout/${result.id || result.checkout_id}`);
        }
        catch (e) {
            setError(errorMessage(e));
            setBusy('');
        }
    }
    const changeQuantity = (line: any) => ((value: any) => change(line, value));
    const removeLine = (line: any) => (async () => {
        setBusy(line.variant_id);
        try {
            await client.mutate('/cart/commands', 'remove_line', { variant_id: line.variant_id }, cart.version);
            await refreshCart();
        }
        catch (e) {
            setError(errorMessage(e));
        }
        finally {
            setBusy('');
        }
    });
    const changeCoupon = () => ((e: any) => setCoupon(e.target.value));
    const applyCoupon = () => (async () => {
        try {
            await client.mutate('/cart/commands', 'set_coupon', { coupon_code: coupon }, cart.version);
            await refreshCart();
            notify('Coupon saved. Eligibility is checked with your quote.');
        }
        catch (e) {
            setError(errorMessage(e));
        }
    });
    return { cart, cartPending, navigate, busy, error, coupon, checkout, changeQuantity, removeLine, changeCoupon, applyCoupon };
}
