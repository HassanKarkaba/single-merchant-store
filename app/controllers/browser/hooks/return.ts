import { useState, useEffect } from 'react';
import { Dict } from '../contracts';
import { errorMessage } from '../errors';
import { titleCase } from '../../../views/presentation/format';
export function useReturnController(props: Dict) {
    const { id, client, navigate, session, notify, products } = props;
    const [record, setRecord] = useState<Dict | null>(null), [error, setError] = useState(''), [revision, setRevision] = useState(0), [selected, setSelected] = useState<string[]>([]), [dispositions, setDispositions] = useState<Dict>({}), [replacement, setReplacement] = useState(''), [dialog, setDialog] = useState<Dict | null>(null), [busy, setBusy] = useState(false), [operationId, setOperationId] = useState('');
    const roles: string[] = session?.actor?.roles || [], can = (...values: string[]) => roles.includes('merchant_admin') || values.some(role => roles.includes(role));
    useEffect(() => {
        let active = true;
        const controller = new AbortController();
        setRecord(null);
        client.query(`/returns/${encodeURIComponent(id)}`, {}, controller.signal).then((value: Dict) => {
            if (active) {
                setRecord(value);
                setError('');
                setSelected([]);
                setDispositions({});
            }
        }).catch((e: Error) => {
            if (active && e.name !== 'AbortError')
                setError(errorMessage(e));
        });
        return () => { active = false; controller.abort(); };
    }, [id, client, revision]);
    async function run(operation: string, input: Dict, endpoint = 'returns') {
        setBusy(true);
        setError('');
        try {
            let value = await client.mutate(`/merchant/${endpoint}/commands`, operation, { return_id: id, ...input }, record?.version);
            setOperationId(value.operation_id || '');
            const deadline = Date.now() + 30000;
            while (value.operation_id && ['ADMITTED', 'BUSY', 'RUNNING', 'PENDING', 'UNKNOWN'].includes(value.state) && Date.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, 250));
                value = await client.query(`/operations/${encodeURIComponent(value.operation_id)}`);
            }
            const current = await client.query(`/returns/${encodeURIComponent(id)}`);
            setRecord(current);
            setSelected([]);
            setDispositions({});
            if (value.operation_id && !['COMPLETE', 'SUCCEEDED'].includes(value.state))
                throw new Error(`The recorded return operation is ${titleCase(value.state)}. Open its status before taking another action.`);
            notify(`Return owner recorded ${titleCase(value.state || 'the request')}.`);
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
    const units = record?.unit_records || (record?.unit_ids || record?.units || []).map((value: any) => typeof value === 'string' ? { unit_id: value } : value);
    const refreshReturn = () => (() => setRevision((value: any) => value + 1));
    const openDecision = () => (() => setDialog({ title: 'Approve or reject this return', operation: 'approve_return', fields: [{ key: 'decision', label: 'Return decision', options: ['approved', 'rejected'] }, { key: 'reason', label: 'Decision reason' }] }));
    const recordReceived = () => (() => run('record_received', { unit_ids: selected }).catch(() => { }));
    const changeDisposition = (unit: any) => ((event: any) => setDispositions({ ...dispositions, [unit.unit_id]: event.target.value }));
    const recordInspection = () => (() => run('record_inspection', { dispositions: Object.entries(dispositions).filter(([, value]: any) => value).map(([unit_id, disposition]: any) => ({ unit_id, accepted: disposition !== 'rejected', disposition })) }).catch(() => { }));
    const openRefund = () => (() => setDialog({ title: 'Request the approved original-unit simulated refund?', operation: 'request_refund', endpoint: 'refunds', fields: [] }));
    const selectReplacement = () => ((event: any) => setReplacement(event.target.value));
    const createExchange = () => (() => run('create_exchange_link', { replacement_variant_id: replacement }).catch(() => { }));
    const openFinalization = () => (() => setDialog({ title: 'Finalize this fully accounted return?', operation: 'close_return', fields: [] }));
    const closeDialog = () => (() => setDialog(null));
    const submitCommand = () => ((input: any) => { if (!dialog)
        throw new Error('This dialog is no longer active.'); return run(dialog.operation, input, dialog.endpoint || 'returns').then(() => { }); });
    return { id, navigate, session, products, record, error, selected, setSelected, dispositions, replacement, dialog, busy, operationId, can, units, refreshReturn, openDecision, recordReceived, changeDisposition, recordInspection, openRefund, selectReplacement, createExchange, openFinalization, closeDialog, submitCommand };
}
