import { useState, useEffect } from 'react';
import { Dict } from '../contracts';
import { errorMessage } from '../errors';
export function usePurchaseController(props: Dict) {
    const { id, client, navigate } = props;
    const [purchase, setPurchase] = useState<Dict | null>(null), [error, setError] = useState(''), [revision, setRevision] = useState(0);
    useEffect(() => {
        let active = true;
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout> | undefined;
        async function poll() {
            try {
                const result = await client.query(`/purchases/${encodeURIComponent(id)}`, {}, controller.signal);
                if (!active)
                    return;
                setPurchase(result);
                setError('');
                if (!['PAID', 'FAILED', 'DECLINED', 'CANCELED', 'EXPIRED'].includes(result.state))
                    timer = setTimeout(poll, 3000);
            }
            catch (e) {
                if (active && (e as Error).name !== 'AbortError')
                    setError(errorMessage(e));
            }
        }
        poll();
        return () => {
            active = false;
            controller.abort();
            if (timer)
                clearTimeout(timer);
        };
    }, [id, client, revision]);
    const paid = purchase?.state === 'PAID', failed = ['FAILED', 'DECLINED', 'CANCELED', 'EXPIRED'].includes(purchase?.state), unknown = ['UNKNOWN', 'CAPTURE_PENDING', 'REVIEW', 'PROCESSING'].includes(purchase?.state);
    const orderId = paid ? purchase?.order_id : undefined;
    const retryPurchase = () => (() => setRevision((value: any) => value + 1));
    const refreshPurchase = () => (() => setRevision((value: any) => value + 1));
    return { orderId, id, navigate, purchase, error, paid, failed, unknown, retryPurchase, refreshPurchase };
}
