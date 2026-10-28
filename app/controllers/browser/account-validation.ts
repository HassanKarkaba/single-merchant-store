export type AccountField = 'full_name' | 'email' | 'password' | 'password_confirmation';
export type AccountFields = Record<AccountField, string>;
export type AccountFieldErrors = Partial<Record<AccountField, string>>;
export function validateAccountFields(values: AccountFields, action: 'register' | 'forgot-password' | 'resend-verification' | 'reset-password'): AccountFieldErrors {
    const errors: AccountFieldErrors = {};
    if (action === 'register') {
        const name = values.full_name.trim().normalize('NFC');
        if ([...name].length < 2 || [...name].length > 120 || /\p{C}/u.test(name)) errors.full_name = 'Enter your full name, between 2 and 120 characters.';
    }
    if (action !== 'reset-password' && (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email.trim()) || values.email.trim().length > 1024))
        errors.email = 'Enter a valid email address.';
    if (action === 'register' || action === 'reset-password') {
        if ([...values.password].length < 12 || [...values.password].length > 128 || new TextEncoder().encode(values.password).length > 512)
            errors.password = 'Use a password between 12 and 128 characters.';
        if (values.password_confirmation !== values.password) errors.password_confirmation = 'The passwords must match.';
    }
    return errors;
}

export function accountFieldErrorsFromApi(details:unknown, privateValues:string[]=[]): AccountFieldErrors {
    const object=details && typeof details==='object' && !Array.isArray(details) ? details as Record<string,unknown> : {};
    const candidate=object.field_errors || object.errors || object;
    if (!candidate || typeof candidate!=='object' || Array.isArray(candidate)) return {};
    const errors:AccountFieldErrors={};
    for (const field of ['full_name','email','password','password_confirmation'] as AccountField[]) {
        if (!Object.hasOwn(candidate,field)) continue;
        const value=(candidate as Record<string,unknown>)[field];
        if (typeof value==='string' && value.length<=300 && !privateValues.some(secret=>secret && value.includes(secret))) errors[field]=value;
    }
    return errors;
}
