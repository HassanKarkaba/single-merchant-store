import { useState, useEffect, useRef } from 'react';
import type { FormEvent } from 'react';
import { Dict } from '../contracts';
import { errorMessage } from '../errors';
export function useCheckoutController(props: Dict) {
    const { id, client, navigate, session, refreshCart, cart } = props;
    const [checkout, setCheckout] = useState<Dict | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [accepted, setAccepted] = useState(false), [dirty, setDirty] = useState(false), [offers, setOffers] = useState<Dict[]>([]), [offerId, setOfferId] = useState(''), [instrument, setInstrument] = useState('demo_success'), [now, setNow] = useState(Date.now());
    const [form, setForm] = useState<Dict>({ email: 'customer@store.invalid', recipient: 'Demo Customer', line1: '100 Example Street', line2: '', city: 'Demo City', region: 'MI', postal_code: '48104', country: 'US' });
    useEffect(() => {
        let active = true;
        const controller = new AbortController();
        setCheckout(null);
        client.query(`/checkout/${encodeURIComponent(id)}`, {}, controller.signal).then((data: Dict) => {
            if (active) {
                setCheckout(data);
                setForm(previous => ({ ...previous, ...(data.address || {}), email: data.email || 'customer@store.invalid' }));
                setOffers(data.delivery_offers || data.offers || []);
                setOfferId(data.selected_offer_id || data.delivery_offer_id || '');
                setDirty(false);
                setAccepted(false);
            }
        }).catch((e: Error) => {
            if (active && e.name !== 'AbortError')
                setError(errorMessage(e));
        });
        return () => { active = false; controller.abort(); };
    }, [id, client]);
    useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
    const lastCartVersion = useRef(cart.version);
    useEffect(() => {
        if (lastCartVersion.current && cart.version !== lastCartVersion.current) {
            setDirty(true);
            setAccepted(false);
        }
        lastCartVersion.current = cart.version;
    }, [cart.version]);
    const quote = checkout?.quote, expiry = quote?.expires_at ? new Date(quote.expires_at).getTime() : null, expired = expiry !== null && now >= expiry;
    function edit(key: string, value: string) { setForm(previous => ({ ...previous, [key]: value })); setDirty(true); setAccepted(false); setOffers([]); setOfferId(''); }
    function fixture(state: string) { const zips: Dict = { MI: '48104', CA: '94105', TX: '78701' }; setForm({ email: 'customer@store.invalid', recipient: 'Demo Customer', line1: '100 Example Street', line2: '', city: 'Demo City', region: state, postal_code: zips[state], country: 'US' }); setDirty(true); setAccepted(false); setOfferId(''); setOffers([]); }
    async function saveAddress(event: FormEvent) {
        event.preventDefault();
        setBusy(true);
        setError('');
        setAccepted(false);
        try {
            const { email, ...address } = form;
            if (!address.line2)
                delete address.line2;
            const saved = await client.mutate('/checkout/commands', 'address', { checkout_id: id, email, address }, checkout?.version);
            const result = await client.mutate('/checkout/commands', 'evaluate', { checkout_id: id }, saved.version);
            setCheckout(result);
            setOffers(result.delivery_offers || result.offers || []);
            setOfferId('');
            setDirty(true);
        }
        catch (e) {
            setError(errorMessage(e));
        }
        finally {
            setBusy(false);
        }
    }
    async function calculate() {
        if (!offerId)
            return;
        setBusy(true);
        setError('');
        setAccepted(false);
        try {
            const selected = await client.mutate('/checkout/commands', 'delivery', { checkout_id: id, offer_id: offerId }, checkout?.version);
            setCheckout(selected);
            const result = await client.mutate('/checkout/commands', 'evaluate', { checkout_id: id }, selected.version);
            setCheckout(result);
            setDirty(false);
        }
        catch (e) {
            setError(errorMessage(e));
            setDirty(true);
        }
        finally {
            setBusy(false);
        }
    }
    async function purchase() {
        if (!quote || !accepted || dirty || expired)
            return;
        setBusy(true);
        setError('');
        try {
            const acceptedCheckout = await client.mutate('/checkout/commands', 'accept_quote', { checkout_id: id, quote_id: quote.id, total_minor: quote.total_minor }, checkout?.version);
            setCheckout(acceptedCheckout);
            const result = await client.mutate('/purchases/commands', 'submit', { checkout_id: id, accepted_quote_id: quote.id, demo_instrument: instrument });
            await refreshCart();
            navigate(`/purchases/${result.id || result.attempt_id || result.operation_id}`);
        }
        catch (e) {
            setError(errorMessage(e));
        }
        finally {
            setBusy(false);
        }
    }
    const useDemoAddress = (state: any) => (() => fixture(state));
    const changeAddressField = (key: any) => ((event: any) => edit(key, event.target.value));
    const selectDeliveryOffer = (offer: any) => (() => { setOfferId(offer.id); setDirty(true); setAccepted(false); });
    const changeAcceptance = () => ((event: any) => setAccepted(event.target.checked));
    const changeInstrument = () => ((event: any) => setInstrument(event.target.value));
    return { id, navigate, session, checkout, error, busy, accepted, dirty, offers, offerId, instrument, form, quote, expiry, expired, saveAddress, calculate, purchase, useDemoAddress, changeAddressField, selectDeliveryOffer, changeAcceptance, changeInstrument };
}
