import {AccountAction, AccountActionInput, AccountActionResult, parseAccountActionResult, validateAccountActionInput} from './account-actions';
import { API_ROOT } from './constants';
import { Dict, Session } from './contracts';
import { ApiError } from './errors';
import { createCommandId } from './ids';
import { BrowserRequestStore } from './request-store';
import { validateMoney, validateSessionDTO } from './validation';
export class BrowserApiClient {
    csrf = '';
    readonly instanceId = createCommandId();
    store: BrowserRequestStore;
    session: Session | null = null;
    private controllers = new Set<AbortController>();
    private listeners = new Set<(session: Session | null) => void>();
    private sessionRequest: Promise<Session> | null = null;
    apply_session(value: Session): Session {
        validateSessionDTO(value);
        const key = `${value.actor?.id || 'preauth'}:${value.epoch}:${value.session_generation}`;
        if (key !== this.store.epoch) {
            this.on_session_changed(key);
            this.store.bind_scope(value.actor?.id || 'preauth');
        }
        this.csrf = value.csrf_token;
        this.session = value;
        this.listeners.forEach(listener => listener(value));
        return value;
    }
    async authenticate(action: 'login' | 'mfa' | 'guest' | 'logout' | 'touch', input: Dict): Promise<any> {
        if (!this.csrf)
            await this.query('/session');
        const allowed: Record<string, string[]> = { login: ['username', 'password'], mfa: ['challenge_id', 'code'], guest: [], logout: [], touch: ['activity_id'] };
        if (!allowed[action] || !Object.keys(input).every(key => allowed[action].includes(key)) || Object.keys(input).length !== allowed[action].length)
            throw new ApiError('Invalid authentication input.', 422, 'invalid_input');
        const identityRevision = this.store.identityRevision;
        try {
            const result = await this.request(`/auth/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this.csrf }, body: JSON.stringify(input) });
            if (identityRevision !== this.store.identityRevision)
                throw new ApiError('Your session changed. Refresh this information.', 409, 'stale_session');
            if (action === 'logout') {
                this.on_session_changed('logged-out');
                this.broadcast();
                return result;
            }
            const session = this.apply_session(result);
            if (action !== 'touch')
                this.broadcast();
            return session;
        }
        catch (error) {
            await this.refreshDeniedCsrf(error);
            if (error instanceof ApiError && ['request_unknown', 'service_contract_error'].includes(error.code)) {
                const observed = await this.query('/session');
                if (observed.actor || observed.challenge)
                    return observed;
                throw new ApiError('The authentication response was lost. Please explicitly sign in again using a fresh allowed code.', 0, 'authentication_unknown');
            }
            throw error;
        }
    }
    async accountAction<A extends AccountAction>(action: A, input: AccountActionInput[A], idempotencyKey: string): Promise<AccountActionResult> {
        validateAccountActionInput(action, input);
        if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(idempotencyKey))
            throw new ApiError('Invalid account action identity.', 422, 'invalid_input');
        try {
        if (!this.csrf) await this.query('/session');
        const result = parseAccountActionResult(action, await this.request(`/auth/${action}`, {
            method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': this.csrf, 'Idempotency-Key': idempotencyKey},
            body: JSON.stringify(input)
        }));
        if (result.state === 'password_reset') {
            this.on_session_changed('password-reset');
            this.broadcast();
            await this.query('/session').catch(() => null);
        }
        return result;
        } catch (error) {await this.refreshDeniedCsrf(error); throw error;}
    }
    private async refreshDeniedCsrf(error: unknown): Promise<void> {
        if (!(error instanceof ApiError) || error.code !== 'csrf_denied') return;
        let refreshed = false;
        try {
            if (this.sessionRequest) await this.sessionRequest.catch(() => null);
            this.sessionRequest = null;
            await this.query('/session');
            refreshed = true;
        } catch {}
        throw new ApiError(refreshed ? 'Your security session was refreshed. Please submit the form again.' : 'Your security session could not be refreshed. Reload this page before submitting again.', 403, refreshed ? 'csrf_refreshed' : 'csrf_refresh_failed');
    }
    async accountActionStatus(action: AccountAction, operationId: string): Promise<AccountActionResult> {
        if (!/^[0-9a-f-]{36}$/i.test(operationId)) throw new ApiError('Invalid account action reference.', 422, 'invalid_input');
        if (!this.csrf) await this.query('/session');
        const parameters = new URLSearchParams({operation_id: operationId});
        return parseAccountActionResult(action, await this.request('/auth/action-status?' + parameters, {headers:{'X-CSRF-Token':this.csrf}}));
    }
    private broadcast(): void {
        if (typeof window !== 'undefined' && typeof BroadcastChannel !== 'undefined') {
            const channel = new BroadcastChannel('sme-session');
            channel.postMessage({ invalidate: true, sender: this.instanceId });
            channel.close();
        }
    }
    constructor(store = new BrowserRequestStore()) { this.store = store; }
    async get_operation_status(id: string, signal?: AbortSignal): Promise<Dict> { return this.query(`/operations/${encodeURIComponent(id)}`, {}, signal); }
    invalidate(resource: string): void { this.store.invalidate(resource); }
    async mutate(resource: string, operation: string, input: Dict, expected_version?: number, command_id?: string): Promise<Dict> {
        if (!this.session)
            await this.query('/session');
        if (!this.session?.actor)
            throw new ApiError('Sign in or explicitly start a guest shopping session before continuing.', 401, 'authentication_required');
        const identity = JSON.stringify({ resource, operation, input, expected_version });
        const previous = [...this.store.pending.entries()].find(([, record]) => record.identity === identity || (!record.identity && record.resource === resource && record.operation === operation));
        command_id = command_id || previous?.[0] || createCommandId();
        const body: Dict = { operation, command_id, input };
        if (expected_version !== undefined)
            body.expected_version = expected_version;
        this.store.remember(command_id, { resource, operation, started_at: Date.now(), identity });
        const result = await this.request(resource, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this.csrf }, body: JSON.stringify(body) });
        this.store.settle(command_id, result.operation_id || result.id || null, /PENDING|UNKNOWN|REVIEW|PROCESSING|ADMISSION/.test(result.state || ''));
        return result;
    }
    observe_session(listener: (session: Session | null) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
    on_session_changed(epoch: string): void { this.csrf = ''; this.session = null; this.sessionRequest = null; this.controllers.forEach(controller => controller.abort()); this.controllers.clear(); this.store.on_session_changed(epoch); this.listeners.forEach(listener => listener(null)); }
    async query(resource: string, parameters: Dict = {}, signal?: AbortSignal): Promise<any> {
        if (resource === '/session') {
            if (!this.sessionRequest) {
                const revision = this.store.identityRevision;
                const promise = this.request('/session').then(value => {
                    if (revision !== this.store.identityRevision)
                        throw new ApiError('Your session changed. Refresh this information.', 409, 'stale_session');
                    return this.apply_session(value);
                });
                this.sessionRequest = promise;
                promise.finally(() => {
                    if (this.sessionRequest === promise)
                        this.sessionRequest = null;
                }).catch(() => { });
            }
            return this.sessionRequest;
        }
        if (!/^\/(catalog|search)(\/|$)/.test(resource) && !this.session)
            await this.query('/session');
        if (signal?.aborted)
            throw new DOMException('Request aborted', 'AbortError');
        const search = new URLSearchParams();
        Object.entries(parameters).forEach(([key, value]) => {
            if (value !== undefined && value !== null && value !== '')
                search.set(key, String(value));
        });
        return this.request(resource + (search.size ? '?' + search : ''), { signal });
    }
    async receive_session_notice(message: Dict): Promise<Session | null> {
        if (!message || message.invalidate !== true || message.sender === this.instanceId)
            return this.session;
        this.on_session_changed('remote-session-change');
        return this.query('/session');
    }
    private async request(resource: string, options: RequestInit = {}): Promise<any> {
        if (!resource.startsWith('/') || resource.startsWith('//') || resource.includes('://'))
            throw new ApiError('Unsupported application resource.', 400, 'invalid_resource');
        if (options.signal?.aborted)
            throw new DOMException('Request aborted', 'AbortError');
        const ticket = this.store.begin_request(resource), controller = new AbortController();
        const stale = () => ticket.identityRevision !== this.store.identityRevision || ticket.epoch !== this.store.epoch || (options.method !== 'POST' && !this.store.can_publish(ticket));
        this.controllers.add(controller);
        const abort = () => controller.abort();
        options.signal?.addEventListener('abort', abort, { once: true });
        const timer = setTimeout(() => controller.abort(), 15000);
        let response: Response;
        try {
            response = await fetch(API_ROOT + resource, { ...options, signal: controller.signal, credentials: 'same-origin', headers: { Accept: 'application/json', ...options.headers } });
        }
        catch (error) {
            if (stale())
                throw new ApiError('Your session changed. Refresh this information.', 409, 'stale_session');
            if (controller.signal.aborted && options.signal?.aborted)
                throw error;
            throw new ApiError(options.method === 'POST' ? 'The response was lost. Check the original operation status before another attempt.' : 'Unable to connect. Please retry the read.', 0, options.method === 'POST' ? 'request_unknown' : 'unavailable');
        }
        finally {
            clearTimeout(timer);
            this.controllers.delete(controller);
            options.signal?.removeEventListener('abort', abort);
        }
        if (stale())
            throw new ApiError('Your session changed. Refresh this information.', 409, 'stale_session');
        let envelope: Dict = {};
        if (response.status !== 204 && response.headers.get('content-type')?.includes('json')) {
            try {
                envelope = await response.json();
            }
            catch {
                throw new ApiError('The service response is unreadable. Its operation may still be processing.', 502, 'service_contract_error');
            }
        }
        if (stale())
            throw new ApiError('Your session changed. Refresh this information.', 409, 'stale_session');
        if (!response.ok) {
            const error = envelope?.error || envelope || {};
            if (response.status === 401) {
                const preauth = error.details?.preauth, profile = this.session?.profile || 'portfolio-demo';
                this.on_session_changed(preauth?.epoch || 'expired');
                if (preauth?.csrf_token)
                    this.apply_session({ profile, actor: null, challenge: null, csrf_token: preauth.csrf_token, epoch: preauth.epoch, session_generation: preauth.session_generation, idle_expires_at: preauth.expires_at, absolute_expires_at: preauth.expires_at });
            }
            throw new ApiError(error.message || `The request could not be completed (${response.status}).`, response.status, error.code || 'unavailable', error.details || {}, error.operation_id || envelope.meta?.operation_id || null);
        }
        if (response.status !== 204 && (!envelope || typeof envelope !== 'object' || Array.isArray(envelope) || !Object.hasOwn(envelope, 'data')))
            throw new ApiError('The service response is incomplete. Check the original operation before retrying.', 502, 'service_contract_error');
        const data = envelope.data ?? {};
        validateMoney(data);
        return data;
    }
}
