import { useState, useEffect } from 'react';
import { Dict } from '../contracts';
import { ApiError, errorMessage } from '../errors';
import { titleCase } from '../../../views/presentation/format';
export function useOrderController(props: Dict) {
    const { id, client, navigate, notify, refreshCart, session } = props;
    const [data, setData] = useState<Dict | null>(null), [error, setError] = useState(''), [dialog, setDialog] = useState<Dict | null>(null), [revision, setRevision] = useState(0), [selected, setSelected] = useState<string[]>([]), [offers, setOffers] = useState<Dict[]>([]), [offerId, setOfferId] = useState(''), [busy, setBusy] = useState(false);
    const roles: string[] = session?.actor?.roles || [], can = (...values: string[]) => roles.includes('merchant_admin') || values.some(role => roles.includes(role));
    useEffect(() => {
        let active = true;
        const controller = new AbortController();
        setData(null);
        setSelected([]);
        setOffers([]);
        client.query(id ? `/orders/${encodeURIComponent(id)}` : '/orders', {}, controller.signal).then((value: Dict) => {
            if (active) {
                setData(value);
                setError('');
            }
        }).catch((e: Error) => {
            if (active && e.name !== 'AbortError')
                setError(errorMessage(e));
        });
        return () => { active = false; controller.abort(); };
    }, [id, client, revision]);
    async function run(endpoint: string, operation: string, input: Dict, version?: number) {
        setBusy(true);
        setError('');
        try {
            const value = await client.mutate(endpoint, operation, input, version);
            setRevision(current => current + 1);
            notify(`Owner recorded ${titleCase(value.state || 'the request')}.`);
            await refreshCart();
            return value;
        }
        catch (e) {
            setError(errorMessage(e));
            throw e;
        }
        finally {
            setBusy(false);
        }
    }
    async function downloadLabel(shipment: Dict) {
        setBusy(true);
        setError('');
        try {
            const label = await client.query(`/shipments/${encodeURIComponent(shipment.id)}/label`);
            if (label.id !== shipment.id || label.state !== 'available' || typeof label.content !== 'string' || !label.content.startsWith('SIMULATED LABEL'))
                throw new ApiError('The protected simulated label is unavailable.', 502, 'service_contract_error');
            const url = URL.createObjectURL(new Blob([label.content], { type: 'text/plain;charset=utf-8' }));
            const link = document.createElement('a');
            link.href = url;
            link.download = `simulated-label-${shipment.id}.txt`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
        catch (e) {
            setError(errorMessage(e));
        }
        finally {
            setBusy(false);
        }
    }
    function units(value: string[]) { setSelected(value); setOffers([]); setOfferId(''); }
    const records: Dict[] = data?.unit_records || [], orders = Array.isArray(data) ? data : data?.orders || data?.items || [], customer = session?.actor?.kind === 'customer' || session?.actor?.kind === 'guest';
    async function quoteParcel() {
        setBusy(true);
        setError('');
        try {
            const result = await client.mutate('/merchant/fulfillment/commands', 'quote_parcel', { order_id: id, unit_ids: selected }, data?.version);
            setOffers(result.offers || result.delivery_offers || []);
            setOfferId('');
        }
        catch (e) {
            setError(errorMessage(e));
        }
        finally {
            setBusy(false);
        }
    }
    const refreshOrder = () => (() => setRevision((value: any) => value + 1));
    const openCancellation = () => (() => setDialog({ title: 'Cancel selected unshipped units', endpoint: `/orders/${id}/commands`, operation: 'cancel_units', input: { unit_ids: selected }, version: data?.version, fields: [{ key: 'reason', label: 'Cancellation reason' }] }));
    const openReturnRequest = () => (() => setDialog({ title: 'Request a return for selected units', endpoint: `/orders/${id}/commands`, operation: 'request_return', input: { unit_ids: selected }, version: data?.version, returnRequest: true, fields: [{ key: 'reason', label: 'Return reason' }, { key: 'requested_resolution', label: 'Preferred resolution', options: ['refund', 'exchange'] }] }));
    const changeParcelOffer = () => ((event: any) => setOfferId(event.target.value));
    const planParcel = () => (() => run('/merchant/fulfillment/commands', 'plan_shipment', { order_id: id, unit_ids: selected, offer_id: offerId }, data?.version).catch(() => { }));
    const purchaseLabel = (shipment: any) => (() => run('/merchant/fulfillment/commands', 'purchase_label', { shipment_id: shipment.id }, shipment.version).catch(() => { }));
    const getLabel = (shipment: any) => (() => downloadLabel(shipment));
    const openHandoverReservation = (shipment: any) => (() => setDialog({ title: 'Reserve handover for this parcel?', endpoint: '/merchant/fulfillment/commands', operation: 'reserve_handover', input: { shipment_id: shipment.id }, version: shipment.version, fields: [] }));
    const openHandoverConfirmation = (shipment: any) => (() => setDialog({ title: 'Confirm simulated physical handover?', endpoint: '/merchant/fulfillment/commands', operation: 'confirm_handover', input: { shipment_id: shipment.id }, version: shipment.version, fields: [] }));
    const openTracking = (shipment: any) => (() => setDialog({ title: 'Record simulated tracking', endpoint: '/merchant/fulfillment/commands', operation: 'simulate_tracking', input: { shipment_id: shipment.id }, version: shipment.version, fields: [{ key: 'status', label: 'Tracking milestone', options: ['IN_TRANSIT', 'DELIVERED', 'EXCEPTION'] }] }));
    const closeDialog = () => (() => setDialog(null));
    const submitCommand = () => (async (input: any) => {
        if (!dialog)
            throw new Error('This dialog is no longer active.');
        const result = await run(dialog.endpoint, dialog.operation, { ...dialog.input, ...input }, dialog.version);
        if (dialog.returnRequest)
            navigate(`/returns/${result.return_id || result.id}`);
    });
    return { id, navigate, data, error, dialog, selected, offers, offerId, busy, can, units, records, orders, customer, quoteParcel, refreshOrder, openCancellation, openReturnRequest, changeParcelOffer, planParcel, purchaseLabel, getLabel, openHandoverReservation, openHandoverConfirmation, openTracking, closeDialog, submitCommand };
}
