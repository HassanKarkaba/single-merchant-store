"""Identity-owned intents atomically enqueue encrypted mail for bounded SMTP retry."""
from __future__ import annotations
from datetime import datetime,timezone
import hashlib
import json
import re
import time
import uuid
from cryptography.fernet import Fernet,InvalidToken
from app.models.persistence.service import OwnerTransaction
from .templates import render_account_mail
from .smtp_delivery import MailDeliveryError,send_account_mail

MAIL_DDL="""CREATE TABLE IF NOT EXISTS notifications.identity_mail(
 id text PRIMARY KEY,purpose text NOT NULL,token_digest text NOT NULL,expires_at timestamptz NOT NULL,
 request_digest text NOT NULL,ciphertext bytea NOT NULL,state text NOT NULL DEFAULT 'QUEUED',
 attempts integer NOT NULL DEFAULT 0,retryable boolean NOT NULL DEFAULT true,last_error text,
 next_at timestamptz NOT NULL DEFAULT clock_timestamp(),lease_token text,lease_until timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),accepted_at timestamptz,
 CHECK(state IN ('QUEUED','SENDING','LOCAL_CAPTURED','SMTP_ACCEPTED','DELIVERY_FAILED','CANCELED')));
CREATE INDEX IF NOT EXISTS identity_mail_dispatch ON notifications.identity_mail(state,next_at);"""


