import type {ReturnTypeOfAccountAccess} from '../../controllers/browser/hooks/account-access';
import {AccountField} from './account-field';
import {Icon, NavLink} from './common';
type Props = {model:ReturnTypeOfAccountAccess};
type Navigation = {navigate:(path:string)=>void};

export function DeliveryStatus({model}:Props) {
    return <>{model.receipt?.delivery_status==='LOCAL_CAPTURED' && <p className="fine" role="status">The account email is available in the local test inbox.</p>}
        {model.receipt?.delivery_status==='SMTP_ACCEPTED' && <p className="fine" role="status">The mail service accepted the account email. Check your inbox and spam folder.</p>}
        {model.deliveryCanceled && <p className="fine" role="status">This email request was replaced or expired. Request a fresh link below.</p>}
        {model.deliveryPending && <p className="fine" role="status">Email is queued. This page checks the delivery status automatically.</p>}
        {model.deliveryFailed && <p className="field-error" role="alert">{model.receipt?.retryable ? 'An email delivery attempt failed. The service will retry; you can also request a fresh email.' : 'Email delivery is unavailable. Check the address and request another email.'}</p>}</>;
}

export function RegistrationForm({model}:Props) {
    const {values,errors,busy,ready,change,submit}=model;
    return <form className="form-stack" onSubmit={submit} noValidate>
        <h2>A little more personal.</h2>
        <p className="fine">Create a customer account, verify your email, then sign in. Your guest bag stays available while you do.</p>
        <AccountField name="full_name" label="Full name" value={values.full_name} onChange={change('full_name')} error={errors.full_name} autoComplete="name" maxLength={512} disabled={busy || !ready}/>
        <AccountField name="email" label="Email address" type="email" value={values.email} onChange={change('email')} error={errors.email} autoComplete="email" maxLength={1024} disabled={busy || !ready}/>
        <AccountField name="password" label="Password" type="password" value={values.password} onChange={change('password')} error={errors.password} autoComplete="new-password" maxLength={1024} hint="Use 12–128 characters. A longer, unique password works well." disabled={busy || !ready}/>
        <AccountField name="password_confirmation" label="Confirm password" type="password" value={values.password_confirmation} onChange={change('password_confirmation')} error={errors.password_confirmation} autoComplete="new-password" maxLength={1024} disabled={busy || !ready}/>
        <button className="button" disabled={busy || !ready}>{busy ? 'Creating account…' : 'Create account'} <Icon name="arrow"/></button>
    </form>;
}

export function VerificationRequest({model,registration=false}:Props & {registration?:boolean}) {
    const {values,errors,busy,ready,receipt,pendingVerification,linkState,change,resend}=model;
    const title=registration || pendingVerification ? 'Check your email.' : linkState==='expired' ? 'This verification link has expired.' : linkState==='used' ? 'This verification link has already been used.' : 'A fresh verification link is needed.';
    return <><div role="status"><h2>{title}</h2><p className="account-copy">{registration || pendingVerification ? receipt?.message : 'Verification links work once and expire. If you already verified, you can sign in. Otherwise, request a new email below.'}</p></div>
        <DeliveryStatus model={model}/>
        <form className="form-stack" onSubmit={resend} noValidate>
            <AccountField name="email" label="Email address" type="email" value={values.email} onChange={change('email')} error={errors.email} autoComplete="email" maxLength={1024} disabled={busy || !ready}/>
            <button className="button secondary" disabled={busy || !ready}>{busy ? 'Requesting email…' : 'Resend verification email'}</button>
        </form></>;
}

export function VerificationConfirmation({model}:Props) {
    return <form className="form-stack" onSubmit={model.submit}><h2>One last step.</h2>
        <p className="fine">Confirm your email address to activate your customer account. This does not sign you in.</p>
        <button className="button" disabled={model.busy || !model.ready || model.linkState!=='ready'}>{model.busy ? 'Verifying email…' : 'Verify email'} <Icon name="check"/></button>
    </form>;
}

export function PasswordRecoveryForm({model}:Props) {
    const {values,errors,busy,ready,receipt,change,submit}=model;
    return <><p className="account-copy">Enter your account email and we’ll send instructions if it is eligible.</p>
        {receipt?.state==='reset_requested' && <p role="status" className="status-banner">{receipt.message}</p>}
        <DeliveryStatus model={model}/>
        <form className="form-stack" onSubmit={submit} noValidate>
            <AccountField name="email" label="Email address" type="email" value={values.email} onChange={change('email')} error={errors.email} autoComplete="email" maxLength={1024} disabled={busy || !ready}/>
            <button className="button" disabled={busy || !ready}>{busy ? 'Requesting instructions…' : 'Send reset instructions'} <Icon name="arrow"/></button>
        </form></>;
}

export function PasswordResetForm({model}:Props) {
    const {values,errors,busy,ready,resetPending,uncertainReset,linkState,change,submit}=model;
    const disabled=busy || resetPending || !ready || linkState!=='ready';
    return <form className="form-stack" onSubmit={submit} noValidate>
        <p className="fine">Choose a unique password. All previous account sessions will be signed out.</p>
        {uncertainReset && !resetPending && <p className="status-banner" role="status">The response is uncertain. Enter the same new password to retry the original update. To choose a different password, request a fresh reset link.</p>}
        {resetPending && <p className="status-banner" role="status">Your password update is still being completed. Retry this same request, or try signing in with your new password.</p>}
        <AccountField name="password" label="New password" type="password" value={values.password} onChange={change('password')} error={errors.password} autoComplete="new-password" maxLength={1024} hint="Use 12–128 characters." disabled={disabled}/>
        <AccountField name="password_confirmation" label="Confirm new password" type="password" value={values.password_confirmation} onChange={change('password_confirmation')} error={errors.password_confirmation} autoComplete="new-password" maxLength={1024} disabled={disabled}/>
        <button className="button" disabled={busy || !ready || linkState!=='ready'}>{busy ? 'Saving password…' : resetPending ? 'Retry password update' : 'Reset password'} <Icon name="lock"/></button>
    </form>;
}

export function ResetLinkUnavailable({model,navigate}:Props & Navigation) {
    return <div role="status"><h2>{model.linkState==='expired' ? 'This reset link has expired.' : model.linkState==='used' ? 'This reset link has already been used.' : 'A fresh reset link is needed.'}</h2>
        <p className="account-copy">Use the complete link from your email. If it is invalid, expired, or already used, request a new one.</p>
        <NavLink href="/forgot-password" navigate={navigate} className="button secondary">Request a new reset link</NavLink>
    </div>;
}

export function AccountActionComplete({model,navigate}:Props & Navigation) {
    const verified=model.receipt?.state==='email_verified';
    return <div role="status"><h2>{verified ? 'Your email is verified.' : 'Your password has been reset.'}</h2>
        <p className="account-copy">{verified ? 'Your account is ready. Sign in to continue shopping and view your orders.' : 'Your previous sessions have been signed out. Sign in with your new password.'}</p>
        <NavLink href="/account" navigate={navigate} className="button">Sign in <Icon name="arrow"/></NavLink>
    </div>;
}
