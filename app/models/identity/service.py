from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import uuid
from .errors import IdentityError
from .accounts import ACCOUNT_DDL, CustomerAccountFlows, canonical_email


DDL='''CREATE SCHEMA IF NOT EXISTS demo_identity;
CREATE TABLE IF NOT EXISTS demo_identity.accounts(
 id text PRIMARY KEY,merchant_id text NOT NULL,username text UNIQUE NOT NULL,kind text NOT NULL,
 roles jsonb NOT NULL,password_salt bytea,password_hash bytea,password_version int NOT NULL DEFAULT 1,
 revision bigint NOT NULL DEFAULT 1,account_epoch bigint NOT NULL DEFAULT 1,active boolean NOT NULL DEFAULT true,
 factor_ref text,factor_version bigint NOT NULL DEFAULT 0,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS demo_identity.sessions(
 id text PRIMARY KEY,principal_id text REFERENCES demo_identity.accounts(id),kind text NOT NULL,
 csrf text NOT NULL,state text NOT NULL,epoch bigint NOT NULL DEFAULT 1,session_generation text NOT NULL,
 account_epoch bigint NOT NULL DEFAULT 0,identity_revision bigint NOT NULL DEFAULT 1,auth_strength text NOT NULL,
 issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),last_activity timestamptz NOT NULL DEFAULT clock_timestamp(),
 idle_expires_at timestamptz NOT NULL,absolute_expires_at timestamptz NOT NULL,publication_id text);
CREATE TABLE IF NOT EXISTS demo_identity.preauth_challenges(
 id text PRIMARY KEY,credential_digest text UNIQUE NOT NULL,preauth_digest text NOT NULL,principal_id text NOT NULL,
 csrf text NOT NULL,account_epoch bigint NOT NULL,factor_version bigint NOT NULL,failures int NOT NULL DEFAULT 0,
 state text NOT NULL,expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS demo_identity.factor_counters(principal_id text PRIMARY KEY REFERENCES demo_identity.accounts(id),factor_version bigint NOT NULL,last_accepted_counter bigint NOT NULL DEFAULT -1);
CREATE TABLE IF NOT EXISTS demo_identity.authentication_limits(key text PRIMARY KEY,window_at timestamptz NOT NULL,failures int NOT NULL);
CREATE TABLE IF NOT EXISTS demo_identity.operation_receipts(id text PRIMARY KEY,operation_id text NOT NULL,principal_id text,session_id text,kind text NOT NULL,metadata jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE INDEX IF NOT EXISTS demo_session_principal ON demo_identity.sessions(principal_id);
'''

STAFF_ROLES=frozenset({'catalog_editor','stock_operator','support','finance','merchant_admin'})

RECEIPT_DDL='''ALTER TABLE demo_identity.operation_receipts ADD COLUMN IF NOT EXISTS receipt_mac text;
ALTER TABLE demo_identity.operation_receipts ADD COLUMN IF NOT EXISTS signature_version text;
UPDATE demo_identity.sessions SET state='QUARANTINED',epoch=epoch+1
WHERE state IN ('ACTIVE','PUBLISHING') AND kind<>'preauth' AND publication_id IN
(SELECT id FROM demo_identity.operation_receipts WHERE receipt_mac IS NULL);'''

GENERATION_DDL='''ALTER TABLE demo_identity.accounts ADD COLUMN IF NOT EXISTS factor_fingerprint text;
CREATE TABLE IF NOT EXISTS demo_identity.generation_state(singleton integer PRIMARY KEY CHECK(singleton=1),epoch bigint NOT NULL CHECK(epoch>0),last_reset_id text);
INSERT INTO demo_identity.generation_state(singleton,epoch) VALUES(1,1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS demo_identity.generation_resets(operation_id text PRIMARY KEY,restore_id text UNIQUE NOT NULL,state text NOT NULL CHECK(state IN ('PREPARED','COMPLETED')),payload jsonb NOT NULL,receipt_mac text NOT NULL,signature_version text NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp());'''

@dataclass(frozen=True)
class IdentityResult:
    public:dict
    credential:str|None=None
    challenge_credential:str|None=None
    preauth_credential:str|None=None
    clear_auth:bool=False
    clear_challenge:bool=False
    clear_preauth:bool=False
    expired:bool=False