class IdentityMailService:
    def __init__(self,db,config=None,*,key_provider=None,intent_guard=None,worker_guard=None):
        self.db,self.config,self.key_provider=db,config,key_provider
        self.intent_guard,self.worker_guard=intent_guard,worker_guard

    def _tx(self,tx,owners):
        if not isinstance(tx,OwnerTransaction) or tx._persistence is not self.db or tx.owner not in owners or not tx.active:raise PermissionError('native_mail_transaction_required')

    def _cipher(self):
        if not self.config or not callable(self.key_provider):raise ValueError('account_mail_not_configured')
        key=self.key_provider()
        try:return Fernet(key.encode('ascii') if isinstance(key,str) else key)
        except (ValueError,TypeError):raise ValueError('protected_mail_queue_key_required') from None

    def _intent(self,tx,payload):
        if not callable(self.intent_guard):raise PermissionError('native_identity_mail_guard_required')
        expires=datetime.fromisoformat(payload['expires_at']) if isinstance(payload['expires_at'],str) else payload['expires_at']
        expected={key:payload[key] for key in ('message_id','purpose','recipient')}
        expected.update(token_digest=hashlib.sha256((payload['purpose']+':'+payload['token']).encode()).hexdigest(),expires_at=expires)
        actual=self.intent_guard(tx,**expected)
        if type(actual) is not dict or actual.get('state')!='CURRENT' or any(actual.get(key)!=value for key,value in expected.items()):raise PermissionError('native_identity_mail_guard_denied')
        return True

    def _worker(self,tx,worker_token):
        if not callable(self.worker_guard):raise PermissionError('native_mail_worker_guard_required')
        proof=self.worker_guard(tx,worker_token)
        if type(proof) is not dict or proof.get('state')!='CURRENT' or proof.get('worker_token')!=worker_token:raise PermissionError('native_mail_worker_guard_denied')
        if type(proof.get('lease_remaining_ms')) is not int or not 0<proof['lease_remaining_ms']<=5000:raise PermissionError('native_mail_worker_deadline_required')
        return proof

    def stage_identity_mail(self,tx,*,message_id,purpose,recipient,token,expires_at,full_name):
        self._tx(tx,{'demo_identity'})
        if not isinstance(message_id,str) or not re.fullmatch('[A-Za-z0-9_-]{8,128}',message_id) or purpose not in ('email_verification','password_reset') or not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43}',token):raise ValueError('invalid_account_mail_intent')
        if not isinstance(recipient,str) or len(recipient)>254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+",recipient) or not isinstance(full_name,str) or len(full_name)>120 or any(c in full_name for c in '\r\n') or not isinstance(expires_at,datetime) or expires_at.tzinfo is None:raise ValueError('invalid_account_mail_intent')
        payload={'message_id':message_id,'purpose':purpose,'recipient':recipient,'token':token,'expires_at':expires_at.astimezone(timezone.utc).isoformat(),'full_name':full_name}
        self._intent(tx,payload);tx.add_precommit(lambda current:self._intent(current,payload))
        schema=tx.execute("SELECT version FROM commerce_schema_registry WHERE owner='notifications' FOR SHARE").fetchone()
        if not schema or schema['version']<3:raise ValueError('account_mail_schema_not_ready')
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        payload['created_at']=tx.execute('SELECT clock_timestamp() AS now').fetchone()['now'].astimezone(timezone.utc).isoformat()
        ciphertext=self._cipher().encrypt(json.dumps(payload,separators=(',',':')).encode())
        row=tx.execute("INSERT INTO notifications.identity_mail(id,purpose,token_digest,expires_at,request_digest,ciphertext) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET id=excluded.id RETURNING request_digest,state",(message_id,purpose,hashlib.sha256((purpose+':'+token).encode()).hexdigest(),expires_at,digest,ciphertext)).fetchone()
        if row['request_digest']!=digest:raise ValueError('account_mail_idempotency_conflict')
        return {'message_id':message_id,'state':row['state']}

    def status(self,tx,*,message_id):
        self._tx(tx,{'demo_identity'})
        row=tx.execute('SELECT state,attempts,retryable FROM notifications.identity_mail WHERE id=%s',(message_id,)).fetchone()
        if not row:raise ValueError('account_mail_not_found')
        return {'message_id':message_id,'state':'QUEUED' if row['state']=='SENDING' else row['state'],'attempts':row['attempts'],'retryable':row['retryable'] and row['state'] not in ('LOCAL_CAPTURED','SMTP_ACCEPTED','CANCELED')}

    def run_once(self,*,worker_token,limit=1):
        if not self.config:return {'processed':0}
        if limit!=1:raise ValueError('bounded_account_mail_batch_required')
        proof={}
        def guard(tx):
            nonlocal proof
            proof=self._worker(tx,worker_token);return True
        with self.db.transaction('notifications',authority_guard=guard) as tx:
            row=tx.execute("SELECT * FROM notifications.identity_mail WHERE ((state IN ('QUEUED','DELIVERY_FAILED') AND retryable=true) OR (state='SENDING' AND lease_until<=clock_timestamp())) AND next_at<=clock_timestamp() ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
            if not row:return {'processed':0}
            identity=row['id'];fence=str(uuid.uuid4())
            if row['attempts']>=self.config['max_attempts']:
                tx.execute("UPDATE notifications.identity_mail SET state='DELIVERY_FAILED',retryable=false,last_error='smtp_attempt_limit',lease_token=NULL,lease_until=NULL WHERE id=%s",(identity,))
                return {'processed':1,'state':'DELIVERY_FAILED','retryable':False}
            tx.execute("UPDATE notifications.identity_mail SET state='SENDING',lease_token=%s,lease_until=clock_timestamp()+interval '15 seconds',attempts=attempts+1 WHERE id=%s",(fence,identity))
            encrypted=bytes(row['ciphertext']);attempts=row['attempts']+1
        state='DELIVERY_FAILED';code=None;retryable=False;payload=None
        try:
            payload=json.loads(self._cipher().decrypt(encrypted))
            if type(payload) is not dict or set(payload)!={'message_id','purpose','recipient','token','expires_at','full_name','created_at'} or payload['message_id']!=identity or payload['purpose']!=row['purpose'] or datetime.fromisoformat(payload['expires_at'])!=row['expires_at'] or hashlib.sha256((payload['purpose']+':'+payload['token']).encode()).hexdigest()!=row['token_digest']:raise ValueError('payload_binding')
            bound={key:value for key,value in payload.items() if key!='created_at'}
            if hashlib.sha256(json.dumps(bound,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=row['request_digest']:raise ValueError('payload_binding')
        except (InvalidToken,ValueError,UnicodeError,TypeError,KeyError):payload=None;code='mail_payload_unavailable'
        # Keep the exact native release, process lease and credential SHARE
        # fences until bounded SMTP and its owner receipt finish. Revocation or
        # recovery hold therefore linearizes before admission or after delivery.
        with self.db.transaction('notifications',authority_guard=guard) as tx:
            current=tx.execute('SELECT state,lease_token,lease_until>clock_timestamp() AS active FROM notifications.identity_mail WHERE id=%s FOR UPDATE',(identity,)).fetchone()
            if not current or current['state']!='SENDING' or current['lease_token']!=fence or not current['active']:raise PermissionError('account_mail_delivery_lease_lost')
            if payload is not None:
                try:self._intent(tx,payload)
                except (PermissionError,ValueError):state='CANCELED'
                if state!='CANCELED':
                    proof=self._worker(tx,worker_token)
                    deadline=time.monotonic()+min(self.config['timeout_seconds'],max(0,proof['lease_remaining_ms']/1000-.1))
                    try:
                        state=send_account_mail(render_account_mail(payload,self.config),self.config,deadline=deadline)
                    except MailDeliveryError as error:code=error.code;retryable=error.retryable and attempts<self.config['max_attempts']
            tx.execute("UPDATE notifications.identity_mail SET state=%s,retryable=%s,last_error=%s,next_at=clock_timestamp()+(%s*interval '1 second'),accepted_at=CASE WHEN %s IN ('LOCAL_CAPTURED','SMTP_ACCEPTED') THEN clock_timestamp() ELSE accepted_at END,lease_token=NULL,lease_until=NULL WHERE id=%s",(state,retryable,code,min(300,2**min(attempts,8)),state,identity))
        return {'processed':1,'state':state,'retryable':retryable}
