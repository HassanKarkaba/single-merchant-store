import {useEffect, useRef, useState} from 'react';
import type {ChangeEvent, FormEvent} from 'react';
import {AccountAction, AccountActionResult} from '../account-actions';
import {consumeAccountLink} from '../account-links';
import {AccountField, AccountFieldErrors, AccountFields, validateAccountFields, accountFieldErrorsFromApi} from '../account-validation';
import {BrowserApiClient} from '../api-client';
import {ApiError, errorMessage} from '../errors';
import {createCommandId} from '../ids';
import {Session} from '../contracts';
import {ResetIntentIdentity} from '../reset-intent';
export type AccountAccessMode = 'register' | 'verify-email' | 'forgot-password' | 'reset-password';
export function useAccountAccessController({mode, client, session}: {mode: AccountAccessMode; client: BrowserApiClient; session: Session | null}) {
    const [values, setValues] = useState<AccountFields>({full_name:'', email:'', password:'', password_confirmation:''});
    const [errors, setErrors] = useState<AccountFieldErrors>({}), [error, setError] = useState(''), [busy, setBusy] = useState(false);
    const [receipt, setReceipt] = useState<AccountActionResult | null>(null), [uncertainReset, setUncertainReset] = useState(false), [linkState, setLinkState] = useState<'loading'|'ready'|'missing'|'invalid'|'expired'|'used'>('loading');
    const inFlight = useRef(false), resetIdentity = useRef(new ResetIntentIdentity());
    const token = useRef(''), command = useRef<{action: AccountAction; id: string} | null>(null), mounted = useRef(true), lastAction = useRef<AccountAction | null>(null), linkRevision = useRef(0), pollDeadline = useRef<{operationId:string; at:number} | null>(null);
    useEffect(() => {
        mounted.current = true;
        let readLink: (() => void) | undefined, onHash: (() => void) | undefined;
        if (mode === 'verify-email' || mode === 'reset-password') {
            readLink = () => {
                const fragmentPresent = Boolean(window.location.hash);
                token.current = consumeAccountLink(mode);
                linkRevision.current += 1;
                command.current = null; lastAction.current = null; resetIdentity.current.clear(); setUncertainReset(false);
                setReceipt(null); setErrors({}); setError('');
                setValues(previous => ({...previous, password:'', password_confirmation:''}));
                setLinkState(token.current ? 'ready' : fragmentPresent ? 'invalid' : 'missing');
            };
            readLink();
            onHash = () => {if (new URLSearchParams(window.location.hash.slice(1)).has('token')) readLink?.();};
            window.addEventListener('hashchange', onHash);
        } else setLinkState('ready');
        return () => {mounted.current = false; token.current = ''; resetIdentity.current.clear(); if (onHash) window.removeEventListener('hashchange', onHash);};
    }, [mode]);
    useEffect(() => {setValues(previous => ({...previous, password:'', password_confirmation:''}));}, [session?.actor?.id]);
    const ready = Boolean(session?.csrf_token), pendingVerification = receipt?.state === 'verification_pending';
    const resetPending = receipt?.state === 'reset_pending';
    const deliveryPending = receipt?.delivery_status === 'queued';
    const deliveryCanceled = receipt?.delivery_status === 'CANCELED';
    const deliveryFailed = receipt?.delivery_status === 'DELIVERY_FAILED';
    useEffect(() => {
        if (!receipt?.operation_id || !(['queued','REQUEST_ACCEPTED'].includes(receipt.delivery_status || '') || receipt.delivery_status === 'DELIVERY_FAILED' && receipt.retryable)) return;
        let active = true;
        const action: AccountAction = lastAction.current || 'register';
        const operationId = receipt.operation_id;
        let timer: ReturnType<typeof setTimeout>;
        if (pollDeadline.current?.operationId !== operationId) pollDeadline.current = {operationId, at:Date.now() + 60000};
        const deadline = pollDeadline.current.at;
        if (Date.now() >= deadline) return;
        const poll = async () => {
            try {
                const current = await client.accountActionStatus(action, operationId);
                if (!active) return;
                setReceipt(current);
                if ((['queued','REQUEST_ACCEPTED'].includes(current.delivery_status || '') || current.delivery_status === 'DELIVERY_FAILED' && current.retryable) && Date.now() < deadline) timer = setTimeout(poll, 2000);
            } catch (cause) {if (active) setError(errorMessage(cause));}
        };
        timer = setTimeout(poll, 1000);
        return () => {active = false; clearTimeout(timer);};
    }, [receipt?.operation_id, receipt?.delivery_status, receipt?.retryable, client]);
    function change(field: AccountField) {
        return (event: ChangeEvent<HTMLInputElement>) => {
            const value = event.target.value;
            setValues(previous => ({...previous, [field]: value}));
            setErrors(previous => ({...previous, [field]: undefined}));
            setError(''); command.current = null;
        };
    }
    async function run(action: AccountAction, event?: FormEvent) {
        event?.preventDefault();
        if (inFlight.current || busy || !ready) return;
        const validation = action === 'verify-email' ? {} : validateAccountFields(values, action);
        setErrors(validation); setError('');
        if (Object.keys(validation).length) {
            setError('Please review the highlighted fields.');
            return;
        }
        if ((action === 'verify-email' || action === 'reset-password') && !token.current) {setLinkState('missing'); return;}
        const revision = linkRevision.current, sessionRevision = client.store.identityRevision, requestToken = token.current;
        inFlight.current = true; setBusy(true);
        try {
            let key:string;
            if (action==='reset-password') {
                const identity=await resetIdentity.current.identityFor(requestToken,values.password);
                key=identity.id; setUncertainReset(identity.reused);
            } else {
                if (command.current?.action !== action) command.current={action,id:createCommandId()};
                key=command.current.id;
            }
            if (!mounted.current || revision!==linkRevision.current) {resetIdentity.current.clear(); return;}
            if (sessionRevision!==client.store.identityRevision) throw new ApiError('Your session changed. Review this form before submitting again.',409,'stale_session');
            const input = action === 'register' ? {...values, full_name:values.full_name.trim(), email:values.email.trim()} :
                action === 'verify-email' ? {token:requestToken} : action === 'reset-password' ? {token:requestToken, password:values.password, password_confirmation:values.password_confirmation} : {email:values.email.trim()};
            const result = await client.accountAction(action, input as never, key);
            if (!mounted.current || revision !== linkRevision.current) return;
            lastAction.current = action;
            setReceipt(result);
            if (result.state === 'reset_pending') {resetIdentity.current.markUncertain(); setUncertainReset(true); return;}
            command.current = null; resetIdentity.current.clear(); setUncertainReset(false);
            setValues(previous => ({...previous, password:'', password_confirmation:''}));
            if (action === 'verify-email' || action === 'reset-password') token.current = '';
        } catch (cause) {
            if (!mounted.current || revision !== linkRevision.current) return;
            const failure = cause instanceof ApiError ? cause : null;
            if (action==='reset-password' && failure && (['request_unknown','service_contract_error','stale_session'].includes(failure.code) || failure.status>=500)) {resetIdentity.current.markUncertain(); setUncertainReset(true);}
            const serverFields=accountFieldErrorsFromApi(failure?.details,[values.password,values.password_confirmation,token.current]);
            const field: Partial<Record<string, AccountField>> = {invalid_full_name:'full_name', invalid_email:'email', password_policy:'password', password_confirmation_mismatch:'password_confirmation'};
            if (Object.keys(serverFields).length) setErrors(serverFields);
            else if (failure && field[failure.code]) setErrors({[field[failure.code]!]:errorMessage(cause)});
            if (failure && ['verification_token_invalid','password_reset_token_invalid','verification_token_expired','password_reset_token_expired','verification_token_used','password_reset_token_used'].includes(failure.code)) {
                const reason = failure.details?.reason;
                setLinkState(reason === 'expired' || failure.code.endsWith('_expired') ? 'expired' : reason === 'used' || failure.code.endsWith('_used') ? 'used' : 'invalid');
                token.current = ''; resetIdentity.current.clear(); setUncertainReset(false);
            }
            setError(errorMessage(cause));
        } finally {inFlight.current=false; if (mounted.current) setBusy(false);}
    }
    const submit = (event: FormEvent) => run(mode, event);
    const resend = (event: FormEvent) => run('resend-verification', event);
    return {values, errors, error, busy, ready, receipt, linkState, pendingVerification, resetPending, uncertainReset, deliveryPending, deliveryFailed, deliveryCanceled, change, submit, resend};
}

export type ReturnTypeOfAccountAccess = ReturnType<typeof useAccountAccessController>;
