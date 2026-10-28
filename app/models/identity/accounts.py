"""Customer enrollment and purpose-bound recovery, without session minting."""
from __future__ import annotations
from datetime import timedelta
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
import uuid
from .errors import IdentityError

ACCOUNT_DDL='''ALTER TABLE demo_identity.accounts ADD COLUMN IF NOT EXISTS full_name text;
ALTER TABLE demo_identity.accounts ADD COLUMN IF NOT EXISTS email_canonical text;
ALTER TABLE demo_identity.accounts ADD COLUMN IF NOT EXISTS email_verified_at timestamptz;
ALTER TABLE demo_identity.accounts ADD COLUMN IF NOT EXISTS customer_registration_state text NOT NULL DEFAULT 'private';
CREATE UNIQUE INDEX IF NOT EXISTS identity_customer_email ON demo_identity.accounts(merchant_id,email_canonical) WHERE email_canonical IS NOT NULL;
CREATE TABLE IF NOT EXISTS demo_identity.public_actions(id text PRIMARY KEY,merchant_id text NOT NULL,carrier_id text NOT NULL,kind text NOT NULL,request_digest text NOT NULL,result jsonb NOT NULL,message_id text,reset_id text,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS demo_identity.customer_tokens(id text PRIMARY KEY,merchant_id text NOT NULL,principal_id text NOT NULL REFERENCES demo_identity.accounts(id),purpose text NOT NULL CHECK(purpose IN ('email_verification','password_reset')),token_digest text UNIQUE NOT NULL,message_id text UNIQUE NOT NULL,state text NOT NULL CHECK(state IN ('PENDING','CLAIMED','CONSUMED','REVOKED')),account_epoch bigint NOT NULL,expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),consumed_at timestamptz);
CREATE INDEX IF NOT EXISTS identity_pending_token ON demo_identity.customer_tokens(principal_id,purpose,state);
CREATE TABLE IF NOT EXISTS demo_identity.password_resets(id text PRIMARY KEY,token_id text UNIQUE NOT NULL REFERENCES demo_identity.customer_tokens(id),principal_id text NOT NULL,request_digest text NOT NULL,password_salt bytea NOT NULL,password_hash bytea NOT NULL,expected_revision bigint NOT NULL,expected_epoch bigint NOT NULL,state text NOT NULL CHECK(state IN ('PREPARED','UPDATED','COMPLETED')),created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE UNIQUE INDEX IF NOT EXISTS identity_one_pending_reset ON demo_identity.password_resets(principal_id) WHERE state<>'COMPLETED';'''

MESSAGES={'register':'If this address is eligible, a verification email will arrive shortly.','resend':'If this address is eligible, a verification email will arrive shortly.','forgot':'If an eligible account exists, a password reset email will arrive shortly.','verify':'Email verified. Sign in to continue.','reset':'Password updated. Sign in with your new password.'}
STATES={'register':'verification_pending','resend':'verification_pending','forgot':'reset_requested','verify':'email_verified','reset':'password_reset'}


def canonical_email(value):
    if not isinstance(value,str):raise IdentityError('invalid_email',422,{'email':'Enter a valid email address.'})
    value=unicodedata.normalize('NFC',value.strip())
    if value.count('@')!=1 or any(c.isspace() or ord(c)<33 or ord(c)==127 for c in value):raise IdentityError('invalid_email',422,{'email':'Enter a valid email address.'})
    local,domain=value.rsplit('@',1)
    try:domain=domain.encode('idna').decode('ascii').lower()
    except (UnicodeError,ValueError):raise IdentityError('invalid_email',422,{'email':'Enter a valid email address.'}) from None
    local=local.casefold()
    if not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}",local) or local.startswith('.') or local.endswith('.') or '..' in local or len(domain)>253 or '.' not in domain or any(not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in domain.split('.')):raise IdentityError('invalid_email',422,{'email':'Enter a valid email address.'})
    result=local+'@'+domain
    if len(result.encode('utf-8'))>254:raise IdentityError('invalid_email',422,{'email':'Email addresses must be at most 254 bytes.'})
    return result


