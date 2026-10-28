import { useState, useEffect } from 'react';
import type { FormEvent } from 'react';
import {isAdministrator} from '../account-navigation';
import { MERCHANT_ROLES } from '../constants';
import { Dict } from '../contracts';
import { ApiError, errorMessage } from '../errors';
export function useAccountController(props: Dict) {
    const { session, client, navigate, notify, refreshSession } = props;
    const [busy, setBusy] = useState(false), [error, setError] = useState(''), [username, setUsername] = useState(''), [password, setPassword] = useState(''), [code, setCode] = useState(''), [verificationRequired, setVerificationRequired] = useState(false);
    const actor = session?.actor, staff = actor?.roles?.some((role: string) => MERCHANT_ROLES.includes(role)), authReady = Boolean(session?.csrf_token), signedIn = Boolean(actor && actor.kind !== 'guest'), guest = actor?.kind === 'guest', administrator=isAdministrator(session);
    useEffect(() => {setPassword(''); setCode(''); setError(''); setVerificationRequired(false);}, [session?.epoch, session?.session_generation]);
    useEffect(() => {setUsername('');}, [actor?.id]);
    useEffect(() => {if (!client.session) void refreshSession().catch((cause:unknown) => setError(errorMessage(cause)));}, [client,refreshSession]);
    async function retrySecurity() {setBusy(true);setError('');try {await refreshSession();} catch(cause) {setError(errorMessage(cause));} finally {setBusy(false);}}
    async function act(action: 'login' | 'mfa' | 'guest' | 'logout', event?: FormEvent) {
        event?.preventDefault();
        setBusy(true);
        setError('');
        setVerificationRequired(false);
        try {
            const input = action === 'login' ? { username, password } : action === 'mfa' ? { challenge_id: session.challenge.id, code } : {};
            const result = await client.authenticate(action, input);
            setPassword('');
            setCode('');
            if (action === 'logout') {
                await refreshSession();
                notify('You have signed out. Private browser information has been cleared.');
            }
            else if (result.actor) {
                notify(action === 'guest' ? 'Guest shopping is active.' : 'Your current session is ready.');
                if (result.actor.roles?.some((role: string) => MERCHANT_ROLES.includes(role)))
                    navigate('/merchant');
            }
        }
        catch (e) {
            setPassword('');
            setCode('');
            setVerificationRequired(e instanceof ApiError && e.code === 'email_verification_required');
            setError(errorMessage(e));
        }
        finally {
            setBusy(false);
        }
    }
    const signOut = () => (() => act('logout'));
    const submitCode = () => ((event: any) => act('mfa', event));
    const changeCode = () => ((event: any) => setCode(event.target.value));
    const submitLogin = () => ((event: any) => act('login', event));
    const changeUsername = () => ((event: any) => setUsername(event.target.value));
    const changePassword = () => ((event: any) => setPassword(event.target.value));
    const startGuest = () => (() => act('guest'));
    return { session, navigate, busy, error, username, password, code, actor, staff, administrator, authReady, signedIn, guest, verificationRequired, retrySecurity, signOut, submitCode, changeCode, submitLogin, changeUsername, changePassword, startGuest };
}
