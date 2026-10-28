from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets


class AuthorityDenied(PermissionError):
    """Current HOST_BOUND_PG admission failed; no protected effect is admitted."""
    pass

class HostBoundPgAuthority:
    """Owning Pg authority gate/epoch bridge, additive to original VAR-01.

    Native Identity/AAC writers must call prepare_change before native mutation
    and publish_change only after a verified current native receipt. Direct
    writers and unqualified policy evaluators keep live readiness blocked.
    """
    DDL = '''
    CREATE TABLE IF NOT EXISTS commerce_authority_gates(
      merchant_id text NOT NULL,kind text NOT NULL CHECK(kind IN ('policy','principal','session','service')),
      subject_id text NOT NULL,principal_id text,epoch bigint NOT NULL DEFAULT 1 CHECK(epoch>0),
      is_open boolean NOT NULL DEFAULT false,revision text NOT NULL,expires_at timestamptz,
      operation_id text,data jsonb NOT NULL DEFAULT '{}'::jsonb,
      PRIMARY KEY(merchant_id,kind,subject_id));
    CREATE TABLE IF NOT EXISTS commerce_authority_operations(
      merchant_id text NOT NULL,operation_id text NOT NULL,state text NOT NULL
      CHECK(state IN ('PREPARED','CLOSED','PUBLISHED')),scopes jsonb NOT NULL,
      expected_epochs jsonb NOT NULL DEFAULT '{}'::jsonb,receipt_digest text,new_revision text,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(merchant_id,operation_id));
    '''
    PF_DDL = '''CREATE TABLE IF NOT EXISTS commerce_workload_credentials(
      merchant_id text NOT NULL,machine_principal_id text NOT NULL,service_id text NOT NULL,
      credential_ref text NOT NULL,credential_hash text NOT NULL,credential_epoch bigint NOT NULL CHECK(credential_epoch>0),
      issued_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,revoked boolean NOT NULL DEFAULT false,
      version bigint NOT NULL CHECK(version>0),PRIMARY KEY(merchant_id,credential_ref),
      CHECK(expires_at>issued_at AND expires_at<=issued_at+interval '90 days'));'''
    ORDER = {'policy':0,'principal':1,'session':2,'service':3}

    def __init__(self, persistence, *, receipt_verifier=None, publication_verifier=None, workload_secret_provider=None, obligation_verifier=None):
        self.persistence, self.receipt_verifier = persistence, receipt_verifier
        self.publication_verifier,self.workload_secret_provider,self.obligation_verifier=publication_verifier,workload_secret_provider,obligation_verifier

    def _identity(self, value):
        if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value):
            raise AuthorityDenied('INVALID_AUTHORITY_IDENTITY')
        return value

    def _scopes(self, scopes):
        if not isinstance(scopes,(list,tuple)) or not scopes or len(scopes)>1024:
            raise AuthorityDenied('INVALID_AUTHORITY_SCOPES')
        result=[]
        for scope in scopes:
            if not isinstance(scope,(list,tuple)) or len(scope)!=2 or scope[0] not in self.ORDER:
                raise AuthorityDenied('INVALID_AUTHORITY_SCOPE')
            result.append((scope[0],self._identity(scope[1])))
        if len(set(result))!=len(result):
            raise AuthorityDenied('DUPLICATE_AUTHORITY_SCOPE')
        return sorted(result,key=lambda item:(self.ORDER[item[0]],item[1]))

    def guard(self, *, merchant_id, principal_id, session_id, service_id, policy_evaluator, action='write', expected_epochs=None, guest=False):
        return PgAuthorityGuard(merchant_id=merchant_id,principal_id=principal_id,session_id=session_id,service_id=service_id,policy_evaluator=policy_evaluator,action=action,expected_epochs=expected_epochs,guest=guest)

    def initialize(self):
        self.persistence.verify_migration('authority',1,self.DDL,allow_newer=True)
        return self.persistence.verify_migration('authority',2,self.PF_DDL)



    def migrate(self):
        self.persistence.migrate('authority',1,self.DDL)
        self.persistence.migrate('authority',2,self.PF_DDL)


    def prepare_change(self, merchant_id, scopes, operation_id, *, admission_guard=None):
        """Persist PREPARED independently, then close all gates in fixed order.

        Only a returned CLOSED receipt authorizes the caller's native change.
        An interrupted attempt resumes its exact operation and cannot fence or
        reopen a successor operation.
        """
        merchant_id,operation_id=self._identity(merchant_id),self._identity(operation_id)
        scopes=self._scopes(scopes)
        encoded=json.dumps(scopes,separators=(',',':'))
        with self.persistence.transaction('authority') as tx:
            tx.execute('INSERT INTO commerce_authority_operations(merchant_id,operation_id,state,scopes) VALUES (%s,%s,\'PREPARED\',%s::jsonb) ON CONFLICT DO NOTHING',(merchant_id,operation_id,encoded))
            op=tx.execute('SELECT * FROM commerce_authority_operations WHERE merchant_id=%s AND operation_id=%s FOR UPDATE',(merchant_id,operation_id)).fetchone()
            if op['scopes']!=[list(s) for s in scopes]:
                raise AuthorityDenied('BRIDGE_OPERATION_IDENTITY_CONFLICT')
        with self.persistence.transaction('authority') as tx:
            if admission_guard is not None:
                if not callable(admission_guard) or admission_guard(tx) is not True:raise AuthorityDenied('NATIVE_CHANGE_ADMISSION_REQUIRED')
                tx.add_precommit(admission_guard)
            op=tx.execute('SELECT * FROM commerce_authority_operations WHERE merchant_id=%s AND operation_id=%s FOR UPDATE',(merchant_id,operation_id)).fetchone()
            if op['state'] in ('CLOSED','PUBLISHED'):
                return {'merchant_id':merchant_id,'operation_id':operation_id,'state':op['state'],'expected_epochs':op['expected_epochs']}
            epochs={}
            for kind,subject_id in scopes:
                row=tx.execute('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR UPDATE',(merchant_id,kind,subject_id)).fetchone()
                if row is None:
                    raise AuthorityDenied('AUTHORITY_SCOPE_MISSING')
                if row['operation_id'] is not None:
                    current=tx.execute('SELECT state FROM commerce_authority_operations WHERE merchant_id=%s AND operation_id=%s',(merchant_id,row['operation_id'])).fetchone()
                    if current is None or current['state']!='PUBLISHED':
                        raise AuthorityDenied('PRIOR_AUTHORITY_CHANGE_UNRESOLVED')
                epoch=row['epoch']+1
                tx.execute('UPDATE commerce_authority_gates SET is_open=false,epoch=%s,operation_id=%s WHERE merchant_id=%s AND kind=%s AND subject_id=%s',(epoch,operation_id,merchant_id,kind,subject_id))
                epochs[kind+':'+subject_id]=epoch
            tx.execute('UPDATE commerce_authority_operations SET state=\'CLOSED\',expected_epochs=%s::jsonb WHERE merchant_id=%s AND operation_id=%s',(json.dumps(epochs,sort_keys=True),merchant_id,operation_id))
        return {'merchant_id':merchant_id,'operation_id':operation_id,'state':'CLOSED','expected_epochs':epochs}



    def prepare_principal_change(self, merchant_id, principal_id, session_ids, operation_id, *, admission_guard=None):
        for value in (merchant_id,principal_id,operation_id,*session_ids):self._identity(value)
        with self.persistence.transaction('authority') as tx:
            for kind,subject in [('principal',principal_id),*[('session',sid) for sid in session_ids]]:
                tx.execute("INSERT INTO commerce_authority_gates(merchant_id,kind,subject_id,principal_id,is_open,revision) VALUES(%s,%s,%s,%s,false,'0') ON CONFLICT DO NOTHING",(merchant_id,kind,subject,principal_id))
            previous=tx.execute('SELECT scopes FROM commerce_authority_operations WHERE merchant_id=%s AND operation_id=%s',(merchant_id,operation_id)).fetchone()
        scopes=previous['scopes'] if previous else [('principal',principal_id),*[('session',sid) for sid in session_ids]]
        return self.prepare_change(merchant_id,scopes,operation_id,admission_guard=admission_guard)

    def prepare_session_binding(self, merchant_id, principal_id, session_id, service_id, operation_id):
        for value in (merchant_id,principal_id,session_id,service_id,operation_id):self._identity(value)
        scopes=[]
        with self.persistence.transaction('authority') as tx:
            for kind,subject,principal in [('policy',merchant_id,None),('principal',principal_id,principal_id),('session',session_id,principal_id),('service',service_id,None)]:
                # Policy/service creation selects the immutable PF policy profile;
                # it grants no person access before verified principal/session publication.
                tx.execute('INSERT INTO commerce_authority_gates(merchant_id,kind,subject_id,principal_id,is_open,revision,data) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING',(merchant_id,kind,subject,principal,kind in ('policy','service'),'pf-policy-1' if kind in ('policy','service') else '0',json.dumps({'profile':'portfolio-demo','policy_revision':'pf-policy-1'} if kind in ('policy','service') else {})))
                row=tx.execute('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR SHARE',(merchant_id,kind,subject)).fetchone()
                if kind=='session' or (kind=='principal' and not row['is_open']):scopes.append((kind,subject))
            previous=tx.execute('SELECT scopes FROM commerce_authority_operations WHERE merchant_id=%s AND operation_id=%s',(merchant_id,operation_id)).fetchone()
            if previous:scopes=previous['scopes']
        return self.prepare_change(merchant_id,scopes,operation_id)


    def provision(self, *, merchant_id, principal_id, session_id, service_id, expires_at, revision, guest=False, data=None, policy_snapshot=None):
        """Trusted bootstrap inserts only. Never updates/reopens existing gates."""
        for value in (merchant_id,principal_id,session_id,service_id,revision):
            self._identity(value)
        if not isinstance(expires_at,datetime) or expires_at.tzinfo is None or expires_at<=datetime.now(timezone.utc):
            raise AuthorityDenied('CURRENT_SESSION_EXPIRY_REQUIRED')
        if type(guest) is not bool or not isinstance(data or {},dict) or not isinstance(policy_snapshot or {},dict):
            raise AuthorityDenied('INVALID_AUTHORITY_BOOTSTRAP')
        rows=[('policy',merchant_id,None,None,policy_snapshot or {}),('principal',principal_id,principal_id,None,data or {}),('session',session_id,principal_id,expires_at,{'guest':guest}),('service',service_id,None,None,{})]
        with self.persistence.transaction('authority') as tx:
            for kind,subject_id,principal,expiry,payload in rows:
                tx.execute('INSERT INTO commerce_authority_gates(merchant_id,kind,subject_id,principal_id,is_open,revision,expires_at,data) VALUES (%s,%s,%s,%s,true,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING',(merchant_id,kind,subject_id,principal,revision,expiry,json.dumps(payload,allow_nan=False)))

    def provision_workload(self, merchant_id, machine_principal_id, service_id, credential_ref, *, issued_at, expires_at, expected_revision=0):
        for value in (merchant_id,machine_principal_id,service_id):self._identity(value)
        if not isinstance(credential_ref,str) or not 1<=len(credential_ref)<=256 or any(ord(c)<32 for c in credential_ref):raise AuthorityDenied('INVALID_WORKLOAD_SECRET_REFERENCE')
        if self.workload_secret_provider is None:raise AuthorityDenied('WORKLOAD_SECRET_PROVIDER_REQUIRED')
        secret=self.workload_secret_provider(credential_ref)
        if not isinstance(secret,bytes) or len(secret)!=32:raise AuthorityDenied('WORKLOAD_CREDENTIAL_256_BITS_REQUIRED')
        if type(expected_revision) is not int or expected_revision<0 or not isinstance(issued_at,datetime) or not isinstance(expires_at,datetime) or issued_at.tzinfo is None or expires_at.tzinfo is None or not issued_at<expires_at or (expires_at-issued_at).total_seconds()>90*86400:raise AuthorityDenied('FINITE_WORKLOAD_LIFETIME_REQUIRED')
        with self.persistence.transaction('authority') as tx:
            now=tx.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            if issued_at>now or expires_at<=now:raise AuthorityDenied('CURRENT_WORKLOAD_LIFETIME_REQUIRED')
            for kind,subject in [('policy',merchant_id),('principal',machine_principal_id),('service',service_id)]:
                tx.execute('INSERT INTO commerce_authority_gates(merchant_id,kind,subject_id,principal_id,is_open,revision,data) VALUES(%s,%s,%s,%s,true,\'pf-policy-1\',%s::jsonb) ON CONFLICT DO NOTHING',(merchant_id,kind,subject,machine_principal_id if kind=='principal' else None,json.dumps({'profile':'portfolio-demo','machine':kind=='principal','policy_revision':'pf-policy-1'})))
                tx.execute('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR UPDATE',(merchant_id,kind,subject)).fetchone()
            previous=tx.execute('SELECT * FROM commerce_workload_credentials WHERE merchant_id=%s AND credential_ref=%s FOR UPDATE',(merchant_id,credential_ref)).fetchone()
            if (previous['version'] if previous else 0)!=expected_revision:raise AuthorityDenied('WORKLOAD_REVISION_CONFLICT')
            epoch=(previous['credential_epoch'] if previous else 0)+1
            tx.execute('INSERT INTO commerce_workload_credentials(merchant_id,machine_principal_id,service_id,credential_ref,credential_hash,credential_epoch,issued_at,expires_at,revoked,version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,false,%s) ON CONFLICT(merchant_id,credential_ref) DO UPDATE SET machine_principal_id=EXCLUDED.machine_principal_id,service_id=EXCLUDED.service_id,credential_hash=EXCLUDED.credential_hash,credential_epoch=EXCLUDED.credential_epoch,issued_at=EXCLUDED.issued_at,expires_at=EXCLUDED.expires_at,revoked=false,version=EXCLUDED.version',(merchant_id,machine_principal_id,service_id,credential_ref,hashlib.sha256(secret).hexdigest(),epoch,issued_at,expires_at,expected_revision+1))
            for kind,subject in [('principal',machine_principal_id),('service',service_id)]:
                tx.execute("UPDATE commerce_authority_gates SET is_open=true,epoch=epoch+1,revision=%s,data=data||%s::jsonb WHERE merchant_id=%s AND kind=%s AND subject_id=%s",(str(expected_revision+1),json.dumps({'machine':True}),merchant_id,kind,subject))
            tx.stage_audit('workload:'+credential_ref+':'+str(epoch),{'operation':'workload_provision','machine_principal_id':machine_principal_id,'credential_epoch':epoch,'expires_at':expires_at.isoformat()})
        return self.workload_epochs(merchant_id,machine_principal_id,service_id,credential_ref)


    def publish_change(self, merchant_id, operation_id, *, native_receipt, revision, open_gate=True):
        merchant_id,operation_id,revision=self._identity(merchant_id),self._identity(operation_id),self._identity(revision)
        if type(open_gate) is not bool:
            raise AuthorityDenied('INVALID_GATE_PUBLICATION')
        # Verify outside the protected transaction: verifier may consult native
        # authority. Publication's Pg CAS still checks the exact closed operation.
        if self.receipt_verifier is None or self.receipt_verifier(merchant_id,operation_id,native_receipt,revision) is not True:
            raise AuthorityDenied('CURRENT_NATIVE_RECEIPT_REQUIRED')
        encoded=json.dumps(native_receipt,sort_keys=True,separators=(',',':'),allow_nan=False)
        receipt_digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        with self.persistence.transaction('authority') as tx:
            op=tx.execute('SELECT * FROM commerce_authority_operations WHERE merchant_id=%s AND operation_id=%s FOR UPDATE',(merchant_id,operation_id)).fetchone()
            if op is None or op['state'] not in ('CLOSED','PUBLISHED'):
                raise AuthorityDenied('BRIDGE_NOT_CLOSED')
            if op['state']=='PUBLISHED':
                if op['receipt_digest']!=receipt_digest or op['new_revision']!=revision:
                    raise AuthorityDenied('PUBLICATION_IDENTITY_CONFLICT')
                return {'state':'PUBLISHED','operation_id':operation_id,'replayed':True}
            for kind,subject_id in self._scopes(op['scopes']):
                row=tx.execute('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR UPDATE',(merchant_id,kind,subject_id)).fetchone()
                expected=op['expected_epochs'][kind+':'+subject_id]
                if row is None or row['epoch']!=expected or row['operation_id']!=operation_id or row['is_open']:
                    raise AuthorityDenied('STALE_AUTHORITY_PUBLICATION')
                tx.execute('UPDATE commerce_authority_gates SET is_open=%s,revision=%s WHERE merchant_id=%s AND kind=%s AND subject_id=%s AND epoch=%s AND operation_id=%s',(open_gate,revision,merchant_id,kind,subject_id,expected,operation_id))
            tx.execute('UPDATE commerce_authority_operations SET state=\'PUBLISHED\',receipt_digest=%s,new_revision=%s WHERE merchant_id=%s AND operation_id=%s',(receipt_digest,revision,merchant_id,operation_id))
        return {'state':'PUBLISHED','operation_id':operation_id,'replayed':False}


    def publish_session_binding(self, merchant_id, operation_id, *, expected_epochs, native_receipt):
        for value in (merchant_id,operation_id):self._identity(value)
        if self.publication_verifier is None:raise AuthorityDenied('CURRENT_IDENTITY_PUBLICATION_VERIFIER_REQUIRED')
        with self.persistence.transaction('authority') as tx:
            verified=self.publication_verifier(tx,native_receipt)
            fields={'receipt_id','operation_id','merchant_id','principal_id','session_id','identity_revision','account_epoch','session_epoch','roles','auth_strength','idle_expires_at','absolute_expires_at','intended_open'}
            if not isinstance(verified,dict) or set(verified)!=fields or verified['merchant_id']!=merchant_id or verified['operation_id']!=operation_id or type(verified['intended_open']) is not bool:raise AuthorityDenied('INVALID_IDENTITY_PUBLICATION_RECEIPT')
            roles=verified['roles'];staff_roles=PortfolioDemoPolicy.STAFF_ROLES
            if not isinstance(roles,list) or not roles or len(set(roles))!=len(roles) or not set(roles)<={'guest','customer',*staff_roles} or (set(roles)&staff_roles and set(roles)&{'guest','customer'}):raise AuthorityDenied('INVALID_IDENTITY_ROLES')
            if set(roles)&staff_roles and verified['intended_open'] and verified['auth_strength']!='password+totp':raise AuthorityDenied('STAFF_MFA_REQUIRED')
            if any(type(verified[k]) is not int or verified[k]<1 for k in ('identity_revision','account_epoch')):raise AuthorityDenied('CURRENT_IDENTITY_REVISION_REQUIRED')
            opened=verified['intended_open'];expiry=None
            if opened:
                if not verified['session_id'] or type(verified['session_epoch']) is not int or verified['session_epoch']<1:raise AuthorityDenied('CURRENT_SESSION_REQUIRED')
                try:expiry=min(datetime.fromisoformat(verified['idle_expires_at']),datetime.fromisoformat(verified['absolute_expires_at']))
                except Exception:raise AuthorityDenied('SESSION_EXPIRY_REQUIRED') from None
                now=tx.execute('SELECT clock_timestamp() AS now').fetchone()['now']
                if expiry.tzinfo is None or expiry<=now:raise AuthorityDenied('SESSION_EXPIRED')
            op=tx.execute('SELECT * FROM commerce_authority_operations WHERE merchant_id=%s AND operation_id=%s FOR UPDATE',(merchant_id,operation_id)).fetchone()
            digest_value=hashlib.sha256(json.dumps(verified,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if op is None or op['state'] not in ('CLOSED','PUBLISHED') or op['expected_epochs']!=expected_epochs:raise AuthorityDenied('PUBLICATION_OPERATION_EPOCH_MISMATCH')
            if op['state']=='PUBLISHED':
                if op['receipt_digest']!=digest_value:raise AuthorityDenied('PUBLICATION_IDENTITY_CONFLICT')
                return {'state':'PUBLISHED','operation_id':operation_id,'replayed':True}
            for kind,subject in self._scopes(op['scopes']):
                row=tx.execute('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR UPDATE',(merchant_id,kind,subject)).fetchone()
                if row is None or row['is_open'] or row['epoch']!=expected_epochs[kind+':'+subject] or row['operation_id']!=operation_id:raise AuthorityDenied('STALE_AUTHORITY_PUBLICATION')
                if kind=='principal' and subject!=verified['principal_id']:raise AuthorityDenied('PUBLICATION_PRINCIPAL_MISMATCH')
                if kind=='session' and verified['session_id'] is not None and subject!=verified['session_id']:raise AuthorityDenied('PUBLICATION_SESSION_MISMATCH')
                data={'roles':roles,'account_epoch':verified['account_epoch'],'identity_revision':verified['identity_revision'],'profile':'portfolio-demo'}
                if kind=='session':data.update(guest=roles==['guest'],session_epoch=verified['session_epoch'],auth_strength=verified['auth_strength'])
                tx.execute('UPDATE commerce_authority_gates SET is_open=%s,revision=%s,expires_at=%s,data=%s::jsonb WHERE merchant_id=%s AND kind=%s AND subject_id=%s',(opened,str(verified['identity_revision']),expiry if kind=='session' else None,json.dumps(data),merchant_id,kind,subject))
            # Existing open principal metadata cannot be silently replaced by login.
            principal=tx.execute("SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind='principal' AND subject_id=%s FOR SHARE",(merchant_id,verified['principal_id'])).fetchone()
            if opened and (principal is None or not principal['is_open'] or principal['data'].get('roles')!=roles or principal['data'].get('account_epoch')!=verified['account_epoch']):raise AuthorityDenied('PRINCIPAL_PUBLICATION_REQUIRED')
            tx.execute("UPDATE commerce_authority_operations SET state='PUBLISHED',receipt_digest=%s,new_revision=%s WHERE merchant_id=%s AND operation_id=%s",(digest_value,str(verified['identity_revision']),merchant_id,operation_id))
            tx.stage_audit('publication:'+operation_id,{'operation':'session_publication','principal_id':verified['principal_id'],'session_id':verified['session_id'],'identity_revision':verified['identity_revision'],'intended_open':opened})
        return {'state':'PUBLISHED','operation_id':operation_id,'replayed':False}
    def renew_expired_workload(self, merchant_id, machine_principal_id, service_id, credential_ref, *, lifetime_days=30):
        """Renew only an expired, still-authorized existing machine credential.

        Ordinary local startup may call this private owner port. It never
        creates credentials, changes their secret or identity, or opens gates.
        Concurrent startups observe one renewal; current credentials keep their
        original expiry. Explicit revocation/closed gates always require an
        operator's separate supported provisioning decision.
        """
        for value in (merchant_id,machine_principal_id,service_id):self._identity(value)
        if not isinstance(credential_ref,str) or not 1<=len(credential_ref)<=256 or any(ord(c)<32 for c in credential_ref):raise AuthorityDenied('INVALID_WORKLOAD_SECRET_REFERENCE')
        if type(lifetime_days) is not int or not 1<=lifetime_days<=90:raise AuthorityDenied('FINITE_WORKLOAD_LIFETIME_REQUIRED')
        if not callable(self.workload_secret_provider):raise AuthorityDenied('WORKLOAD_SECRET_PROVIDER_REQUIRED')
        secret=self.workload_secret_provider(credential_ref)
        if not isinstance(secret,bytes) or len(secret)!=32:raise AuthorityDenied('WORKLOAD_CREDENTIAL_256_BITS_REQUIRED')
        digest=hashlib.sha256(secret).hexdigest()
        with self.persistence.transaction('authority') as tx:
            epochs={}
            for kind,subject in [('policy',merchant_id),('principal',machine_principal_id),('service',service_id)]:
                gate=tx.execute('SELECT *, (expires_at IS NULL OR expires_at>clock_timestamp()) AS current_time_valid FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR UPDATE',(merchant_id,kind,subject)).fetchone()
                if gate is None or gate['is_open'] is not True or gate['current_time_valid'] is not True:raise AuthorityDenied('WORKLOAD_GATE_CLOSED_OR_EXPIRED')
                if kind=='principal' and (gate['principal_id']!=machine_principal_id or gate['data'].get('machine') is not True):raise AuthorityDenied('WORKLOAD_IDENTITY_BINDING_CHANGED')
                epochs[kind]=gate['epoch']
            row=tx.execute('SELECT * FROM commerce_workload_credentials WHERE merchant_id=%s AND credential_ref=%s FOR UPDATE',(merchant_id,credential_ref)).fetchone()
            now=tx.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            if (row is None or row['revoked'] is not False or row['machine_principal_id']!=machine_principal_id or row['service_id']!=service_id or row['issued_at']>now or not secrets.compare_digest(row['credential_hash'],digest)):raise AuthorityDenied('WORKLOAD_RENEWAL_REJECTED')
            state='CURRENT'
            if row['expires_at']<=now:
                renewed=tx.execute('UPDATE commerce_workload_credentials SET issued_at=%s,expires_at=%s,credential_epoch=credential_epoch+1,version=version+1 WHERE merchant_id=%s AND credential_ref=%s AND version=%s AND revoked=false AND expires_at<=%s RETURNING *',(now,now+timedelta(days=lifetime_days),merchant_id,credential_ref,row['version'],now)).fetchone()
                if renewed is None:raise AuthorityDenied('WORKLOAD_REVISION_CONFLICT')
                row=renewed;state='RENEWED'
                tx.stage_audit('workload-renew:'+hashlib.sha256((merchant_id+'\0'+credential_ref).encode('utf-8')).hexdigest()+':'+str(row['version']),{'operation':'workload_expiry_renewal','machine_principal_id':machine_principal_id,'credential_epoch':row['credential_epoch'],'version':row['version'],'expires_at':row['expires_at'].isoformat()})
            epochs['credential']=row['credential_epoch']
            return {'state':state,'version':row['version'],'expected_epochs':epochs,'issued_at':row['issued_at'].isoformat(),'expires_at':row['expires_at'].isoformat()}

    def revoke_workload(self, merchant_id, machine_principal_id, service_id, credential_ref, *, expected_revision):
        with self.persistence.transaction('authority') as tx:
            for kind,subject in [('policy',merchant_id),('principal',machine_principal_id),('service',service_id)]:
                row=tx.execute('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR UPDATE',(merchant_id,kind,subject)).fetchone()
                if row is None:raise AuthorityDenied('WORKLOAD_GATE_MISSING')
            row=tx.execute('SELECT * FROM commerce_workload_credentials WHERE merchant_id=%s AND credential_ref=%s FOR UPDATE',(merchant_id,credential_ref)).fetchone()
            if row is None or row['version']!=expected_revision or row['machine_principal_id']!=machine_principal_id or row['service_id']!=service_id:raise AuthorityDenied('WORKLOAD_REVISION_CONFLICT')
            tx.execute('UPDATE commerce_workload_credentials SET revoked=true,credential_epoch=credential_epoch+1,version=version+1 WHERE merchant_id=%s AND credential_ref=%s',(merchant_id,credential_ref))
            tx.execute("UPDATE commerce_authority_gates SET is_open=false,epoch=epoch+1 WHERE merchant_id=%s AND kind='principal' AND subject_id=%s",(merchant_id,machine_principal_id))
            tx.stage_audit('workload-revoke:'+credential_ref+':'+str(expected_revision+1),{'operation':'workload_revoke','machine_principal_id':machine_principal_id})
        return {'state':'REVOKED','version':expected_revision+1}
    def workload_epochs(self, merchant_id, machine_principal_id, service_id, credential_ref):
        result={}
        with self.persistence.transaction('authority') as tx:
            for kind,subject in [('policy',merchant_id),('principal',machine_principal_id),('service',service_id)]:
                row=tx.execute('SELECT epoch FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s',(merchant_id,kind,subject)).fetchone()
                if row is None:raise AuthorityDenied('WORKLOAD_GATE_MISSING')
                result[kind]=row['epoch']
            credential=tx.execute('SELECT credential_epoch,version FROM commerce_workload_credentials WHERE merchant_id=%s AND credential_ref=%s AND machine_principal_id=%s AND service_id=%s',(merchant_id,credential_ref,machine_principal_id,service_id)).fetchone()
            if credential is None:raise AuthorityDenied('WORKLOAD_CREDENTIAL_MISSING')
            result['credential']=credential['credential_epoch']
        return {'expected_epochs':result,'version':credential['version']}
    def workload_guard(self, *, merchant_id, machine_principal_id, service_id, credential_ref, operation, resource_id, obligation_id, expected_epochs):
        return PgWorkloadGuard(merchant_id=merchant_id,machine_principal_id=machine_principal_id,service_id=service_id,credential_ref=credential_ref,operation=operation,resource_id=resource_id,obligation_id=obligation_id,expected_epochs=expected_epochs,secret_provider=self.workload_secret_provider,obligation_verifier=self.obligation_verifier)

class PgAuthorityGuard:
    """Current shared authority row locks held by the exact owner transaction.

    policy_evaluator is a trusted supported native evaluator adapter; absent or
    nonliteral-True decisions fail closed. No browser authority fields are read.
    A deterministic callback is test evidence only, never native policy parity.
    """
    def __init__(self, *, merchant_id, principal_id, session_id, service_id, policy_evaluator=None, action='write', expected_epochs=None, guest=False):
        self.merchant_id,self.principal_id,self.session_id,self.service_id=merchant_id,principal_id,session_id,service_id
        for value in (merchant_id,principal_id,session_id,service_id,action):
            if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value):
                raise AuthorityDenied('INVALID_AUTHORITY_CONTEXT')
        if type(guest) is not bool or (policy_evaluator is not None and not callable(policy_evaluator)):
            raise AuthorityDenied('INVALID_AUTHORITY_BINDING')
        self.action,self.policy_evaluator,self.guest=action,policy_evaluator,guest
        self.expected_epochs=dict(expected_epochs or {})

    def admit(self, tx):
        snapshots={}
        scopes=(('policy',self.merchant_id),('principal',self.principal_id),('session',self.session_id),('service',self.service_id))
        for kind,subject_id in scopes:
            row=tx.execute('SELECT *, (expires_at IS NULL OR expires_at>clock_timestamp()) AS current_time_valid FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR SHARE',(self.merchant_id,kind,subject_id)).fetchone()
            if row is None or row['is_open'] is not True or row['current_time_valid'] is not True:
                raise AuthorityDenied('AUTHORITY_GATE_CLOSED_OR_EXPIRED')
            if kind in ('principal','session') and row['principal_id']!=self.principal_id:
                raise AuthorityDenied('SESSION_PRINCIPAL_MISMATCH')
            expected=self.expected_epochs.get(kind)
            if expected is not None and row['epoch']!=expected:
                raise AuthorityDenied('AUTHORITY_EPOCH_CHANGED')
            snapshots[kind]=row
        session=snapshots['session']
        if session['expires_at'] is None or session['data'].get('guest') is not self.guest:
            raise AuthorityDenied('SESSION_CONTEXT_MISMATCH')
        # Guest support requires an explicit native capability decision too.
        if self.policy_evaluator is None or self.policy_evaluator(snapshots,tx.owner,self.action) is not True:
            raise AuthorityDenied('CURRENT_NATIVE_POLICY_ALLOW_REQUIRED')
        return True

class PgWorkloadGuard:
    def __init__(self, *, merchant_id, machine_principal_id, service_id, credential_ref, operation, resource_id, obligation_id, expected_epochs, secret_provider, obligation_verifier):
        self.context={'merchant_id':merchant_id,'machine_principal_id':machine_principal_id,'service_id':service_id,'credential_ref':credential_ref,'operation':operation,'resource_id':resource_id,'obligation_id':obligation_id}
        if any(not isinstance(v,str) or not 1<=len(v)<=256 for v in self.context.values()) or not isinstance(expected_epochs,dict) or set(expected_epochs)!={'policy','principal','service','credential'} or any(type(n)is not int or n<1 for n in expected_epochs.values()):raise AuthorityDenied('EXACT_WORKLOAD_CONTEXT_REQUIRED')
        if not callable(secret_provider) or not callable(obligation_verifier):raise AuthorityDenied('QUALIFIED_WORKLOAD_BINDINGS_REQUIRED')
        self.expected_epochs=dict(expected_epochs);self.obligation_verifier=obligation_verifier
        # Protected credential resolution is outside all Pg authority/owner locks.
        secret=secret_provider(credential_ref)
        if not isinstance(secret,bytes) or len(secret)!=32:raise AuthorityDenied('WORKLOAD_CREDENTIAL_256_BITS_REQUIRED')
        self._credential_digest=hashlib.sha256(secret).hexdigest()

    def admit(self, tx):
        c=self.context;snapshots={}
        for kind,subject in [('policy',c['merchant_id']),('principal',c['machine_principal_id']),('service',c['service_id'])]:
            row=tx.execute('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s FOR SHARE',(c['merchant_id'],kind,subject)).fetchone()
            if row is None or not row['is_open'] or row['epoch']!=self.expected_epochs[kind]:raise AuthorityDenied('WORKLOAD_GATE_CLOSED_OR_STALE')
            snapshots[kind]=row
        credential=tx.execute('SELECT *, issued_at<=clock_timestamp() AND expires_at>clock_timestamp() AS current_time_valid FROM commerce_workload_credentials WHERE merchant_id=%s AND credential_ref=%s FOR SHARE',(c['merchant_id'],c['credential_ref'])).fetchone()
        if credential is None or credential['revoked'] or credential['current_time_valid'] is not True or credential['credential_epoch']!=self.expected_epochs['credential'] or credential['machine_principal_id']!=c['machine_principal_id'] or credential['service_id']!=c['service_id'] or not secrets.compare_digest(credential['credential_hash'],self._credential_digest):raise AuthorityDenied('WORKLOAD_CREDENTIAL_REJECTED')
        if tx.owner!=c['operation'].split('.',1)[0]:raise AuthorityDenied('WORKLOAD_OWNER_OPERATION_MISMATCH')
        receipt=self.obligation_verifier(tx,dict(c))
        if not isinstance(receipt,dict) or any(receipt.get(k)!=c[k] for k in ('merchant_id','machine_principal_id','service_id','resource_id','obligation_id')) or not isinstance(receipt.get('admission_receipt'),str) or not receipt['admission_receipt'] or c['operation'] not in receipt.get('operations',[]):raise AuthorityDenied('ADMITTED_RESOURCE_OBLIGATION_REQUIRED')
        forbidden={'identity.provision_account','catalog.create_product','inventory.adjust_stock','purchase.submit','application.purchase.submit'}
        if c['operation'] in forbidden:raise AuthorityDenied('WORKLOAD_INITIATION_FORBIDDEN')
        return True

class PortfolioDemoPolicy:
    STAFF_ROLES=frozenset({'catalog_editor','stock_operator','support','finance','merchant_admin'})
    OPERATIONS = {
        'application.actor': ('application', frozenset(['guest', 'customer', 'catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'])),
        'application.cancellation.submit': ('application', frozenset(['guest', 'customer', 'support', 'merchant_admin'])),
        'application.fulfillment.submit': ('application', frozenset(['stock_operator', 'merchant_admin'])),
        'application.refund.submit': ('application', frozenset(['finance', 'merchant_admin'])),
        'application.return.manage': ('application', frozenset(['support', 'merchant_admin'])),
        'application.return.submit': ('application', frozenset(['guest', 'customer', 'support', 'merchant_admin'])),
        'application.command': ('application', frozenset(['guest', 'customer', 'catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'])),
        'application.operation': ('application', frozenset(['guest', 'customer', 'catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'])),
        'application.recovery.read': ('application', frozenset(['finance'])),
        'application.seed': ('application', frozenset(['merchant_admin', 'catalog_editor', 'stock_operator'])),
        'browser.command': ('application', frozenset(['guest', 'customer', 'catalog_editor', 'stock_operator', 'support', 'finance'])),
        'browser.read': ('application', frozenset(['guest', 'customer', 'catalog_editor', 'stock_operator', 'support', 'finance'])),
        'cart.capture_guest_handoff': ('cart', frozenset(['guest', 'customer'])),
        'cart.consume_guest_handoff': ('cart', frozenset(['customer', 'catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'])),
        'cart.create_guest_cart': ('cart', frozenset(['guest', 'customer'])),
        'cart.get_cart': ('cart', frozenset(['guest', 'customer'])),
        'cart.get_for_principal': ('cart', frozenset(['customer', 'catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'])),
        'cart.get_or_create_account_cart': ('cart', frozenset(['customer'])),
        'cart.inspect_merge': ('cart', frozenset(['guest', 'customer'])),
        'cart.merge_guest_cart': ('cart', frozenset(['customer', 'catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'])),
        'cart.pending_handoffs': ('cart', frozenset(['customer', 'catalog_editor', 'stock_operator', 'support', 'finance', 'merchant_admin'])),
        'cart.prepare_guest_handoff': ('cart', frozenset(['guest', 'customer'])),
        'cart.remove_line': ('cart', frozenset(['guest', 'customer'])),
        'cart.set_coupon': ('cart', frozenset(['guest', 'customer'])),
        'cart.set_line': ('cart', frozenset(['guest', 'customer'])),
        'catalog.attach_media': ('catalog', frozenset(['catalog_editor'])),
        'catalog.create_product': ('catalog', frozenset(['catalog_editor'])),
        'catalog.get_merchant_product': ('catalog', frozenset(['catalog_editor', 'stock_operator'])),
        'catalog.get_variants': ('catalog', frozenset(['catalog_editor', 'stock_operator'])),
        'catalog.list_merchant_products': ('catalog', frozenset(['catalog_editor', 'stock_operator'])),
        'catalog.publish': ('catalog', frozenset(['catalog_editor'])),
        'catalog.replace_slug': ('catalog', frozenset(['catalog_editor'])),
        'catalog.retire_variant': ('catalog', frozenset(['catalog_editor'])),
        'catalog.set_collection': ('catalog', frozenset(['catalog_editor'])),
        'catalog.unpublish': ('catalog', frozenset(['catalog_editor'])),
        'catalog.update_product': ('catalog', frozenset(['catalog_editor'])),
        'catalog.upsert_variant': ('catalog', frozenset(['catalog_editor'])),
        'checkout.accept_quote': ('checkout', frozenset(['guest', 'customer'])),
        'checkout.begin': ('checkout', frozenset(['guest', 'customer'])),
        'checkout.evaluate': ('checkout', frozenset(['guest', 'customer'])),
        'checkout.get_checkout': ('checkout', frozenset(['guest', 'customer'])),
        'checkout.inspect_purchase_claim': ('checkout', frozenset(['guest', 'customer'])),
        'checkout.revalidate_sellability': ('checkout', frozenset(['guest', 'customer'])),
        'checkout.select_delivery': ('checkout', frozenset(['guest', 'customer'])),
        'checkout.update_contact_and_address': ('checkout', frozenset(['guest', 'customer'])),
        'inventory.adjust_stock': ('inventory', frozenset(['stock_operator'])),
        'inventory.get_availability': ('inventory', frozenset(['guest', 'customer', 'stock_operator', 'catalog_editor'])),
        'inventory.inspect_allocation': ('inventory', frozenset(['guest', 'customer', 'stock_operator', 'catalog_editor'])),
        'inventory.inspect_reservation': ('inventory', frozenset(['guest', 'customer', 'stock_operator', 'catalog_editor'])),
        'inventory.list_movements': ('inventory', frozenset(['stock_operator'])),
        'notifications.get_delivery_status': ('notifications', frozenset(['guest', 'customer', 'support'])),
        'notifications.get_message': ('notifications', frozenset(['guest', 'customer'])),
        'notifications.list_inbox': ('notifications', frozenset(['guest', 'customer'])),
        'operation.read': ('application', frozenset(['guest', 'customer'])),
        'orders.get_order': ('orders', frozenset(['guest', 'customer', 'support', 'stock_operator', 'finance'])),
        'orders.get_receipt': ('orders', frozenset(['guest', 'customer', 'support', 'stock_operator', 'finance'])),
        'orders.get_refund_plan': ('orders', frozenset(['guest', 'customer', 'support', 'stock_operator', 'finance'])),
        'orders.inspect_operation': ('orders', frozenset(['guest', 'customer', 'support', 'stock_operator', 'finance'])),
        'orders.list_customer_orders': ('orders', frozenset(['guest', 'customer', 'support', 'stock_operator', 'finance'])),
        'orders.list_merchant_orders': ('orders', frozenset(['support', 'stock_operator', 'finance'])),
        'orders.merchant_summary': ('orders', frozenset(['support', 'stock_operator', 'finance'])),
        'orders.summarize_merchant_orders': ('orders', frozenset(['support', 'stock_operator', 'finance'])),
        'payments.inspect_attempt': ('payments', frozenset(['finance'])),
        'payments.inspect_receipt': ('payments', frozenset(['finance', 'support'])),
        'payments.inspect_refund': ('payments', frozenset(['finance'])),
        'payments.list_reconciliation_items': ('payments', frozenset(['finance', 'support'])),
        'payments.list_settlements': ('payments', frozenset(['finance'])),
        'payments.reconcile': ('payments', frozenset(['finance'])),
        'pricing.activate_promotion': ('pricing', frozenset(['catalog_editor'])),
        'pricing.create_promotion': ('pricing', frozenset(['catalog_editor'])),
        'pricing.end_promotion': ('pricing', frozenset(['catalog_editor'])),
        'pricing.get_prices': ('pricing', frozenset(['guest', 'customer', 'catalog_editor', 'stock_operator'])),
        'pricing.get_promotion': ('pricing', frozenset(['catalog_editor'])),
        'pricing.get_quote': ('pricing', frozenset(['guest', 'customer'])),
        'pricing.inspect_redemption_hold': ('pricing', frozenset(['guest', 'customer', 'catalog_editor', 'stock_operator'])),
        'pricing.list_promotions': ('pricing', frozenset(['catalog_editor'])),
        'pricing.quote': ('pricing', frozenset(['guest', 'customer'])),
        'pricing.set_price': ('pricing', frozenset(['catalog_editor'])),
        'purchase.read': ('application', frozenset(['guest', 'customer'])),
        'purchase.submit': ('application', frozenset(['guest', 'customer'])),
        'return.request': ('application', frozenset(['guest', 'customer'])),
        'returns.approve_return': ('returns', frozenset(['support'])),
        'returns.close_return': ('returns', frozenset(['support'])),
        'returns.create_exchange_link': ('returns', frozenset(['support'])),
        'returns.get_refund_plan': ('returns', frozenset(['guest', 'customer', 'support', 'finance'])),
        'returns.get_return': ('returns', frozenset(['guest', 'customer', 'support'])),
        'returns.inspect_operation': ('returns', frozenset(['guest', 'customer', 'support'])),
        'returns.inspect_units': ('returns', frozenset(['support'])),
        'returns.list_customer_returns': ('returns', frozenset(['guest', 'customer', 'support'])),
        'returns.list_order_returns': ('returns', frozenset(['guest','customer','support','finance'])),
        'returns.list_returns': ('returns', frozenset(['support'])),
        'returns.receive_return': ('returns', frozenset(['support'])),
        'returns.record_inspection': ('returns', frozenset(['support'])),
        'returns.record_received': ('returns', frozenset(['support'])),
        'returns.reject_return': ('returns', frozenset(['support'])),
        'returns.request_return': ('returns', frozenset(['guest', 'customer', 'support'])),
        'shipping.buy_label': ('shipping', frozenset(['stock_operator'])),
        'shipping.cancel_units': ('shipping', frozenset(['support'])),
        'shipping.claim_units': ('shipping', frozenset(['stock_operator'])),
        'shipping.confirm_handover': ('shipping', frozenset(['stock_operator'])),
        'shipping.create_shipment': ('shipping', frozenset(['stock_operator'])),
        'shipping.get_label': ('shipping', frozenset(['stock_operator'])),
        'shipping.get_offer': ('shipping', frozenset(['guest', 'customer', 'stock_operator'])),
        'shipping.get_quote': ('shipping', frozenset(['guest', 'customer'])),
        'shipping.list_order_shipments': ('shipping', frozenset(['guest','customer','support','stock_operator','finance'])),
        'shipping.get_shipment': ('shipping', frozenset(['stock_operator'])),
        'shipping.get_unit_status': ('shipping', frozenset(['stock_operator', 'support'])),
        'shipping.inspect_cancel': ('shipping', frozenset(['support'])),
        'shipping.purchase_label': ('shipping', frozenset(['stock_operator'])),
        'shipping.quote': ('shipping', frozenset(['guest', 'customer', 'stock_operator'])),
        'shipping.reserve_handover': ('shipping', frozenset(['stock_operator'])),
        'shipping.simulate_tracking': ('shipping', frozenset(['stock_operator'])),
        'tax.calculate': ('tax', frozenset(['guest', 'customer'])),
        'tax.get_calculation': ('tax', frozenset(['guest', 'customer'])),
    }

    @classmethod
    def evaluate(cls, snapshots, owner, action, *, resource_allowed):
        entry=cls.OPERATIONS.get(action)
        if entry is None or entry[0]!=owner or resource_allowed is not True:return False
        if snapshots['policy']['data'].get('policy_revision')!='pf-policy-1':return False
        principal=snapshots['principal']['data'];session=snapshots['session']['data']
        roles=principal.get('roles',[])
        if roles!=session.get('roles') or principal.get('account_epoch')!=session.get('account_epoch'):return False
        if set(roles)&cls.STAFF_ROLES and session.get('auth_strength')!='password+totp':return False
        effective=set(roles)
        if 'merchant_admin' in effective:effective.update({'catalog_editor','stock_operator','support','finance'})
        return bool(effective&entry[1])
