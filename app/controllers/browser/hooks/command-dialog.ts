import { useState, useEffect, useRef } from 'react';
import { Dict } from '../contracts';
import { errorMessage } from '../errors';
export function useCommandDialogController(props: Dict & {
    fields: Dict[];
}) {
    const { title, fields, onClose, onSubmit } = props;
    const [values, setValues] = useState<Dict>(() => Object.fromEntries(fields.map(field => [field.key, field.default ?? '']))), [busy, setBusy] = useState(false), [error, setError] = useState('');
    const ref = useRef<HTMLDialogElement | null>(null), trigger = useRef<HTMLElement | null>(null);
    useEffect(() => {
        const dialog = ref.current;
        trigger.current = document.activeElement as HTMLElement;
        dialog?.showModal();
        return () => {
            if (dialog?.open)
                dialog.close();
            trigger.current?.focus();
        };
    }, []);
    const cancelDialog = () => ((event: any) => {
        event.preventDefault();
        if (!busy)
            onClose();
    });
    const submitDialog = () => (async (event: any) => {
        event.preventDefault();
        setBusy(true);
        try {
            const input = { ...values };
            fields.forEach((field: any) => {
                if (field.type === 'variant_list')
                    return;
                if (input[field.key] === '' && field.required === false) {
                    delete input[field.key];
                    return;
                }
                if (field.type === 'number' && input[field.key] !== undefined)
                    input[field.key] = Number(input[field.key]);
                if (field.type === 'json' && input[field.key])
                    input[field.key] = JSON.parse(input[field.key]);
            });
            await onSubmit(input);
            onClose();
        }
        catch (e) {
            setError(errorMessage(e));
            setBusy(false);
        }
    });
    const changeMultipleField = (field: any) => ((event: any) => setValues({ ...values, [field.key]: Array.from(event.target.selectedOptions, (option: any) => option.value) }));
    const changeSelectField = (field: any) => ((e: any) => setValues({ ...values, [field.key]: e.target.value }));
    const changeTextAreaField = (field: any) => ((e: any) => setValues({ ...values, [field.key]: e.target.value }));
    const changeInputField = (field: any) => ((e: any) => setValues({ ...values, [field.key]: e.target.value }));
    return { title, fields, onClose, onSubmit, values, busy, error, ref, cancelDialog, submitDialog, changeMultipleField, changeSelectField, changeTextAreaField, changeInputField };
}