class PortfolioDemoIdentity(CustomerAccountFlows):
    MIGRATIONS=((1,DDL),(2,RECEIPT_DDL),(3,GENERATION_DDL),(4,ACCOUNT_DDL))
    owner='demo_identity'
    def __init__(self,db,*,merchant_id,profile,authority=None,secret_provider=None,transition_capture=None,receipt_key_provider=None):
        if profile not in ('local-sandbox','demo-test','portfolio-demo'):raise IdentityError('profile_denied',503)
        self.db,self.merchant_id,self.profile=db,canonical_uuid(merchant_id),profile
        self.authority,self.secret_provider,self.transition_capture=authority,secret_provider,transition_capture
        self.receipt_key_provider=receipt_key_provider;self.mail_port=None
        self._slots=threading.BoundedSemaphore(2);self._closed=False
        self._dummy_salt=secrets.token_bytes(16)
        self._dummy_hash=hash_password('not-a-user-password',self._dummy_salt)
    def _carrier(self,tx,credential,csrf=None,*,allow_guest=True):
        self._generation_lock(tx)
        row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s FOR SHARE',(digest_credential(credential),)).fetchone()
        now=db_now(tx)
        if not row or row['state']!='ACTIVE' or row['kind'] not in (('preauth','guest') if allow_guest else ('preauth',)) or now>=min(row['idle_expires_at'],row['absolute_expires_at']):raise IdentityError('preauth_required')
        if csrf is not None and not hmac.compare_digest(row['csrf'],csrf):raise IdentityError('csrf_denied',403)
        return row
    def _failure(self,keys):
        with self.db.transaction(self.owner) as tx:
            now=db_now(tx)
            for key in keys:
                tx.execute('INSERT INTO demo_identity.authentication_limits(key,window_at,failures) VALUES(%s,%s,1) ON CONFLICT(key) DO UPDATE SET failures=CASE WHEN demo_identity.authentication_limits.window_at<=excluded.window_at-interval \'15 minutes\' THEN 1 ELSE demo_identity.authentication_limits.failures+1 END,window_at=CASE WHEN demo_identity.authentication_limits.window_at<=excluded.window_at-interval \'15 minutes\' THEN excluded.window_at ELSE demo_identity.authentication_limits.window_at END',(key,now))


    def _generation_fingerprint(self,secret):
        if not isinstance(secret,str) or not re.fullmatch('[A-Z2-7]{32}',secret):raise IdentityError('invalid_factor_secret',503)
        return hashlib.sha256(b'PF-DI-FACTOR/1\0'+secret.encode('ascii')).hexdigest()


    def _generation_lock(self,tx):
        row=tx.execute('SELECT epoch FROM demo_identity.generation_state WHERE singleton=1 FOR SHARE').fetchone()
        if not row:raise IdentityError('identity_generation_not_ready',503)
        return row['epoch']






    def _limits(self,tx,keys,limits):
        now=db_now(tx)
        for key,limit in zip(keys,limits):
            tx.execute('INSERT INTO demo_identity.authentication_limits(key,window_at,failures) VALUES(%s,%s,0) ON CONFLICT DO NOTHING',(key,now))
            row=tx.execute('SELECT * FROM demo_identity.authentication_limits WHERE key=%s FOR UPDATE',(key,)).fetchone()
            if row and row['window_at']>now-timedelta(minutes=15) and row['failures']>=limit:raise IdentityError('authentication_rate_limited',429)
    def _mint(self,principal_id,kind,auth_strength,*,operation_id=None,expected_identity_revision=None,expected_account_epoch=None):
        if self._closed:raise IdentityError('identity_closed',503)
        credential=secrets.token_urlsafe(32);sid=digest_credential(credential);operation_id=operation_id or str(uuid.uuid4())
        with self.db.transaction(self.owner) as tx:
            self._generation_lock(tx)
            now=db_now(tx);account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',(principal_id,)).fetchone() if principal_id else None
            if account and (not account['active'] or account['merchant_id']!=self.merchant_id):raise IdentityError('authentication_failed')
            if account and (account['kind']!=kind or (expected_identity_revision is not None and account['revision']!=expected_identity_revision) or (expected_account_epoch is not None and account['account_epoch']!=expected_account_epoch)):raise IdentityError('authentication_failed')
            absolute=now+timedelta(hours=24 if kind=='guest' else 4);idle=absolute if kind=='guest' else now+timedelta(minutes=30)
            if kind=='preauth':absolute=idle=now+timedelta(minutes=10)
            tx.execute('INSERT INTO demo_identity.sessions(id,principal_id,kind,csrf,state,session_generation,account_epoch,identity_revision,auth_strength,idle_expires_at,absolute_expires_at,publication_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(sid,principal_id,kind,secrets.token_urlsafe(32),'ACTIVE' if kind=='preauth' else 'PUBLISHING',str(uuid.uuid4()),account['account_epoch'] if account else 0,account['revision'] if account else 1,auth_strength,idle,absolute,operation_id))
            row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s',(sid,)).fetchone()
            if kind!='preauth':self._receipt(tx,'session_publish',operation_id,account,row,True)
        if kind!='preauth':self._publish(sid,operation_id)
        context=self.resolve_session(credential)
        return IdentityResult(self.public_dto(context),credential=credential if kind!='preauth' else None,preauth_credential=credential if kind=='preauth' else None,clear_challenge=True,clear_preauth=kind!='preauth')
    def _publish(self,sid,operation_id):
        if self.authority is None:raise IdentityError('authority_publication_unavailable',503)
        with self.db.transaction(self.owner) as tx:
            row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s',(sid,)).fetchone();account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(row['principal_id'],)).fetchone()
        closed=self.authority.prepare_session_binding(self.merchant_id,account['id'],sid,'sme-web',operation_id)
        self.authority.publish_session_binding(self.merchant_id,operation_id,expected_epochs=closed['expected_epochs'],native_receipt={'receipt_id':operation_id,'operation_id':operation_id})
        with self.db.transaction(self.owner) as tx:
            tx.execute("UPDATE demo_identity.sessions SET state='ACTIVE' WHERE id=%s AND publication_id=%s AND state='PUBLISHING'",(sid,operation_id))
    def _receipt(self,tx,kind,operation_id,account,session,intended_open):
        metadata={'receipt_id':operation_id,'operation_id':operation_id,'merchant_id':self.merchant_id,'principal_id':account['id'],'session_id':session['id'] if session else None,'identity_revision':account['revision'],'account_epoch':account['account_epoch'],'session_epoch':session['epoch'] if session else None,'roles':account['roles'],'auth_strength':session['auth_strength'] if session else 'none','idle_expires_at':session['idle_expires_at'].isoformat() if session else None,'absolute_expires_at':session['absolute_expires_at'].isoformat() if session else None,'intended_open':intended_open}
        signature=self._receipt_mac(metadata)
        tx.execute("INSERT INTO demo_identity.operation_receipts(id,operation_id,principal_id,session_id,kind,metadata,receipt_mac,signature_version) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,'PF-DI-RECEIPT/1') ON CONFLICT DO NOTHING",(operation_id,operation_id,account['id'],session['id'] if session else None,kind,json.dumps(metadata),signature))
        tx.stage_audit(operation_id,{'operation':kind,'principal_id':account['id'],'session_generation':session['session_generation'] if session else None,'identity_revision':account['revision']})
        return metadata
    def _receipt_mac(self,metadata):
        if not callable(self.receipt_key_provider):raise IdentityError('identity_receipt_key_unavailable',503)
        key=self.receipt_key_provider()
        if not isinstance(key,bytes) or len(key)!=32:raise IdentityError('identity_receipt_key_invalid',503)
        body=b'PF-DI-RECEIPT/1\0'+json.dumps(metadata,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
        return hmac.new(key,body,hashlib.sha256).hexdigest()
    def admit_session(self,tx,actor):
        """Supported current native check inside the already AAC-guarded owner tx."""
        if self._closed or not isinstance(actor,dict) or not actor.get('session_id') or not actor.get('id'):raise IdentityError('unauthenticated')
        account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',(actor['id'],)).fetchone()
        row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s FOR SHARE',(actor['session_id'],)).fetchone();now=db_now(tx)
        if not account or not row or not account['active'] or row['state']!='ACTIVE' or row['principal_id']!=account['id'] or account['merchant_id']!=self.merchant_id or row['kind']!=account['kind'] or row['kind']!=actor.get('kind') or row['epoch']!=actor.get('epoch') or row['account_epoch']!=account['account_epoch'] or account['account_epoch']!=actor.get('account_epoch') or row['identity_revision']!=account['revision'] or row['session_generation']!=actor.get('session_generation') or account['roles']!=actor.get('roles') or now>=min(row['idle_expires_at'],row['absolute_expires_at']):raise IdentityError('session_expired')
        if account['kind']=='staff' and row['auth_strength']!='password+totp':raise IdentityError('staff_mfa_required',403)
        return {'id':account['id'],'merchant_id':self.merchant_id,'kind':row['kind'],'roles':list(account['roles']),'session_id':row['id'],'epoch':row['epoch'],'account_epoch':account['account_epoch'],'session_generation':row['session_generation'],'auth_strength':row['auth_strength'],'idle_expires_at':row['idle_expires_at'],'absolute_expires_at':row['absolute_expires_at']}
    def authenticate(self,username,password,*,preauth_credential,csrf,source_key,operation_id=None,old_credential=None):
        if self._closed:raise IdentityError('identity_closed',503)
        if not isinstance(username,str) or not 1<=len(username)<=254 or not isinstance(password,str) or not 1<=len(password)<=1024:raise IdentityError('authentication_failed')
        try:email=canonical_email(username)
        except IdentityError:email=None
        if email is None and not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}',username):raise IdentityError('authentication_failed')
        keys=['account:'+hashlib.sha256((email or username.lower()).encode()).hexdigest(),'source:'+hashlib.sha256(str(source_key).encode()).hexdigest()]
        if not self._slots.acquire(blocking=False):raise IdentityError('authentication_overload',429)
        try:
            with self.db.transaction(self.owner) as tx:
                carrier=self._carrier(tx,preauth_credential,csrf);self._limits(tx,keys,(5,20));account=tx.execute('SELECT * FROM demo_identity.accounts WHERE username=%s AND merchant_id=%s FOR SHARE',(username,self.merchant_id)).fetchone()
                if account is None and email:account=tx.execute('SELECT * FROM demo_identity.accounts WHERE email_canonical=%s AND merchant_id=%s FOR SHARE',(email,self.merchant_id)).fetchone()
                valid=hmac.compare_digest(hash_password(password,bytes(account['password_salt']) if account and account['password_salt'] else self._dummy_salt),bytes(account['password_hash']) if account and account['password_hash'] else self._dummy_hash)
                pending=bool(valid and account and account['kind']=='customer' and account['customer_registration_state']=='pending')
                valid=bool(valid and account and account['active'] and account['kind']!='guest' and not pending)
                if not valid and not pending:
                    now=db_now(tx)
                    for key in keys:
                        tx.execute("UPDATE demo_identity.authentication_limits SET failures=CASE WHEN window_at<=%s-interval '15 minutes' THEN 1 ELSE failures+1 END,window_at=CASE WHEN window_at<=%s-interval '15 minutes' THEN %s ELSE window_at END WHERE key=%s",(now,now,now,key))
                    tx.stage_audit(str(uuid.uuid4()),{'operation':'authentication_failed','outcome':'denied'})
            if pending:raise IdentityError('email_verification_required',403)
            if not valid:raise IdentityError('authentication_failed')
        finally:self._slots.release()
        operation_id=operation_id or str(uuid.uuid4())
        if carrier['kind']=='preauth':
            with self.db.transaction(self.owner) as tx:
                changed=tx.execute("UPDATE demo_identity.sessions SET state='CONSUMED' WHERE id=%s AND state='ACTIVE'",(carrier['id'],))
                if changed.rowcount!=1:raise IdentityError('preauth_required')
        if old_credential and self.transition_capture:self.transition_capture(self.resolve_session(old_credential),account['id'],operation_id)
        if old_credential:self.logout(old_credential,operation_id=str(uuid.uuid5(uuid.UUID(operation_id),'old-session')))
        if account['kind']=='staff':
            # The guest/preauth carrier may have been consumed or revoked during
            # account handoff. A new unprivileged carrier binds the MFA cookie.
            next_preauth=self.bootstrap_preauth()
            challenge_cookie=secrets.token_urlsafe(32);challenge_id=str(uuid.uuid4());challenge_csrf=secrets.token_urlsafe(32)
            with self.db.transaction(self.owner) as tx:
                current=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',(account['id'],)).fetchone()
                if not current['active'] or current['account_epoch']!=account['account_epoch']:raise IdentityError('authentication_failed')
                now=db_now(tx);expires=now+timedelta(minutes=5)
                tx.execute('INSERT INTO demo_identity.preauth_challenges(id,credential_digest,preauth_digest,principal_id,csrf,account_epoch,factor_version,state,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,\'PENDING\',%s)',(challenge_id,digest_credential(challenge_cookie),digest_credential(next_preauth.preauth_credential),account['id'],challenge_csrf,current['account_epoch'],current['factor_version'],expires))
            public=self.public_dto({'kind':'preauth','csrf':challenge_csrf,'epoch':current['account_epoch'],'session_generation':challenge_id,'idle_expires_at':expires,'absolute_expires_at':expires,'challenge':{'id':challenge_id,'expires_at':expires.isoformat(),'state':'mfa_required'}})
            return IdentityResult(public,challenge_credential=challenge_cookie,preauth_credential=next_preauth.preauth_credential,clear_auth=True)
        return self._mint(account['id'],'customer','password',operation_id=operation_id,expected_identity_revision=account['revision'],expected_account_epoch=account['account_epoch'])
    def bootstrap_guest(self,*,preauth_credential,csrf):
        with self.db.transaction(self.owner) as tx:
            carrier=self._carrier(tx,preauth_credential,csrf,allow_guest=False);pid=str(uuid.uuid4())
            tx.execute('INSERT INTO demo_identity.accounts(id,merchant_id,username,kind,roles) VALUES(%s,%s,%s,\'guest\',%s::jsonb)',(pid,self.merchant_id,'guest-'+pid,json.dumps(['guest'])))
            tx.execute("UPDATE demo_identity.sessions SET state='CONSUMED' WHERE id=%s",(carrier['id'],))
        return self._mint(pid,'guest','opaque-guest')
    def bootstrap_preauth(self):return self._mint(None,'preauth','none')
    def close(self):self._closed=True
    def finish_publication(self,operation_id):
        """Recover only an already committed native receipt; mint no credential."""
        canonical_uuid(operation_id)
        with self.db.transaction(self.owner) as tx:
            receipt=tx.execute('SELECT * FROM demo_identity.operation_receipts WHERE operation_id=%s',(operation_id,)).fetchone()
        if not receipt:raise IdentityError('not_found',404)
        metadata=receipt['metadata']
        if metadata['intended_open']:
            self._publish(metadata['session_id'],operation_id)
            return {'operation_id':operation_id,'state':'PUBLISHED'}
        sessions=[metadata['session_id']] if metadata['session_id'] else []
        closed=self.authority.prepare_principal_change(self.merchant_id,metadata['principal_id'],sessions,operation_id) if metadata['session_id'] is None else self.authority.prepare_change(self.merchant_id,[('session',metadata['session_id'])],operation_id)
        self.authority.publish_session_binding(self.merchant_id,operation_id,expected_epochs=closed['expected_epochs'],native_receipt={'receipt_id':operation_id,'operation_id':operation_id})
        return {'operation_id':operation_id,'state':'PUBLISHED_CLOSED'}
    def initialize(self):
        for version,ddl in self.MIGRATIONS:self.db.verify_migration(self.owner,version,ddl,allow_newer=version!=self.MIGRATIONS[-1][0])
        return {'ready':not self._closed,'profile':self.profile}


    def inspect_operation(self,operation_id):
        with self.db.transaction(self.owner) as tx:
            row=tx.execute('SELECT id,kind,metadata FROM demo_identity.operation_receipts WHERE operation_id=%s',(operation_id,)).fetchone()
            return dict(row) if row else None
    def invalidate_account(self,principal_id,*,reason,operation_id=None,expected_identity_revision=None):
        canonical_uuid(principal_id);operation_id=operation_id or str(uuid.uuid4())
        if not isinstance(reason,str) or not 1<=len(reason)<=300:raise IdentityError('invalidation_reason_required',422)
        with self.db.transaction(self.owner) as tx:
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(principal_id,)).fetchone();sessions=tx.execute("SELECT id FROM demo_identity.sessions WHERE principal_id=%s AND state IN ('ACTIVE','PUBLISHING')",(principal_id,)).fetchall()
        if not account or account['merchant_id']!=self.merchant_id:raise IdentityError('not_found',404)
        if expected_identity_revision is not None and account['revision']!=expected_identity_revision:raise IdentityError('identity_version_conflict',409)
        closed=self.authority.prepare_principal_change(self.merchant_id,principal_id,[r['id'] for r in sessions],operation_id)
        with self.db.transaction(self.owner) as tx:
            current=tx.execute('SELECT revision FROM demo_identity.accounts WHERE id=%s FOR UPDATE',(principal_id,)).fetchone()
            if expected_identity_revision is not None and current['revision']!=expected_identity_revision:raise IdentityError('identity_version_conflict',409)
            tx.execute('UPDATE demo_identity.accounts SET active=false,account_epoch=account_epoch+1,revision=revision+1 WHERE id=%s',(principal_id,));tx.execute("UPDATE demo_identity.sessions SET state='REVOKED',epoch=epoch+1 WHERE principal_id=%s",(principal_id,));tx.execute("UPDATE demo_identity.preauth_challenges SET state='REVOKED' WHERE principal_id=%s",(principal_id,));current=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(principal_id,)).fetchone();self._receipt(tx,'account_invalidate',operation_id,current,None,False)
            tx.stage_audit('invalidation:'+operation_id,{'operation':'account_invalidate','principal_id':principal_id,'reason':reason})
        self.authority.publish_session_binding(self.merchant_id,operation_id,expected_epochs=closed['expected_epochs'],native_receipt={'receipt_id':operation_id,'operation_id':operation_id})
        return {'state':'PUBLISHED_CLOSED','operation_id':operation_id,'expected_epochs':closed['expected_epochs']}
    def logout(self,credential=None,*,operation_id=None,preauth_credential=None,challenge_credential=None):
        credential=credential or preauth_credential;operation_id=operation_id or str(uuid.uuid4())
        if not credential:
            if challenge_credential:
                with self.db.transaction(self.owner) as tx:tx.execute("UPDATE demo_identity.preauth_challenges SET state='REVOKED' WHERE credential_digest=%s",(digest_credential(challenge_credential),))
            return IdentityResult({'state':'logged_out'},clear_auth=True,clear_challenge=True,clear_preauth=True)
        sid=digest_credential(credential)
        with self.db.transaction(self.owner) as tx:row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s',(sid,)).fetchone()
        if not row:return IdentityResult({'state':'logged_out'},clear_auth=True,clear_challenge=True,clear_preauth=True)
        if row['kind']=='preauth':
            with self.db.transaction(self.owner) as tx:
                tx.execute("UPDATE demo_identity.sessions SET state='REVOKED',epoch=epoch+1 WHERE id=%s",(sid,))
                tx.execute("UPDATE demo_identity.preauth_challenges SET state='REVOKED' WHERE preauth_digest=%s",(sid,))
            return IdentityResult({'state':'logged_out'},clear_auth=True,clear_challenge=True,clear_preauth=True)
        closed=self.authority.prepare_change(self.merchant_id,[('session',sid)],operation_id)
        with self.db.transaction(self.owner) as tx:
            row=tx.execute("UPDATE demo_identity.sessions SET state='REVOKED',epoch=epoch+1 WHERE id=%s RETURNING *",(sid,)).fetchone();account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(row['principal_id'],)).fetchone();self._receipt(tx,'logout',operation_id,account,row,False)
        self.authority.publish_session_binding(self.merchant_id,operation_id,expected_epochs=closed['expected_epochs'],native_receipt={'receipt_id':operation_id,'operation_id':operation_id})
        return IdentityResult({'state':'logged_out'},clear_auth=True,clear_challenge=True,clear_preauth=True)
    def migrate(self):
        for version,ddl in self.MIGRATIONS:self.db.migrate(self.owner,version,ddl)
    def provision_account(self,*,principal_id,username,roles,password,factor_ref=None,factor_version=0,expected_identity_revision=0,operation_id=None):
        principal_id=canonical_uuid(principal_id)
        try:reserved_email=canonical_email(username)
        except IdentityError:reserved_email=None
        if type(expected_identity_revision) is not int or expected_identity_revision<0 or type(factor_version) is not int or factor_version<0:raise IdentityError('invalid_identity_revision',422)
        if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}',username):raise IdentityError('invalid_username',422)
        if not isinstance(roles,list) or not roles or len(roles)!=len(set(roles)) or not (set(roles)=={'customer'} or set(roles)<=STAFF_ROLES):raise IdentityError('invalid_roles',422)
        if not isinstance(password,str) or not 24<=len(password)<=1024:raise IdentityError('password_policy',422)
        kind='customer' if roles==['customer'] else 'staff'
        if kind=='staff' and (not factor_ref or type(factor_version) is not int or factor_version<1):raise IdentityError('factor_required',422)
        if kind=='customer' and (factor_ref is not None or factor_version!=0):raise IdentityError('factor_not_permitted',422)
        factor_fingerprint=self._generation_fingerprint(self.secret_provider.resolve(factor_ref)) if kind=='staff' else None
        salt=secrets.token_bytes(16);hashed=hash_password(password,salt);operation_id=operation_id or str(uuid.uuid4())
        with self.db.transaction(self.owner) as tx:
            self._generation_lock(tx)
            old=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(principal_id,)).fetchone()
        if old and old['kind']=='guest':raise IdentityError('guest_credential_reprovision_forbidden',403)
        if old and (old['merchant_id']!=self.merchant_id or old['username']!=username or old['revision']!=expected_identity_revision):raise IdentityError('identity_version_conflict',409)
        if not old and expected_identity_revision!=0:raise IdentityError('identity_version_conflict',409)
        invalidated=None
        if old:
            if factor_ref!=old['factor_ref'] and factor_version<=old['factor_version']:raise IdentityError('factor_version_conflict',409)
            if factor_ref==old['factor_ref'] and factor_version!=old['factor_version']:raise IdentityError('factor_version_conflict',409)
            self.invalidate_account(principal_id,reason='private credential rotation',operation_id=operation_id,expected_identity_revision=old['revision'])
            invalidated=self.inspect_operation(operation_id)['metadata']
        with self.db.transaction(self.owner) as tx:
            if reserved_email:
                tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('identity-email:'+reserved_email,))
                claimed=tx.execute('SELECT id FROM demo_identity.accounts WHERE email_canonical=%s AND id<>%s',(reserved_email,principal_id)).fetchone()
                if claimed:raise IdentityError('identity_version_conflict',409)
            current=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR UPDATE',(principal_id,)).fetchone()
            if current:
                if old is None or invalidated is None or current['active'] or current['revision']!=invalidated['identity_revision'] or current['account_epoch']!=invalidated['account_epoch']:raise IdentityError('identity_version_conflict',409)
                if factor_ref!=old['factor_ref'] and factor_version<=old['factor_version']:raise IdentityError('factor_version_conflict',409)
                if factor_ref==old['factor_ref'] and factor_version!=old['factor_version']:raise IdentityError('factor_version_conflict',409)
                tx.execute('UPDATE demo_identity.accounts SET password_salt=%s,password_hash=%s,password_version=password_version+1,roles=%s::jsonb,kind=%s,factor_ref=%s,factor_version=%s,factor_fingerprint=%s,active=true,revision=revision+1 WHERE id=%s',(salt,hashed,json.dumps(roles),kind,factor_ref,factor_version,factor_fingerprint,principal_id))
            else:tx.execute('INSERT INTO demo_identity.accounts(id,merchant_id,username,kind,roles,password_salt,password_hash,factor_ref,factor_version,factor_fingerprint) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)',(principal_id,self.merchant_id,username,kind,json.dumps(roles),salt,hashed,factor_ref,factor_version,factor_fingerprint))
            if kind=='staff':tx.execute('INSERT INTO demo_identity.factor_counters(principal_id,factor_version) VALUES(%s,%s) ON CONFLICT(principal_id) DO UPDATE SET factor_version=excluded.factor_version,last_accepted_counter=CASE WHEN demo_identity.factor_counters.factor_version=excluded.factor_version THEN demo_identity.factor_counters.last_accepted_counter ELSE -1 END',(principal_id,factor_version))
            current=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(principal_id,)).fetchone();self._receipt(tx,'provision',str(uuid.uuid4()),current,None,False)
        return {'principal_id':principal_id,'revision':current['revision'],'epoch':current['account_epoch'],'outcome':'provisioned'}
    def public_dto(self,context):
        actor=None
        if context.get('kind') not in ('preauth',None):actor={'id':context['id'],'kind':context['kind'],'roles':context['roles']}
        if actor and context.get('kind')=='customer' and context.get('email'):actor.update(full_name=context['full_name'],email=context['email'])
        return {'profile':self.profile,'csrf_token':context['csrf'],'actor':actor,'epoch':context['epoch'],'session_generation':context['session_generation'],'idle_expires_at':iso(context['idle_expires_at']),'absolute_expires_at':iso(context['absolute_expires_at']),'challenge':context.get('challenge')}
    def readiness(self):
        try:self.initialize();return {'ready':self.authority is not None and not self._closed,'profile':self.profile}
        except Exception:return {'ready':False,'reason':'identity_schema_or_binding_unavailable'}


    def resolve_challenge(self,challenge_credential):
        with self.db.transaction(self.owner) as tx:
            row=tx.execute('SELECT * FROM demo_identity.preauth_challenges WHERE credential_digest=%s',(digest_credential(challenge_credential),)).fetchone()
            if not row or row['state']!='PENDING' or db_now(tx)>=row['expires_at']:raise IdentityError('challenge_expired')
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',(row['principal_id'],)).fetchone()
            if not account or not account['active'] or account['kind']!='staff' or account['account_epoch']!=row['account_epoch'] or account['factor_version']!=row['factor_version'] or row['failures']>=5:raise IdentityError('challenge_expired')
            return {'kind':'preauth','csrf':row['csrf'],'epoch':row['account_epoch'],'session_generation':row['id'],'idle_expires_at':row['expires_at'],'absolute_expires_at':row['expires_at'],'challenge':{'id':row['id'],'expires_at':iso(row['expires_at']),'state':'mfa_required'}}
    def resolve_session(self,credential):
        with self.db.transaction(self.owner) as tx:
            row=tx.execute('SELECT s.*,a.roles,a.active,a.account_epoch AS current_account_epoch,a.revision AS current_revision,a.kind AS account_kind,a.full_name,a.email_canonical,a.customer_registration_state FROM demo_identity.sessions s LEFT JOIN demo_identity.accounts a ON a.id=s.principal_id WHERE s.id=%s',(digest_credential(credential),)).fetchone();now=db_now(tx)
            if not row or row['state']!='ACTIVE' or now>=min(row['idle_expires_at'],row['absolute_expires_at']):raise IdentityError('session_expired')
            if row['kind']!='preauth' and (not row['active'] or row['current_account_epoch']!=row['account_epoch'] or row['current_revision']!=row['identity_revision'] or row['account_kind']!=row['kind']):raise IdentityError('session_expired')
            return {'id':row['principal_id'],'merchant_id':self.merchant_id,'kind':row['kind'],'roles':row['roles'] or [],'session_id':row['id'],'epoch':row['epoch'],'account_epoch':row['account_epoch'],'session_generation':row['session_generation'],'csrf':row['csrf'],'auth_strength':row['auth_strength'],'idle_expires_at':row['idle_expires_at'],'absolute_expires_at':row['absolute_expires_at'],**({'full_name':row['full_name'],'email':row['email_canonical']} if row['kind']=='customer' and row['customer_registration_state']=='verified' else {})}
    def touch(self,credential,activity_id):
        canonical_uuid(activity_id);context=self.resolve_session(credential)
        if context['kind'] in ('guest','preauth'):return IdentityResult(self.public_dto(context))
        with self.db.transaction(self.owner) as tx:
            prior=tx.execute('SELECT kind,session_id FROM demo_identity.operation_receipts WHERE operation_id=%s',(activity_id,)).fetchone()
            if prior:
                if prior['kind']!='session_touch' or prior['session_id']!=context['session_id']:raise IdentityError('activity_identity_conflict',409)
                return IdentityResult(self.public_dto(context))
            row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s',(context['session_id'],)).fetchone();now=db_now(tx)
            if now-row['last_activity']<timedelta(minutes=1):return IdentityResult(self.public_dto(context))
        self.authority.prepare_change(self.merchant_id,[('session',context['session_id'])],activity_id)
        with self.db.transaction(self.owner) as tx:
            row=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s FOR UPDATE',(context['session_id'],)).fetchone();now=db_now(tx)
            if row['state']!='ACTIVE' or row['epoch']!=context['epoch'] or now>=min(row['idle_expires_at'],row['absolute_expires_at']):raise IdentityError('session_expired')
            row=tx.execute("UPDATE demo_identity.sessions SET idle_expires_at=least(absolute_expires_at,%s),last_activity=%s,epoch=epoch+1,state='PUBLISHING',publication_id=%s WHERE id=%s RETURNING *",(now+timedelta(minutes=30),now,activity_id,context['session_id'])).fetchone();account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(row['principal_id'],)).fetchone();self._receipt(tx,'session_touch',activity_id,account,row,True)
        self._publish(context['session_id'],activity_id)
        return IdentityResult(self.public_dto(self.resolve_session(credential)))


    def verify_publication_receipt(self,tx,receipt):
        if not isinstance(receipt,dict) or set(receipt)!={'receipt_id','operation_id'}:raise IdentityError('invalid_identity_receipt',403)
        row=tx.execute('SELECT * FROM demo_identity.operation_receipts WHERE id=%s AND operation_id=%s',(receipt['receipt_id'],receipt['operation_id'])).fetchone()
        if not row:raise IdentityError('invalid_identity_receipt',403)
        if row['signature_version']!='PF-DI-RECEIPT/1' or not isinstance(row['receipt_mac'],str) or not hmac.compare_digest(row['receipt_mac'],self._receipt_mac(row['metadata'])):raise IdentityError('invalid_identity_receipt_signature',403)
        metadata=dict(row['metadata']);account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',(metadata['principal_id'],)).fetchone()
        if not account or account['merchant_id']!=self.merchant_id or account['revision']!=metadata['identity_revision'] or account['account_epoch']!=metadata['account_epoch']:raise IdentityError('stale_identity_receipt',403)
        if metadata['session_id']:
            session=tx.execute('SELECT * FROM demo_identity.sessions WHERE id=%s FOR SHARE',(metadata['session_id'],)).fetchone()
            if not session or session['epoch']!=metadata['session_epoch'] or session['account_epoch']!=account['account_epoch']:raise IdentityError('stale_identity_receipt',403)
            if metadata['intended_open'] and (session['state'] not in ('PUBLISHING','ACTIVE') or not account['active'] or db_now(tx)>=min(session['idle_expires_at'],session['absolute_expires_at'])):raise IdentityError('expired_identity_receipt',403)
            if not metadata['intended_open'] and session['state']!='REVOKED':raise IdentityError('invalid_identity_receipt',403)
        elif metadata['intended_open'] or tx.execute("SELECT 1 FROM demo_identity.sessions WHERE principal_id=%s AND state IN ('ACTIVE','PUBLISHING') LIMIT 1",(account['id'],)).fetchone():raise IdentityError('invalid_terminal_identity_receipt',403)
        if metadata['roles']!=account['roles']:raise IdentityError('stale_identity_roles',403)
        return metadata
    def verify_staff_totp(self,challenge_id,code,*,challenge_credential,preauth_credential,csrf,operation_id=None):
        if self._closed:raise IdentityError('identity_closed',503)
        canonical_uuid(challenge_id)
        if not isinstance(code,str) or len(code)!=6 or not code.isascii() or not code.isdigit():raise IdentityError('invalid_code',422)
        with self.db.transaction(self.owner) as tx:
            challenge=tx.execute('SELECT * FROM demo_identity.preauth_challenges WHERE id=%s',(challenge_id,)).fetchone()
            if not challenge:raise IdentityError('authentication_failed')
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s',(challenge['principal_id'],)).fetchone()
        secret=self.secret_provider.resolve(account['factor_ref']);accepted=False;denied='authentication_failed'
        if account.get('factor_fingerprint') is not None and not hmac.compare_digest(account['factor_fingerprint'],self._generation_fingerprint(secret)):raise IdentityError('factor_binding_changed',403)
        import pyotp
        with self.db.transaction(self.owner) as tx:
            account=tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR UPDATE',(account['id'],)).fetchone();factor=tx.execute('SELECT * FROM demo_identity.factor_counters WHERE principal_id=%s FOR UPDATE',(account['id'],)).fetchone();challenge=tx.execute('SELECT * FROM demo_identity.preauth_challenges WHERE id=%s FOR UPDATE',(challenge_id,)).fetchone();now=db_now(tx)
            self._carrier(tx,preauth_credential,allow_guest=False)
            self._limits(tx,['otp:'+account['id']],(10,))
            valid=challenge['state']=='PENDING' and now<challenge['expires_at'] and challenge['failures']<5 and account['active'] and account['kind']=='staff' and factor is not None and factor['factor_version']==account['factor_version'] and account['account_epoch']==challenge['account_epoch'] and account['factor_version']==challenge['factor_version'] and hmac.compare_digest(challenge['credential_digest'],digest_credential(challenge_credential)) and hmac.compare_digest(challenge['preauth_digest'],digest_credential(preauth_credential)) and hmac.compare_digest(challenge['csrf'],csrf)
            counter=int(now.timestamp())//30
            matched=next((n for n in (counter-1,counter,counter+1) if hmac.compare_digest(pyotp.TOTP(secret).at(n*30),code)),None) if valid else None
            if matched is not None and matched>factor['last_accepted_counter']:
                tx.execute('UPDATE demo_identity.factor_counters SET last_accepted_counter=%s WHERE principal_id=%s',(matched,account['id']));tx.execute("UPDATE demo_identity.preauth_challenges SET state='CONSUMED' WHERE id=%s",(challenge_id,));accepted=True
            else:
                tx.execute("UPDATE demo_identity.preauth_challenges SET failures=failures+1,state=CASE WHEN failures+1>=5 THEN 'REVOKED' ELSE state END WHERE id=%s",(challenge_id,))
                tx.execute("UPDATE demo_identity.authentication_limits SET failures=CASE WHEN window_at<=%s-interval '15 minutes' THEN 1 ELSE failures+1 END,window_at=CASE WHEN window_at<=%s-interval '15 minutes' THEN %s ELSE window_at END WHERE key=%s",(now,now,now,'otp:'+account['id']))
        if not accepted:raise IdentityError('invalid_code',422) if valid and challenge['failures']+1<5 else IdentityError(denied)
        return self._mint(account['id'],'staff','password+totp',operation_id=operation_id,expected_identity_revision=account['revision'],expected_account_epoch=account['account_epoch'])

