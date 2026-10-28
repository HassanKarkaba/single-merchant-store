import { MERCHANT_ROLES } from './constants';
import { ApiError } from './errors';
export function validateMoney(value: any, depth = 0): void {
    if (depth > 30)
        throw new ApiError('The service response exceeds its structure limit.', 502, 'service_contract_error');
    if (value && typeof value === 'object')
        for (const [key, item] of Object.entries(value)) {
            if ((key.endsWith('_minor') || key === 'quantity') && item !== null && (typeof item !== 'number' || !Number.isSafeInteger(item) || (key === 'total_minor' && (item < 0 || item > 99999999))))
                throw new ApiError('The service returned an invalid exact amount or quantity.', 502, 'service_contract_error');
            if (item && typeof item === 'object')
                validateMoney(item, depth + 1);
        }
}
export function validateSessionDTO(value: any): void {
    const fields = ['profile', 'csrf_token', 'actor', 'epoch', 'session_generation', 'idle_expires_at', 'absolute_expires_at', 'challenge'];
    const scalar = (item: any) => (typeof item === 'string' && item.length > 0 && item.length <= 256) || (typeof item === 'number' && Number.isSafeInteger(item) && item >= 0);
    if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => !fields.includes(key)) || typeof value.profile !== 'string' || typeof value.csrf_token !== 'string' || value.csrf_token.length < 1 || value.csrf_token.length > 512 || !scalar(value.epoch) || !scalar(value.session_generation) || !('actor' in value) || !('challenge' in value))
        throw new ApiError('Invalid current session response.', 502, 'service_contract_error');
    if (value.actor !== null) {
        const actor = value.actor;
        if (!actor || Object.keys(actor).some(key => !['id','kind','roles','full_name','email'].includes(key)) || typeof actor.id !== 'string' || !['guest','customer','staff'].includes(actor.kind) || !Array.isArray(actor.roles) || new Set(actor.roles).size !== actor.roles.length || actor.roles.some((role: any) => typeof role !== 'string' || !['guest','customer',...MERCHANT_ROLES].includes(role)))
            throw new ApiError('Invalid session principal.', 502, 'service_contract_error');
        const named = Object.hasOwn(actor,'full_name') || Object.hasOwn(actor,'email');
        if (named && (actor.kind !== 'customer' || typeof actor.full_name !== 'string' || [...actor.full_name].length < 2 || [...actor.full_name].length > 120 || /[\u0000-\u001f\u007f]/.test(actor.full_name) || typeof actor.email !== 'string' || actor.email.length > 254 || !/^[^\s@]+@[^\s@]+$/.test(actor.email)))
            throw new ApiError('Invalid customer account response.', 502, 'service_contract_error');
    }
    if (value.challenge !== null) {
        const challenge = value.challenge;
        if (!challenge || Object.keys(challenge).some(key => !['id','state','expires_at'].includes(key)) || typeof challenge.id !== 'string' || challenge.state !== 'mfa_required' || !scalar(challenge.expires_at))
            throw new ApiError('Invalid MFA challenge response.', 502, 'service_contract_error');
    }
}
