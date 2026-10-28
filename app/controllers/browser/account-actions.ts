import {ApiError} from './errors';
export type AccountAction = 'register' | 'verify-email' | 'resend-verification' | 'forgot-password' | 'reset-password';
export type AccountActionResult = {
    state: 'verification_pending' | 'email_verified' | 'reset_requested' | 'password_reset' | 'reset_pending';
    message: string;
    operation_id?: string;
    delivery_status?: 'queued' | 'LOCAL_CAPTURED' | 'SMTP_ACCEPTED' | 'DELIVERY_FAILED' | 'NOT_APPLICABLE' | 'CANCELED' | 'REQUEST_ACCEPTED';
    retry_after?: number;
    retryable?: boolean;
};
export type AccountActionInput = {
    register: {full_name: string; email: string; password: string; password_confirmation: string};
    'verify-email': {token: string};
    'resend-verification': {email: string};
    'forgot-password': {email: string};
    'reset-password': {token: string; password: string; password_confirmation: string};
};
const fields: Record<AccountAction, string[]> = {
    register: ['full_name', 'email', 'password', 'password_confirmation'],
    'verify-email': ['token'], 'resend-verification': ['email'], 'forgot-password': ['email'],
    'reset-password': ['token', 'password', 'password_confirmation']
};
const states: Record<AccountAction, AccountActionResult['state']> = {
    register: 'verification_pending', 'resend-verification': 'verification_pending',
    'verify-email': 'email_verified', 'forgot-password': 'reset_requested', 'reset-password': 'password_reset'
};
export function validateAccountActionInput(action: AccountAction, input: unknown): void {
    const keys = fields[action];
    if (!keys || !input || typeof input !== 'object' || Array.isArray(input) ||
        Object.keys(input).length !== keys.length || Object.keys(input).some(key => !keys.includes(key)) ||
        Object.values(input).some(value => typeof value !== 'string'))
        throw new ApiError('Invalid account action input.', 422, 'invalid_input');
}
export function parseAccountActionResult(action: AccountAction, value: unknown): AccountActionResult {
    const result = value as AccountActionResult;
    if (!result || typeof result !== 'object' || Array.isArray(result) ||
        Object.keys(result).some(key => !['state', 'message', 'operation_id', 'delivery_status', 'retry_after', 'retryable'].includes(key)) ||
        (result.state !== states[action] && !(action === 'reset-password' && result.state === 'reset_pending')) || typeof result.message !== 'string' || result.message.length > 2000 ||
        (result.delivery_status !== undefined && !['queued', 'LOCAL_CAPTURED', 'SMTP_ACCEPTED', 'DELIVERY_FAILED', 'NOT_APPLICABLE', 'CANCELED', 'REQUEST_ACCEPTED'].includes(result.delivery_status)) ||
        (result.operation_id !== undefined && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(result.operation_id)) ||
        (result.retryable !== undefined && typeof result.retryable !== 'boolean') ||
        (result.retry_after !== undefined && (!Number.isSafeInteger(result.retry_after) || result.retry_after < 0)))
        throw new ApiError('The account service response is incomplete. Check its status before retrying.', 502, 'service_contract_error');
    return result;
}
