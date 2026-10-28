import type {ChangeEventHandler, HTMLInputAutoCompleteAttribute} from 'react';
export function AccountField({name, label, value, onChange, error, type='text', autoComplete, disabled, hint, maxLength}: {
    name:string; label:string; value:string; onChange:ChangeEventHandler<HTMLInputElement>; error?:string;
    type?:string; autoComplete?:HTMLInputAutoCompleteAttribute; disabled?:boolean; hint?:string; maxLength?:number;
}) {
    const id = `account-${name}`, descriptions = [error ? `${id}-error` : '', hint ? `${id}-hint` : ''].filter(Boolean).join(' ');
    return <div className="field"><label htmlFor={id}><span>{label}</span></label><input id={id} name={name} type={type} value={value} onChange={onChange} required disabled={disabled} autoComplete={autoComplete} maxLength={maxLength} aria-invalid={Boolean(error)} aria-describedby={descriptions || undefined}/>{hint && <small id={`${id}-hint`}>{hint}</small>}{error && <small className="field-error" id={`${id}-error`}>{error}</small>}</div>;
}
