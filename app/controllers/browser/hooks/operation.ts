import { useState, useEffect } from 'react';
import { Dict } from '../contracts';
import { errorMessage } from '../errors';
export function useOperationController(props: Dict) {
    const { id, client, navigate } = props;
    const [record, setRecord] = useState<Dict | null>(null), [error, setError] = useState(''), [revision, setRevision] = useState(0);
    useEffect(() => {
        let active = true;
        const controller = new AbortController();
        client.get_operation_status(id, controller.signal).then((value: Dict) => {
            if (active)
                setRecord(value);
        }).catch((e: Error) => {
            if (active && e.name !== 'AbortError')
                setError(errorMessage(e));
        });
        return () => { active = false; controller.abort(); };
    }, [id, client, revision]);
    const retryStatus = () => (() => setRevision((value: any) => value + 1));
    const refreshStatus = () => (() => setRevision((value: any) => value + 1));
    return { navigate, record, error, retryStatus, refreshStatus };
}