class SecretFileProvider:
    def __init__(self,totp_root):self.totp_root=Path(totp_root).resolve()
    def resolve(self,reference):
        if not isinstance(reference,str) or not reference.startswith('file:'):raise IdentityError('secret_reference_required',503)
        path=Path(reference[5:])
        if not path.is_absolute() or '..' in path.parts or self.totp_root not in path.resolve().parents:raise IdentityError('secret_reference_denied',503)
        value=read_protected_file(path).strip()
        if not re.fullmatch(r'[A-Z2-7]{32}',value):raise IdentityError('invalid_factor_secret',503)
        return value

def canonical_uuid(value):
    try:
        if str(uuid.UUID(value))!=value:raise ValueError()
        return value
    except (ValueError,TypeError,AttributeError):raise IdentityError('invalid_uuid',422) from None

def db_now(tx):return tx.execute('SELECT clock_timestamp() now').fetchone()['now']

def digest_credential(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value):raise IdentityError('session_expired')
    return hashlib.sha256(value.encode('ascii')).hexdigest()

def hash_password(value,salt):return hashlib.scrypt(value.encode('utf-8'),salt=salt,n=32768,r=8,p=1,maxmem=64*1024*1024,dklen=32)

def iso(value):return value.isoformat() if isinstance(value,datetime) else value

def read_protected_file(path):
    path=Path(path)
    if not path.is_absolute():raise IdentityError('protected_secret_file_required',503)
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current=current/part
        if current.is_symlink():raise IdentityError('secret_symlink_denied',503)
    try:fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    except OSError:raise IdentityError('protected_secret_file_required',503) from None
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o600 or st.st_uid!=os.getuid() or st.st_size>4096:raise IdentityError('protected_secret_file_required',503)
        try:return os.read(fd,4097).decode('utf-8')
        except UnicodeError:raise IdentityError('protected_secret_file_required',503) from None
    finally:os.close(fd)