def public_password(password,confirmation):
    try:
        encoded=password.encode('utf-8') if isinstance(password,str) else None
        confirmed=confirmation.encode('utf-8') if isinstance(confirmation,str) else None
    except UnicodeError:raise IdentityError('password_policy',422,{'password':'Use valid Unicode characters.'}) from None
    if encoded is None or not 12<=len(password)<=128 or len(encoded)>512 or '\x00' in password:raise IdentityError('password_policy',422,{'password':'Use 12 to 128 characters.'})
    if confirmed is None or not hmac.compare_digest(encoded,confirmed):raise IdentityError('password_confirmation_mismatch',422,{'password_confirmation':'Passwords must match.'})
    return password


def token_digest(purpose,token):
    if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43}',token):raise IdentityError('verification_token_invalid' if purpose=='email_verification' else 'password_reset_token_invalid',422)
    return hashlib.sha256((purpose+':'+token).encode('ascii')).hexdigest()


class CustomerAccountFlows:
    def _account_digest(self,value):
        key=self.receipt_key_provider() if callable(self.receipt_key_provider) else None
        if not isinstance(key,bytes) or len(key)!=32:raise IdentityError('identity_receipt_key_unavailable',503)
        return hmac.new(key,b'SME-CUSTOMER-ACTION/1\0'+json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8'),hashlib.sha256).hexdigest()

    def _account_carrier(self,tx,credential,csrf):
        from .service import db_now,digest_credential
        if self._closed:raise IdentityError('identity_closed',503)
        self._generation_lock(tx)
        row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s FOR SHARE',(digest_credential(credential),)).fetchone()
        if not row or row['state']!='ACTIVE' or db_now(tx)>=min(row['idle_expires_at'],row['absolute_expires_at']):raise IdentityError('preauth_required')
        if not isinstance(csrf,str) or not hmac.compare_digest(row['csrf'],csrf):raise IdentityError('csrf_denied',403)
        if row['kind'] in ('preauth','guest'):return self._carrier(tx,credential,csrf)
        if row['kind'] not in ('customer','staff'):raise IdentityError('preauth_required')
        account=tx.execute('SELECT roles FROM demo_identity.accounts WHERE id=%s FOR SHARE',(row['principal_id'],)).fetchone()
        if not account:raise IdentityError('session_expired')
        self.admit_session(tx,{'id':row['principal_id'],'kind':row['kind'],'roles':account['roles'],'session_id':row['id'],'epoch':row['epoch'],'account_epoch':row['account_epoch'],'session_generation':row['session_generation']})
        return row

    def _action_preflight(self,kind,payload,preauth_credential,csrf,source_key,operation_id):
        try:
            if str(uuid.UUID(operation_id))!=operation_id:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise IdentityError('invalid_operation_id',422) from None
        if self._closed:raise IdentityError('identity_closed',503)
        with self.db.transaction(self.owner) as tx:
            carrier=self._account_carrier(tx,preauth_credential,csrf)
            ident=str(uuid.uuid5(uuid.UUID(self.merchant_id),carrier['id']+':'+operation_id+':'+kind));digest=self._account_digest([kind,payload])
            previous=tx.execute('SELECT * FROM demo_identity.public_actions WHERE id=%s',(ident,)).fetchone()
            if previous:
                if previous['request_digest']!=digest:raise IdentityError('idempotency_conflict',409)
                return ident,digest,dict(previous['result'])
            self._account_limits(tx,kind,source_key,payload.get('email'))
        return ident,digest,None

    def _account_action(self,kind,payload,*,preauth_credential,csrf,source_key,operation_id,work,_preflight=None):
        ident,digest,previous=_preflight if _preflight is not None else self._action_preflight(kind,payload,preauth_credential,csrf,source_key,operation_id)
        if previous is not None:return previous
        with self.db.transaction(self.owner) as tx:
            carrier=self._account_carrier(tx,preauth_credential,csrf)
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('identity-action:'+ident,))
            previous=tx.execute('SELECT * FROM demo_identity.public_actions WHERE id=%s FOR UPDATE',(ident,)).fetchone()
            if previous:
                if previous['request_digest']!=digest:raise IdentityError('idempotency_conflict',409)
                return dict(previous['result'])
            message_id=work(tx,ident)
            result={'state':STATES[kind],'message':MESSAGES[kind],'operation_id':ident,'delivery_status':'REQUEST_ACCEPTED' if kind in ('register','resend','forgot') else 'NOT_APPLICABLE'}
            if message_id and kind=='resend':
                token=tx.execute('SELECT created_at FROM demo_identity.customer_tokens WHERE message_id=%s',(message_id,)).fetchone();now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
                remaining=60-(now-token['created_at']).total_seconds()
                if remaining>0:result['retry_after']=min(60,int(remaining)+1)
            tx.execute('INSERT INTO demo_identity.public_actions(id,merchant_id,carrier_id,kind,request_digest,result,message_id) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)',(ident,self.merchant_id,carrier['id'],kind,digest,json.dumps(result),message_id))
            return result

    def _account_limits(self,tx,kind,source_key,email=None):
        if not isinstance(source_key,str) or not 1<=len(source_key)<=512:raise IdentityError('invalid_source',422)
        names={'public-source:'+hashlib.sha256(source_key.encode()).hexdigest():20}
        if email:names['public-email:'+kind+':'+hashlib.sha256(email.encode()).hexdigest()]=5
        self._limits(tx,sorted(names),[names[k] for k in sorted(names)])
        now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
        for key in sorted(names):tx.execute("UPDATE demo_identity.authentication_limits SET failures=CASE WHEN window_at<=%s-interval '15 minutes' THEN 1 ELSE failures+1 END,window_at=CASE WHEN window_at<=%s-interval '15 minutes' THEN %s ELSE window_at END WHERE key=%s",(now,now,now,key))

    def _queue_account_token(self,tx,account,purpose):
        if self.mail_port is None or not callable(getattr(self.mail_port,'stage_identity_mail',None)):raise IdentityError('account_email_unavailable',503)
        now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
        previous=tx.execute('SELECT created_at,message_id,state,expires_at,account_epoch FROM demo_identity.customer_tokens WHERE principal_id=%s AND purpose=%s ORDER BY created_at DESC LIMIT 1',(account['id'],purpose)).fetchone()
        if previous and now-previous['created_at']<timedelta(seconds=60) and previous['state']=='PENDING' and previous['expires_at']>now and previous['account_epoch']==account['account_epoch']:return previous['message_id']
        tx.execute("UPDATE demo_identity.customer_tokens SET state='REVOKED' WHERE principal_id=%s AND purpose=%s AND state='PENDING'",(account['id'],purpose))
        token=secrets.token_urlsafe(32);ident=str(uuid.uuid4());message_id=str(uuid.uuid5(uuid.UUID(ident),'mail'));expires=now+timedelta(hours=24) if purpose=='email_verification' else now+timedelta(minutes=30)
        tx.execute("INSERT INTO demo_identity.customer_tokens(id,merchant_id,principal_id,purpose,token_digest,message_id,state,account_epoch,expires_at) VALUES(%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)",(ident,self.merchant_id,account['id'],purpose,token_digest(purpose,token),message_id,account['account_epoch'],expires))
        self.mail_port.stage_identity_mail(tx,message_id=message_id,purpose=purpose,recipient=account['email_canonical'],token=token,expires_at=expires,full_name=account['full_name'])
        return message_id

    def register_customer(self,full_name,email,password,password_confirmation,*,preauth_credential,csrf,source_key,operation_id):
        if not isinstance(full_name,str):raise IdentityError('invalid_full_name',422,{'full_name':'Enter your full name.'})
        full_name=unicodedata.normalize('NFC',full_name.strip())
        if not 2<=len(full_name)<=120 or any(unicodedata.category(c).startswith('C') for c in full_name):raise IdentityError('invalid_full_name',422,{'full_name':'Use 2 to 120 characters without control characters.'})
        email=canonical_email(email);public_password(password,password_confirmation)
        def work(tx,ident):
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('identity-email:'+email,))
            existing=tx.execute('SELECT id FROM demo_identity.accounts WHERE email_canonical=%s OR lower(username)=%s LIMIT 1',(email,email)).fetchone()
            if existing:return None
            salt=secrets.token_bytes(16);hashed=self._public_password_hash(password,salt);pid=str(uuid.uuid4())
            account=tx.execute("INSERT INTO demo_identity.accounts(id,merchant_id,username,kind,roles,password_salt,password_hash,full_name,email_canonical,customer_registration_state,active) VALUES(%s,%s,%s,'customer','[\"customer\"]'::jsonb,%s,%s,%s,%s,'pending',false) RETURNING *",(pid,self.merchant_id,email,salt,hashed,full_name,email)).fetchone()
            message=self._queue_account_token(tx,account,'email_verification');tx.stage_audit(ident,{'operation':'customer_registration_pending','principal_id':pid});return message
        return self._account_action('register',{'full_name':full_name,'email':email,'password':password},preauth_credential=preauth_credential,csrf=csrf,source_key=source_key,operation_id=operation_id,work=work)

    def verify_email(self,token,*,preauth_credential,csrf,source_key,operation_id):
        digest=token_digest('email_verification',token)
        def work(tx,ident):
            row=tx.execute("SELECT * FROM demo_identity.customer_tokens WHERE token_digest=%s AND purpose='email_verification'",(digest,)).fetchone();now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if not row or row['merchant_id']!=self.merchant_id or row['state']!='PENDING' or now>=row['expires_at']:raise IdentityError('verification_token_invalid',422)
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR UPDATE',(row['principal_id'],)).fetchone()
            row=tx.execute('SELECT * FROM demo_identity.customer_tokens WHERE id=%s FOR UPDATE',(row['id'],)).fetchone();now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if row['state']!='PENDING' or now>=row['expires_at']:raise IdentityError('verification_token_invalid',422)
            if account['kind']!='customer' or account['roles']!=['customer'] or account['customer_registration_state']!='pending' or account['account_epoch']!=row['account_epoch']:raise IdentityError('verification_token_invalid',422)
            tx.execute("UPDATE demo_identity.accounts SET active=true,customer_registration_state='verified',email_verified_at=%s,revision=revision+1 WHERE id=%s",(now,account['id']))
            tx.execute("UPDATE demo_identity.customer_tokens SET state='CONSUMED',consumed_at=%s WHERE id=%s",(now,row['id']));tx.stage_audit(ident,{'operation':'customer_email_verified','principal_id':account['id']});return None
        return self._account_action('verify',{'token_digest':digest},preauth_credential=preauth_credential,csrf=csrf,source_key=source_key,operation_id=operation_id,work=work)

    def resend_verification(self,email,*,preauth_credential,csrf,source_key,operation_id):
        email=canonical_email(email)
        def work(tx,ident):
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE merchant_id=%s AND email_canonical=%s FOR UPDATE',(self.merchant_id,email)).fetchone()
            if account and account['kind']=='customer' and account['roles']==['customer'] and account['customer_registration_state']=='pending':return self._queue_account_token(tx,account,'email_verification')
            return None
        return self._account_action('resend',{'email':email},preauth_credential=preauth_credential,csrf=csrf,source_key=source_key,operation_id=operation_id,work=work)

    def request_password_reset(self,email,*,preauth_credential,csrf,source_key,operation_id):
        email=canonical_email(email)
        preflight=self._action_preflight('forgot',{'email':email},preauth_credential,csrf,source_key,operation_id)
        if preflight[2] is not None:return preflight[2]
        # Resume only an already-admitted immutable hash, not anything chosen by
        # this email-only request. No account row lock spans the AAC transaction.
        with self.db.transaction(self.owner) as tx:
            self._account_carrier(tx,preauth_credential,csrf)
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE merchant_id=%s AND email_canonical=%s FOR UPDATE',(self.merchant_id,email)).fetchone()
            if account:self._retire_stale_reset(tx,account)
            pending=tx.execute("SELECT id,principal_id FROM demo_identity.password_resets WHERE principal_id=%s AND state IN ('PREPARED','UPDATED')",(account['id'],)).fetchone() if account and account['kind']=='customer' and account['roles']==['customer'] and account['active'] and account['customer_registration_state']=='verified' else None
        if pending:
            try:self._finish_admitted_reset(pending['id'],pending['principal_id'])
            except IdentityError:
                with self.db.transaction(self.owner) as tx:
                    current=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR UPDATE',(pending['principal_id'],)).fetchone()
                    if not current or not self._retire_stale_reset(tx,current):raise
        def work(tx,ident):
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE merchant_id=%s AND email_canonical=%s FOR UPDATE',(self.merchant_id,email)).fetchone()
            if account:self._retire_stale_reset(tx,account)
            if account and account['kind']=='customer' and account['roles']==['customer'] and account['active'] and account['customer_registration_state']=='verified' and account['email_verified_at'] and not tx.execute("SELECT id FROM demo_identity.password_resets WHERE principal_id=%s AND state<>'COMPLETED'",(account['id'],)).fetchone():return self._queue_account_token(tx,account,'password_reset')
            return None
        return self._account_action('forgot',{'email':email},preauth_credential=preauth_credential,csrf=csrf,source_key=source_key,operation_id=operation_id,work=work,_preflight=preflight)

    def get_account_action_status(self,operation_id,*,preauth_credential,csrf,source_key=None):
        with self.db.transaction(self.owner) as tx:
            carrier=self._account_carrier(tx,preauth_credential,csrf)
            row=tx.execute('SELECT * FROM demo_identity.public_actions WHERE id=%s AND merchant_id=%s AND carrier_id=%s',(operation_id,self.merchant_id,carrier['id'])).fetchone()
            if not row:raise IdentityError('not_found',404)
            if row['reset_id'] and row['result']['state']=='reset_pending':
                terminal=tx.execute('SELECT r.state,t.state AS token_state FROM demo_identity.password_resets r JOIN demo_identity.customer_tokens t ON t.id=r.token_id WHERE r.id=%s',(row['reset_id'],)).fetchone()
                if terminal and terminal['state']=='COMPLETED' and terminal['token_state']!='CONSUMED':raise IdentityError('password_reset_token_invalid',422)
            result=dict(row['result'])
            if row['message_id']:
                if self.mail_port is None:raise IdentityError('account_email_unavailable',503)
                delivery=self.mail_port.status(tx,message_id=row['message_id']);result['delivery_status']='queued' if delivery['state'] in ('QUEUED','SENDING') else delivery['state'];result['retryable']=delivery.get('retryable',False)
            return result

    def verify_mail_intent(self,tx,*,message_id,purpose,recipient,token_digest,expires_at):
        from app.models.persistence.service import OwnerTransaction
        if not isinstance(tx,OwnerTransaction) or tx._persistence is not self.db or not tx.active or tx.owner not in ('demo_identity','notifications'):raise IdentityError('invalid_mail_intent',403)
        found=tx.execute('SELECT principal_id FROM demo_identity.customer_tokens WHERE message_id=%s',(message_id,)).fetchone()
        if not found:raise IdentityError('stale_mail_intent',403)
        account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',(found['principal_id'],)).fetchone()
        row=tx.execute('SELECT * FROM demo_identity.customer_tokens WHERE message_id=%s FOR SHARE',(message_id,)).fetchone();now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
        if not row or not account or row['merchant_id']!=self.merchant_id or row['state']!='PENDING' or row['purpose']!=purpose or account['email_canonical']!=recipient or not isinstance(token_digest,str) or not hmac.compare_digest(row['token_digest'],token_digest) or row['expires_at']!=expires_at or now>=expires_at or row['account_epoch']!=account['account_epoch'] or account['kind']!='customer' or account['roles']!=['customer'] or (purpose=='email_verification' and account['customer_registration_state']!='pending') or (purpose=='password_reset' and (not account['active'] or account['customer_registration_state']!='verified')):raise IdentityError('stale_mail_intent',403)
        return {'message_id':message_id,'purpose':purpose,'recipient':recipient,'token_digest':token_digest,'expires_at':expires_at,'state':'CURRENT'}

    def _retire_stale_reset(self,tx,account):
        reset=tx.execute("SELECT * FROM demo_identity.password_resets WHERE principal_id=%s AND state='PREPARED' FOR UPDATE",(account['id'],)).fetchone()
        if reset and (account['revision'],account['account_epoch'])!=(reset['expected_revision'],reset['expected_epoch']):
            tx.execute("UPDATE demo_identity.password_resets SET state='COMPLETED' WHERE id=%s",(reset['id'],))
            tx.execute("UPDATE demo_identity.customer_tokens SET state='REVOKED' WHERE id=%s AND state IN ('PENDING','CLAIMED')",(reset['token_id'],))
            tx.stage_audit(str(uuid.uuid5(uuid.UUID(reset['id']),'superseded')),{'operation':'customer_reset_superseded','principal_id':account['id']})
            return True
        return False

    def _reset_publication_guard(self,tx,reset_id,principal_id):
        from app.models.persistence.service import OwnerTransaction
        if not isinstance(tx,OwnerTransaction) or tx._persistence is not self.db or tx.owner!='authority':raise IdentityError('invalid_reset_publication',403)
        account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',(principal_id,)).fetchone()
        reset=tx.execute('SELECT * FROM demo_identity.password_resets WHERE id=%s FOR SHARE',(reset_id,)).fetchone()
        offset=1 if reset and reset['state'] in ('UPDATED','COMPLETED') else 0
        if not account or not reset or reset['state'] not in ('PREPARED','UPDATED','COMPLETED') or account['kind']!='customer' or account['roles']!=['customer'] or not account['active'] or account['customer_registration_state']!='verified' or (account['revision'],account['account_epoch'])!=(reset['expected_revision']+offset,reset['expected_epoch']+offset):raise IdentityError('password_reset_token_invalid',422)
        token=tx.execute('SELECT * FROM demo_identity.customer_tokens WHERE id=%s FOR SHARE',(reset['token_id'],)).fetchone()
        expected_state='CLAIMED' if reset['state']=='PREPARED' else 'CONSUMED'
        if not token or token['state']!=expected_state or token['purpose']!='password_reset' or token['merchant_id']!=self.merchant_id or token['principal_id']!=principal_id or token['account_epoch']!=reset['expected_epoch']:raise IdentityError('password_reset_token_invalid',422)
        return True

    def reset_password(self,token,password,password_confirmation,*,preauth_credential,csrf,source_key,operation_id):
        public_password(password,password_confirmation);digest=token_digest('password_reset',token)
        try:
            if str(uuid.UUID(operation_id))!=operation_id:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise IdentityError('invalid_operation_id',422) from None
        if self._closed or self.authority is None:raise IdentityError('identity_closed',503)
        action_id,request_digest,prior=self._action_preflight('reset',{'token_digest':digest,'password':password,'recovery_key':operation_id},preauth_credential,csrf,source_key,operation_id)
        with self.db.transaction(self.owner) as tx:
            carrier=self._account_carrier(tx,preauth_credential,csrf)
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('identity-action:'+action_id,))
            action=tx.execute('SELECT * FROM demo_identity.public_actions WHERE id=%s FOR UPDATE',(action_id,)).fetchone()
            if action and action['request_digest']!=request_digest:raise IdentityError('idempotency_conflict',409)
            token_row=tx.execute("SELECT * FROM demo_identity.customer_tokens WHERE token_digest=%s AND purpose='password_reset'",(digest,)).fetchone()
            if not token_row or token_row['merchant_id']!=self.merchant_id:raise IdentityError('password_reset_token_invalid',422)
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR UPDATE',(token_row['principal_id'],)).fetchone()
            row=tx.execute('SELECT * FROM demo_identity.customer_tokens WHERE id=%s FOR UPDATE',(token_row['id'],)).fetchone()
            reset_id=str(uuid.uuid5(uuid.UUID(row['id']),'password-reset'))
            reset=tx.execute('SELECT * FROM demo_identity.password_resets WHERE id=%s FOR UPDATE',(reset_id,)).fetchone();now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if account['kind']!='customer' or account['roles']!=['customer'] or not account['active'] or account['customer_registration_state']!='verified' or not account['email_verified_at']:raise IdentityError('password_reset_token_invalid',422)
            stale=self._retire_stale_reset(tx,account) if reset and reset['state']=='PREPARED' else False
            if reset:
                if reset['request_digest']!=request_digest:raise IdentityError('password_reset_token_invalid',422) if reset['state']=='COMPLETED' else IdentityError('idempotency_conflict',409)
                if reset['state']=='COMPLETED':
                    if row['state']!='CONSUMED' or (account['revision'],account['account_epoch'])!=(reset['expected_revision']+1,reset['expected_epoch']+1):raise IdentityError('password_reset_token_invalid',422)
                    completed={'state':'password_reset','message':MESSAGES['reset'],'operation_id':action_id,'delivery_status':'NOT_APPLICABLE'}
                    tx.execute("INSERT INTO demo_identity.public_actions(id,merchant_id,carrier_id,kind,request_digest,result,reset_id) VALUES(%s,%s,%s,'reset',%s,%s::jsonb,%s) ON CONFLICT(id) DO NOTHING",(action_id,self.merchant_id,carrier['id'],request_digest,json.dumps(completed),reset_id))
                    return completed
            else:
                if row['state']!='PENDING' or now>=row['expires_at'] or row['account_epoch']!=account['account_epoch']:raise IdentityError('password_reset_token_invalid',422)
                if tx.execute("SELECT 1 FROM demo_identity.password_resets WHERE principal_id=%s AND state<>'COMPLETED'",(account['id'],)).fetchone():raise IdentityError('password_reset_in_progress',409)
                salt=secrets.token_bytes(16);hashed=self._public_password_hash(password,salt)
                tx.execute("INSERT INTO demo_identity.password_resets(id,token_id,principal_id,request_digest,password_salt,password_hash,expected_revision,expected_epoch,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'PREPARED')",(reset_id,row['id'],account['id'],request_digest,salt,hashed,account['revision'],account['account_epoch']))
                tx.execute("UPDATE demo_identity.customer_tokens SET state='CLAIMED' WHERE id=%s",(row['id'],))
            if not action and not stale:
                pending={'state':'reset_pending','message':'Your password update is being completed.','operation_id':action_id,'delivery_status':'NOT_APPLICABLE'}
                tx.execute('INSERT INTO demo_identity.public_actions(id,merchant_id,carrier_id,kind,request_digest,result,reset_id) VALUES(%s,%s,%s,\'reset\',%s,%s::jsonb,%s)',(action_id,self.merchant_id,carrier['id'],request_digest,json.dumps(pending),reset_id))
        if stale:raise IdentityError('password_reset_token_invalid',422)
        self._finish_admitted_reset(reset_id,account['id'])
        with self.db.transaction(self.owner) as tx:
            return dict(tx.execute('SELECT result FROM demo_identity.public_actions WHERE id=%s',(action_id,)).fetchone()['result'])

    def _finish_admitted_reset(self,reset_id,principal_id):
        # The native principal fence closes before any hash/epoch mutation. Its
        # original durable operation survives a crash; no guest is converted.
        with self.db.transaction(self.owner) as tx:sessions=tx.execute("SELECT id FROM demo_identity.sessions WHERE principal_id=%s AND state IN ('ACTIVE','PUBLISHING')",(principal_id,)).fetchall()
        closed=self.authority.prepare_principal_change(self.merchant_id,principal_id,[r['id'] for r in sessions],reset_id,admission_guard=lambda tx:self._reset_publication_guard(tx,reset_id,principal_id))
        with self.db.transaction(self.owner) as tx:
            current=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR UPDATE',(principal_id,)).fetchone()
            reset=tx.execute('SELECT * FROM demo_identity.password_resets WHERE id=%s FOR UPDATE',(reset_id,)).fetchone()
            if reset['state']=='PREPARED':
                if current['kind']!='customer' or current['roles']!=['customer'] or current['customer_registration_state']!='verified' or not current['active'] or (current['revision'],current['account_epoch'])!=(reset['expected_revision'],reset['expected_epoch']):raise IdentityError('identity_version_conflict',409)
                tx.execute('UPDATE demo_identity.accounts SET password_salt=%s,password_hash=%s,password_version=password_version+1,account_epoch=account_epoch+1,revision=revision+1 WHERE id=%s',(reset['password_salt'],reset['password_hash'],current['id']))
                tx.execute("UPDATE demo_identity.sessions SET state='REVOKED',epoch=epoch+1 WHERE principal_id=%s AND state<>'REVOKED'",(current['id'],))
                tx.execute("UPDATE demo_identity.preauth_challenges SET state='REVOKED' WHERE principal_id=%s",(current['id'],))
                tx.execute("UPDATE demo_identity.customer_tokens SET state=CASE WHEN id=%s THEN 'CONSUMED' ELSE 'REVOKED' END,consumed_at=clock_timestamp() WHERE principal_id=%s AND state IN ('PENDING','CLAIMED')",(reset['token_id'],current['id']))
                current=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(current['id'],)).fetchone();self._receipt(tx,'customer_password_reset',reset_id,current,None,False)
                tx.execute("UPDATE demo_identity.password_resets SET state='UPDATED' WHERE id=%s",(reset_id,))
        self.authority.publish_session_binding(self.merchant_id,reset_id,expected_epochs=closed['expected_epochs'],native_receipt={'receipt_id':reset_id,'operation_id':reset_id})
        with self.db.transaction(self.owner) as tx:
            actions=tx.execute('SELECT id,result FROM demo_identity.public_actions WHERE reset_id=%s ORDER BY id FOR UPDATE',(reset_id,)).fetchall()
            tx.execute("UPDATE demo_identity.password_resets SET state='COMPLETED' WHERE id=%s",(reset_id,))
            for action in actions:
                result={'state':'password_reset','message':MESSAGES['reset'],'operation_id':action['id'],'delivery_status':'NOT_APPLICABLE'}
                tx.execute('UPDATE demo_identity.public_actions SET result=%s::jsonb WHERE id=%s',(json.dumps(result),action['id']))
        return None

    def _public_password_hash(self,password,salt):
        if not self._slots.acquire(blocking=False):raise IdentityError('authentication_overload',429)
        try:return hashlib.scrypt(password.encode('utf-8'),salt=salt,n=32768,r=8,p=1,maxmem=64*1024*1024,dklen=32)
        finally:self._slots.release()
