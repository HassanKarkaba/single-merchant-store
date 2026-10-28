import type {ReactNode} from 'react';
import {BrowserApiClient} from '../../controllers/browser/api-client';
import {Session} from '../../controllers/browser/contracts';
import {AccountAccessMode,useAccountAccessController} from '../../controllers/browser/hooks/account-access';
import {AccountActionComplete,PasswordRecoveryForm,PasswordResetForm,RegistrationForm,ResetLinkUnavailable,VerificationConfirmation,VerificationRequest} from '../components/account-forms';
import {ErrorSummary,NavLink,PageHead} from '../components/common';

export function AccountAccessScreen({mode,client,session,navigate}: {mode:AccountAccessMode;client:BrowserApiClient;session:Session|null;navigate:(path:string)=>void}) {
    const model=useAccountAccessController({mode,client,session});
    const title=mode==='register' ? 'Create your account' : mode==='verify-email' ? 'Verify your email' : mode==='forgot-password' ? 'Forgot your password?' : 'Choose a new password';
    const blockedLink=['missing','invalid','expired','used'].includes(model.linkState);
    let content:ReactNode;
    if(model.receipt?.state==='email_verified' || model.receipt?.state==='password_reset') content=<AccountActionComplete model={model} navigate={navigate}/>;
    else if(mode==='register') content=model.pendingVerification ? <VerificationRequest model={model} registration/> : <RegistrationForm model={model}/>;
    else if(mode==='verify-email') content=blockedLink ? <VerificationRequest model={model}/> : <VerificationConfirmation model={model}/>;
    else if(mode==='forgot-password') content=<PasswordRecoveryForm model={model}/>;
    else content=blockedLink ? <ResetLinkUnavailable model={model} navigate={navigate}/> : <PasswordResetForm model={model}/>;
    return <div className="container"><PageHead title={title} subtitle="A considered place for your orders and account."/>
        <div className="account-layout"><section className="panel" aria-busy={model.busy}>
            {model.error && <ErrorSummary message={model.error}/>}{content}
            <div className="account-links"><NavLink href="/account" navigate={navigate}>Already have an account? Sign in</NavLink>
                {mode!=='register' && <NavLink href="/register" navigate={navigate}>Create an account</NavLink>}
            </div>
        </section></div>
    </div>;
}
