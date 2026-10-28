from __future__ import annotations
import hashlib
import json
import uuid
from typing import Any
from .attempts import PaymentAttemptPolicy
from .effects import MonetaryEffectPolicy

DDL = """
CREATE SCHEMA IF NOT EXISTS payments;
CREATE TABLE IF NOT EXISTS payments.attempts(
 id text PRIMARY KEY, owner_id text NOT NULL, amount_minor bigint NOT NULL CHECK(amount_minor>=0 AND amount_minor<=99999999),
 currency text NOT NULL CHECK(currency='USD'),quote_digest text NOT NULL, state text NOT NULL,
 provider_id text, version bigint NOT NULL DEFAULT 1,captured_minor bigint NOT NULL DEFAULT 0,
 refunded_minor bigint NOT NULL DEFAULT 0,capture_fenced boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS payments.effects(
 id text PRIMARY KEY,attempt_id text NOT NULL REFERENCES payments.attempts(id),kind text NOT NULL,
 request_digest text NOT NULL, amount_minor bigint NOT NULL, state text NOT NULL DEFAULT 'REQUESTED',
 provider_id text,result jsonb, dispatch_token text, admitted_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(attempt_id,kind,id));
CREATE UNIQUE INDEX IF NOT EXISTS one_capture_per_attempt ON payments.effects(attempt_id) WHERE kind='capture';
CREATE TABLE IF NOT EXISTS payments.callback_inbox(id text PRIMARY KEY,digest text NOT NULL,received_at timestamptz DEFAULT clock_timestamp());
"""

class LegacyPaymentService:
    def __init__(self, db: Any, provider: Any, *, authorize: Any):
        self.db, self.provider, self.authorize = db, provider, authorize
        self.attempt_policy=PaymentAttemptPolicy
        self.effect_policy=MonetaryEffectPolicy
    def _effect(self, attempt_id, kind, amount, effect_id, request, *, actor):
        encoded=json.dumps(request,sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.db.transaction('payments') as tx:
            self.authorize(tx,actor,'payments.'+kind,attempt_id)
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone()
            if not row: raise ValueError('not_found')
            if actor['kind'] not in ('service','staff') and row['owner_id']!=actor['id']:raise ValueError('not_found')
            if kind in ('capture','refund') and actor['kind'] not in ('service','staff'):raise PermissionError('forbidden')
            old=tx.execute('SELECT * FROM payments.effects WHERE id=%s FOR UPDATE',(effect_id,)).fetchone()
            if old:
                if old['request_digest']!=digest or old['attempt_id']!=attempt_id or old['kind']!=kind: raise ValueError('idempotency_conflict')
                if old['state'] in ('SUCCEEDED','FAILED') and (old.get('result') or {}).get('state')!='ACTION_REQUIRED': return old['result']
                # A possible send is resolved through lookup; it is never blindly repeated.
                lookup=True
            else:
                if kind=='capture' and (row['capture_fenced'] or row['state'] not in ('AUTHORIZED','ZERO_TOTAL')):raise ValueError('capture_not_admitted')
                if kind=='refund':
                    pending=tx.execute("SELECT COALESCE(sum(amount_minor),0)::bigint n FROM payments.effects WHERE attempt_id=%s AND kind='refund' AND state IN ('REQUESTED','SENDING','UNKNOWN','PENDING')",(attempt_id,)).fetchone()['n']
                    self.effect_policy.reserve_refund(amount,row['captured_minor'],row['refunded_minor'],pending)
                tx.execute('INSERT INTO payments.effects(id,attempt_id,kind,request_digest,amount_minor) VALUES(%s,%s,%s,%s,%s)',(effect_id,attempt_id,kind,digest,amount))
                lookup=False
            effect=tx.execute('SELECT * FROM payments.effects WHERE id=%s',(effect_id,)).fetchone()
            if effect['dispatch_token'] and effect['state']=='SENDING':lookup=True
            if not lookup:
                tx.execute("UPDATE payments.effects SET state='SENDING',dispatch_token=%s,admitted_at=clock_timestamp() WHERE id=%s",(str(uuid.uuid4()),effect_id))
        # Deliberately outside the locked owner transaction.
        try:
            if lookup:
                result=self.provider.inspect_effect('stripe',effect_id)
                if result is None:return self._pending(attempt_id, effect_id)
                if result.get('id') and result.get('status') in ('requires_action','action_required','pending','processing'):
                    result=self.provider.inspect_provider_object('stripe',result['id'])
            else:
                result=self.provider.invoke('stripe',kind,request,effect_id)
        except Exception:
            with self.db.transaction('payments') as tx:
                tx.execute("UPDATE payments.effects SET state='UNKNOWN' WHERE id=%s AND state<>'SUCCEEDED'",(effect_id,))
                tx.execute("UPDATE payments.attempts SET state=CASE WHEN state='CAPTURED' THEN state ELSE 'UNKNOWN' END,version=version+1 WHERE id=%s",(attempt_id,))
            return self._pending(attempt_id,effect_id)
        if not isinstance(result,dict) or result.get('currency')!='USD' or result.get('amount_minor')!=amount:
            return self._pending(attempt_id,effect_id)
        if request.get('object_id') and kind!='refund' and result.get('id')!=request['object_id']:return self._pending(attempt_id,effect_id)
        provider_state=result.get('status')
        provider_state={'requires_capture':'authorized','requires_action':'action_required'}.get(provider_state,provider_state)
        if kind=='capture' and provider_state=='succeeded':provider_state='captured'
        allowed={'create_authorization':{'authorized','declined','canceled'},'capture':{'captured','declined','canceled'},'cancel':{'canceled'},'refund':{'succeeded','declined','canceled'}}
        final=provider_state in allowed.get(kind,set())
        with self.db.transaction('payments') as tx:
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone()
            effect=tx.execute('SELECT * FROM payments.effects WHERE id=%s FOR UPDATE',(effect_id,)).fetchone()
            if effect['state'] in ('SUCCEEDED','FAILED') and (effect.get('result') or {}).get('state')!='ACTION_REQUIRED':return effect['result']
            state={'authorized':'AUTHORIZED','captured':'CAPTURED','canceled':'CANCELED','declined':'DECLINED','action_required':'ACTION_REQUIRED','succeeded':row['state']}.get(provider_state,'UNKNOWN')
            # Confirmed capture is not regressed by late authorisation/cancellation observations.
            if row['state']=='CAPTURED':state='CAPTURED'
            receipt={'owner':'payments','id':attempt_id,'effect_id':effect_id,'provider_id':result.get('id'),'state':state,'amount_minor':amount,'currency':'USD','version':row['version']+1,'effect_certainty':'committed' if final else 'unknown'}
            if kind=='refund':receipt.update(id=effect_id,payment_id=attempt_id,state='SUCCEEDED' if provider_state=='succeeded' else 'FAILED' if final else 'PENDING')
            effect_state='FAILED' if final and provider_state in ('declined','canceled') and kind!='cancel' else 'SUCCEEDED' if final else 'PENDING'
            tx.execute('UPDATE payments.effects SET state=%s,provider_id=%s,result=%s::jsonb WHERE id=%s',(effect_state,result.get('id'),json.dumps(receipt),effect_id))
            if kind=='refund' and provider_state=='succeeded':
                tx.execute('UPDATE payments.attempts SET refunded_minor=refunded_minor+%s,version=version+1 WHERE id=%s',(amount,attempt_id))
            else:
                tx.execute('UPDATE payments.attempts SET state=%s,provider_id=COALESCE(provider_id,%s),captured_minor=CASE WHEN %s THEN amount_minor ELSE captured_minor END,version=version+1 WHERE id=%s',(state,result.get('id'),kind=='capture' and state=='CAPTURED',attempt_id))
            transition_id=effect_id+':'+str(receipt['version'])
            tx.stage_outbox('payments.'+kind,transition_id,receipt)
            tx.stage_audit(transition_id,{'kind':kind,'attempt_id':attempt_id,'state':receipt['state'],'amount_minor':amount})
            return receipt
    def _pending(self, attempt_id,effect_id):
        return {'owner':'payments','id':attempt_id,'effect_id':effect_id,'state':'UNKNOWN','effect_certainty':'unknown'}
    def cancel_authorization(self,attempt_id,*,actor=None):
        with self.db.transaction('payments') as tx:
            self.authorize(tx,actor,'payments.cancel',attempt_id)
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone()
            if not row:raise ValueError('not_found')
            if actor['kind'] not in ('service','staff') and row['owner_id']!=actor['id']:raise ValueError('not_found')
            if row['captured_minor']:raise ValueError('already_captured')
            active=tx.execute("SELECT id FROM payments.effects WHERE attempt_id=%s AND kind='capture' AND state IN ('SENDING','UNKNOWN','PENDING')",(attempt_id,)).fetchone()
            tx.execute('UPDATE payments.attempts SET capture_fenced=true WHERE id=%s',(attempt_id,))
            if active:return self._pending(attempt_id,active['id'])
            if row['amount_minor']==0:
                tx.execute("UPDATE payments.attempts SET state='CANCELED',version=version+1 WHERE id=%s",(attempt_id,))
                return {'owner':'payments','id':attempt_id,'state':'CANCELED','amount_minor':0,'currency':'USD','effect_certainty':'committed'}
        return self._effect(attempt_id,'cancel',row['amount_minor'],attempt_id+':cancel',{'object_id':row['provider_id'],'attempt_id':attempt_id,'amount_minor':row['amount_minor'],'currency':'USD'},actor=actor)
    def capture(self,attempt_id,order_receipt,allocation_receipt,*,actor=None):
        if order_receipt.get('owner')!='orders' or allocation_receipt.get('owner')!='inventory' or allocation_receipt.get('order_id')!=order_receipt['id']:raise ValueError('invalid_participant_receipts')
        with self.db.transaction('payments') as tx:
            self.authorize(tx,actor,'payments.capture',attempt_id)
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s',(attempt_id,)).fetchone()
            if not row:raise ValueError('not_found')
            if actor['kind'] not in ('service','staff'):raise PermissionError('forbidden')
            if row['amount_minor']==0:
                return {'owner':'payments','id':attempt_id,'state':'ZERO_TOTAL','amount_minor':0,'currency':'USD','effect_certainty':'committed'}
        request={'object_id':row['provider_id'],'attempt_id':attempt_id,'amount_minor':row['amount_minor'],'currency':'USD','order_id':order_receipt['id'],'allocation_id':allocation_receipt['id']}
        return self._effect(attempt_id,'capture',row['amount_minor'],attempt_id+':capture',request,actor=actor)
    def create_authorization(self,attempt_id,quote_digest,amount_minor,currency,customer_email,*,actor=None):
        self.attempt_policy.validate_authorization(amount_minor,currency)
        with self.db.transaction('payments') as tx:
            self.authorize(tx,actor,'payments.authorize',attempt_id)
            tx.execute('INSERT INTO payments.attempts(id,owner_id,amount_minor,currency,quote_digest,state) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(attempt_id,actor['id'],amount_minor,currency,quote_digest,'ZERO_TOTAL' if amount_minor==0 else 'REQUESTED'))
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s',(attempt_id,)).fetchone()
            if row['amount_minor']!=amount_minor or row['quote_digest']!=quote_digest or row['owner_id']!=actor['id']:raise ValueError('idempotency_conflict')
            if amount_minor==0:return {'owner':'payments','id':attempt_id,'state':'ZERO_TOTAL','amount_minor':0,'currency':'USD','effect_certainty':'committed'}
        return self._effect(attempt_id,'create_authorization',amount_minor,attempt_id+':authorization',{'attempt_id':attempt_id,'amount_minor':amount_minor,'currency':currency,'capture_method':'manual'},actor=actor)
    def initialize(self):self.db.migrate('payments',1,DDL)
    def inspect_attempt(self,attempt_id,*,actor=None):
        with self.db.transaction('payments') as tx:
            self.authorize(tx,actor,'payments.read',attempt_id)
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s',(attempt_id,)).fetchone()
            if not row or (actor['kind'] not in ('service','staff') and row['owner_id']!=actor['id']):raise ValueError('not_found')
            return dict(row,owner='payments')
    def refund(self,payment_id,refund_request_id,amount_minor,reason,*,actor=None):
        if type(amount_minor) is not int:raise ValueError('invalid_amount')
        row=self.inspect_attempt(payment_id,actor=actor)
        return self._effect(payment_id,'refund',amount_minor,refund_request_id,{'object_id':row['provider_id'],'attempt_id':payment_id,'amount_minor':amount_minor,'currency':'USD','reason':reason},actor=actor)

class PaymentService(LegacyPaymentService):
    PF_DDL='''ALTER TABLE payments.attempts ADD COLUMN IF NOT EXISTS demo_instrument text;
    ALTER TABLE payments.attempts ADD COLUMN IF NOT EXISTS obligation_id text;
    ALTER TABLE payments.attempts ADD COLUMN IF NOT EXISTS order_id text;
    ALTER TABLE payments.attempts ADD COLUMN IF NOT EXISTS allocation_id text;
    ALTER TABLE payments.effects ADD COLUMN IF NOT EXISTS request jsonb;
    ALTER TABLE payments.effects ADD COLUMN IF NOT EXISTS plan_id text;
    CREATE UNIQUE INDEX IF NOT EXISTS one_refund_per_approved_plan ON payments.effects(plan_id) WHERE kind='refund' AND plan_id IS NOT NULL;
    ALTER TABLE payments.attempts ADD CONSTRAINT payment_conservation_pf CHECK(captured_minor>=0 AND captured_minor<=amount_minor AND refunded_minor>=0 AND refunded_minor<=captured_minor);'''

    PF_FENCE_DDL='CREATE TABLE payments.authorization_fences(attempt_id text PRIMARY KEY,receipt jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp())'

    def __init__(self,db,provider,*,authorize,profile='local-sandbox'):
        super().__init__(db,provider,authorize=authorize)
        if profile not in ('local-sandbox','demo-test','portfolio-demo'):raise ValueError('profile_denied')
        self.owner='payments';self.profile=profile;self.admission_port=self.order_port=self.inventory_port=self.approved_plan_port=None
        self.resource_predicate=None

    def _guard(self,actor,operation,resource):
        def admit(tx):
            if self.authorize(tx,actor,operation,resource) is not True:raise PermissionError('forbidden')
            if self.resource_predicate is not None and self.resource_predicate(tx,actor,operation,resource) is not True:raise PermissionError('forbidden')
            row=tx.execute('SELECT owner_id FROM payments.attempts WHERE id=%s FOR SHARE',(resource,)).fetchone()
            if row and actor.get('kind') not in ('staff','recovery') and row['owner_id']!=actor.get('id'):raise ValueError('not_found')
            return True
        return admit

    def _pf_effect(self,attempt_id,kind,amount,effect_id,request,*,actor,plan_id=None,zero_state=None,admission_operation=None):
        digest=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        operation=admission_operation or 'payments.'+kind
        with self.db.transaction('payments',authority_guard=self._guard(actor,operation,attempt_id)) as tx:
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone()
            if row is None:raise ValueError('not_found')
            old=tx.execute('SELECT * FROM payments.effects WHERE id=%s FOR UPDATE',(effect_id,)).fetchone()
            if old:
                if old['attempt_id']!=attempt_id or old['kind']!=kind or old['request_digest']!=digest:raise ValueError('idempotency_conflict')
                if old['state'] in ('SUCCEEDED','FAILED'):return old['result']
                sending=False
            else:
                if kind=='capture' and (row['capture_fenced'] or row['state'] not in ('AUTHORIZED','ZERO_TOTAL')):raise ValueError('capture_not_admitted')
                if kind=='capture':
                    deadline=request.get('capture_deadline')
                    now=tx.execute('SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS now').fetchone()['now']
                    if type(deadline)is not int or now>=deadline:raise ValueError('capture_deadline_expired')
                if kind=='refund' and plan_id and tx.execute('SELECT id FROM payments.effects WHERE plan_id=%s',(plan_id,)).fetchone():raise ValueError('refund_plan_already_claimed')
                if kind=='refund' and amount:
                    pending=tx.execute("SELECT COALESCE(sum(amount_minor),0)::bigint n FROM payments.effects WHERE attempt_id=%s AND kind='refund' AND state NOT IN ('SUCCEEDED','FAILED')",(attempt_id,)).fetchone()['n']
                    self.effect_policy.reserve_refund(amount,row['captured_minor'],row['refunded_minor'],pending)
                tx.execute("INSERT INTO payments.effects(id,attempt_id,kind,request_digest,amount_minor,state,dispatch_token,admitted_at,request,plan_id) VALUES(%s,%s,%s,%s,%s,'SENDING',%s,clock_timestamp(),%s::jsonb,%s)",(effect_id,attempt_id,kind,digest,amount,str(uuid.uuid4()),json.dumps(request),plan_id))
                sending=True
            effect=tx.execute('SELECT * FROM payments.effects WHERE id=%s',(effect_id,)).fetchone()
            if zero_state:return self._record(tx,row,effect,request,zero_state,terminal=True)
        # Only the durable SENDING winner may invoke. Every successor reconciles
        # the original effect/object through scoped supported provider ports.
        try:
            result=self.provider.invoke('stripe',kind,request,effect_id,actor=actor) if sending else self.provider.inspect_effect('stripe',effect_id,actor=actor)
            if result is not None and not sending and result.get('id'):
                result=self.provider.inspect_provider_object('stripe',result['id'],actor=actor)
            if result is None:raise ValueError('provider_outcome_unknown')
            self.attempt_policy.validate_provider_result(result,amount,request.get('object_id') if kind!='refund' else None)
        except Exception as error:
            if getattr(error,'code',None)=='not_applied':
                with self.db.transaction('payments',authority_guard=self._guard(actor,operation,attempt_id)) as tx:
                    row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone();effect=tx.execute('SELECT * FROM payments.effects WHERE id=%s FOR UPDATE',(effect_id,)).fetchone()
                    if effect['state'] in ('SUCCEEDED','FAILED'):return effect['result']
                    return self._record(tx,row,effect,request,'FAILED',terminal=True,failed=True)
            with self.db.transaction('payments',authority_guard=self._guard(actor,operation,attempt_id)) as tx:
                tx.execute("UPDATE payments.effects SET state='UNKNOWN' WHERE id=%s AND state NOT IN ('SUCCEEDED','FAILED')",(effect_id,))
            return {'owner':'payments','id':effect_id if kind=='refund' else attempt_id,'attempt_id':attempt_id,'payment_id':attempt_id,'effect_id':effect_id,'receipt_id':effect_id,'state':'UNKNOWN','effect_certainty':'unknown'}
        status=result.get('status')
        success={'create_authorization':{'requires_capture':'AUTHORIZED','authorized':'AUTHORIZED','declined':'DECLINED','canceled':'CANCELED','requires_action':'ACTION_REQUIRED'},'capture':{'succeeded':'CAPTURED','captured':'CAPTURED','declined':'DECLINED','canceled':'CANCELED'},'cancel':{'canceled':'CANCELED'},'refund':{'succeeded':'SUCCEEDED','declined':'FAILED','canceled':'FAILED'}}
        state=success.get(kind,{}).get(status,'PENDING')
        terminal=state not in ('PENDING','ACTION_REQUIRED')
        with self.db.transaction('payments',authority_guard=self._guard(actor,operation,attempt_id)) as tx:
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone();effect=tx.execute('SELECT * FROM payments.effects WHERE id=%s FOR UPDATE',(effect_id,)).fetchone()
            if effect['state'] in ('SUCCEEDED','FAILED'):return effect['result']
            if effect.get('result') and effect['result'].get('state')==state and effect['provider_id']==result['id']:return effect['result']
            return self._record(tx,row,effect,request,state,provider_id=result['id'],terminal=terminal,failed=state in ('DECLINED','FAILED'))

    def _record(self,tx,row,effect,request,state,*,provider_id=None,terminal=False,failed=False):
        kind=effect['kind'];version=row['version']+1
        receipt={'owner':'payments','id':effect['id'] if kind=='refund' else row['id'],'payment_id':row['id'],'attempt_id':row['id'],'effect_id':effect['id'],'receipt_id':effect['id'],'kind':kind,'state':state,'amount_minor':effect['amount_minor'],'currency':'USD','version':version,'effect_certainty':'committed' if terminal else 'unknown','captured_minor':row['captured_minor'],'capture_fenced':row['capture_fenced']}
        if request.get('order_id'):receipt['order_id']=request['order_id']
        if request.get('allocation_id'):receipt['allocation_id']=request['allocation_id']
        if effect.get('plan_id'):receipt['plan_id']=effect['plan_id']
        if kind=='capture' and state in ('CAPTURED','ZERO_SETTLEMENT'):
            receipt['captured_minor']=effect['amount_minor']
            tx.execute('UPDATE payments.attempts SET captured_minor=%s,state=%s,order_id=%s,allocation_id=%s,version=%s WHERE id=%s',(effect['amount_minor'],state,request['order_id'],request['allocation_id'],version,row['id']))
        elif kind=='refund':
            tx.execute('UPDATE payments.attempts SET refunded_minor=refunded_minor+%s,version=%s WHERE id=%s',(effect['amount_minor'] if state=='SUCCEEDED' else 0,version,row['id']))
        else:
            receipt['no_future_capture']=bool(terminal and state in ('CANCELED','DECLINED','FAILED') and row['capture_fenced'])
            tx.execute('UPDATE payments.attempts SET state=CASE WHEN captured_minor>0 THEN state ELSE %s END,provider_id=COALESCE(provider_id,%s),version=%s WHERE id=%s',(state,provider_id,version,row['id']))
        tx.execute('UPDATE payments.effects SET state=%s,provider_id=COALESCE(%s,provider_id),result=%s::jsonb WHERE id=%s',('FAILED' if failed else 'SUCCEEDED' if terminal else 'PENDING',provider_id,json.dumps(receipt),effect['id']))
        event=str(uuid.uuid5(uuid.NAMESPACE_URL,effect['id']+':'+str(version)))
        tx.stage_outbox('payments.'+kind,event,receipt);tx.stage_audit(event,{'operation':kind,'attempt_id':row['id'],'state':state,'amount_minor':effect['amount_minor']})
        return receipt

    def cancel_authorization(self,attempt_id,*,actor=None):
        if self.profile=='local-sandbox':return super().cancel_authorization(attempt_id,actor=actor)
        if self.admission_port is None:raise ValueError('actual_purchase_admission_required')
        admitted=self.admission_port(attempt_id,actor=actor)
        if not isinstance(admitted,dict) or admitted.get('attempt_id',admitted.get('id'))!=attempt_id or admitted.get('obligation_id')!=actor.get('obligation_id') or not admitted.get('owner_id'):raise ValueError('admission_payment_mismatch')
        with self.db.transaction('payments',authority_guard=self._guard(actor,'payments.cancel',attempt_id)) as tx:
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('payment-attempt:'+attempt_id,))
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone()
            if row is None:
                receipt={'owner':'payments','id':attempt_id,'attempt_id':attempt_id,'state':'NOT_CREATED','capture_fenced':True,'no_future_capture':True,'captured_minor':0,'amount_minor':0,'currency':'USD','effect_certainty':'committed','receipt_id':self.effect_id(attempt_id,'fence')}
                saved=tx.execute('INSERT INTO payments.authorization_fences(attempt_id,receipt) VALUES(%s,%s::jsonb) ON CONFLICT(attempt_id) DO UPDATE SET receipt=payments.authorization_fences.receipt RETURNING receipt',(attempt_id,json.dumps(receipt))).fetchone()['receipt']
                tx.stage_audit(saved['receipt_id'],{'operation':'capture_fence','attempt_id':attempt_id,'state':'NOT_CREATED'})
                return saved
            tx.execute('UPDATE payments.attempts SET capture_fenced=true WHERE id=%s',(attempt_id,));row=dict(row,capture_fenced=True)
            active=tx.execute("SELECT * FROM payments.effects WHERE attempt_id=%s AND kind='capture' AND state NOT IN ('SUCCEEDED','FAILED')",(attempt_id,)).fetchone()
            if row['captured_minor'] or active:return {'owner':'payments','id':attempt_id,'state':'CAPTURED' if row['captured_minor'] else 'UNKNOWN','captured_minor':row['captured_minor'],'capture_fenced':True,'no_future_capture':False,'effect_certainty':'committed' if row['captured_minor'] else 'unknown'}
            request={'attempt_id':attempt_id,'amount_minor':row['amount_minor'],'currency':'USD','object_id':row['provider_id'],'obligation_id':row['obligation_id'],'merchant_id':actor['merchant_id']}
            local=row['amount_minor']==0 or row['state'] in ('DECLINED','FAILED','CANCELED')
        if row['provider_id'] is None and not local:
            # Closing capture does not discard an authorization sent before a lost
            # acknowledgement. Inspect only its retained identity; never resend it.
            with self.db.transaction('payments',authority_guard=self._guard(actor,'payments.cancel',attempt_id)) as tx:
                authorization=tx.execute("SELECT * FROM payments.effects WHERE attempt_id=%s AND kind='create_authorization'",(attempt_id,)).fetchone()
            if authorization is not None:
                self._pf_effect(attempt_id,'create_authorization',authorization['amount_minor'],authorization['id'],authorization['request'],actor=actor,admission_operation='payments.cancel')
                row=self.inspect_attempt(attempt_id,actor=actor)
                local=row['amount_minor']==0 or row['state'] in ('DECLINED','FAILED','CANCELED')
                request['object_id']=row['provider_id']
            if row['provider_id'] is None and not local:return {'owner':'payments','id':attempt_id,'state':'UNKNOWN','capture_fenced':True,'no_future_capture':False,'effect_certainty':'unknown'}
        return self._pf_effect(attempt_id,'cancel',row['amount_minor'],self.effect_id(attempt_id,'cancel'),request,actor=actor,zero_state='CANCELED' if local else None)

    def capture(self,attempt_id,order_receipt,allocation_receipt,*,actor=None):
        if self.profile=='local-sandbox':return super().capture(attempt_id,order_receipt,allocation_receipt,actor=actor)
        if self.order_port is None or self.inventory_port is None:raise ValueError('actual_order_allocation_inspectors_required')
        order=self.order_port(order_receipt,actor=actor);allocation=self.inventory_port(allocation_receipt,actor=actor)
        if allocation['order_id']!=order['id'] or allocation['state']!='allocated':raise ValueError('allocation_order_mismatch')
        row=self.inspect_attempt(attempt_id,actor=actor)
        if order['total_minor']!=row['amount_minor'] or (order.get('payment_id') is None and order.get('payment_state')!='pending') or (order.get('payment_id') is not None and order['payment_id']!=attempt_id):raise ValueError('order_amount_mismatch')
        admitted=self.admission_port(attempt_id,actor=actor) if self.admission_port else {}
        if order.get('owner_id')!=row['owner_id'] or order.get('checkout_id')!=admitted.get('checkout_id'):raise ValueError('order_admission_mismatch')
        deadline=admitted.get('frozen_quote_until')
        if type(deadline)is not int or allocation.get('expires_at')!=deadline:raise ValueError('original_reservation_capture_deadline_required')
        request={'attempt_id':attempt_id,'object_id':row['provider_id'],'amount_minor':row['amount_minor'],'currency':'USD','order_id':order['id'],'allocation_id':allocation['id'],'obligation_id':row['obligation_id'],'merchant_id':actor['merchant_id'],'capture_deadline':deadline}
        return self._pf_effect(attempt_id,'capture',row['amount_minor'],self.effect_id(attempt_id,'capture'),request,actor=actor,zero_state='ZERO_SETTLEMENT' if row['amount_minor']==0 else None)

    def create_authorization(self,attempt_id,quote_digest,amount_minor,currency,customer_email,*,actor=None,demo_instrument=None):
        if self.profile=='local-sandbox':return super().create_authorization(attempt_id,quote_digest,amount_minor,currency,customer_email,actor=actor)
        self.attempt_policy.validate_authorization(amount_minor,currency)
        if demo_instrument not in ('demo_success','demo_decline','demo_pending') or self.admission_port is None:raise ValueError('immutable_purchase_admission_required')
        admitted=self.admission_port(attempt_id,actor=actor)
        if admitted.get('quote_digest')!=quote_digest or admitted.get('total_minor')!=amount_minor or admitted.get('demo_instrument')!=demo_instrument or admitted.get('currency')!='USD' or not admitted.get('owner_id') or admitted.get('obligation_id')!=actor.get('obligation_id'):raise ValueError('admission_payment_mismatch')
        with self.db.transaction('payments',authority_guard=self._guard(actor,'payments.create_authorization',attempt_id)) as tx:
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('payment-attempt:'+attempt_id,))
            if tx.execute('SELECT 1 FROM payments.authorization_fences WHERE attempt_id=%s FOR SHARE',(attempt_id,)).fetchone():raise ValueError('capture_fenced')
            tx.execute('INSERT INTO payments.attempts(id,owner_id,amount_minor,currency,quote_digest,state,demo_instrument,obligation_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(attempt_id,admitted['owner_id'],amount_minor,currency,quote_digest,'ZERO_TOTAL' if amount_minor==0 else 'REQUESTED',demo_instrument,admitted['obligation_id']))
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s FOR UPDATE',(attempt_id,)).fetchone()
            if (row['owner_id'],row['amount_minor'],row['quote_digest'],row['demo_instrument'])!=(admitted['owner_id'],amount_minor,quote_digest,demo_instrument):raise ValueError('idempotency_conflict')
        request={'attempt_id':attempt_id,'amount_minor':amount_minor,'currency':'USD','capture_method':'manual','demo_instrument':demo_instrument,'obligation_id':admitted['obligation_id'],'merchant_id':actor['merchant_id']}
        return self._pf_effect(attempt_id,'create_authorization',amount_minor,self.effect_id(attempt_id,'create_authorization'),request,actor=actor,zero_state='ZERO_TOTAL' if amount_minor==0 else None)

    @staticmethod
    def effect_id(attempt_id,kind,request_id=None):
        return str(uuid.uuid5(uuid.NAMESPACE_URL,'sme:payments:'+str(attempt_id)+':'+kind+':'+str(request_id or attempt_id)))

    def fence_capture(self,attempt_id,*,actor=None):return self.cancel_authorization(attempt_id,actor=actor)

    def initialize(self):
        self.db.verify_migration('payments',1,DDL,allow_newer=self.profile!='local-sandbox')
        if self.profile!='local-sandbox':
            self.db.verify_migration('payments',2,self.PF_DDL,allow_newer=True)
            self.db.verify_migration('payments',3,self.PF_FENCE_DDL)
        return {'owner':'payments','ready':True,'profile':self.profile}

    def inspect_attempt(self,attempt_id,*,actor=None):
        if self.profile=='local-sandbox':return super().inspect_attempt(attempt_id,actor=actor)
        with self.db.transaction('payments',authority_guard=self._guard(actor,'payments.inspect_attempt',attempt_id)) as tx:
            row=tx.execute('SELECT * FROM payments.attempts WHERE id=%s',(attempt_id,)).fetchone()
            if row is None:raise ValueError('not_found')
            return dict(row,owner='payments')

    def inspect_receipt(self,evidence,*,actor=None):
        effect_id=evidence.get('effect_id') or evidence.get('receipt_id')
        if not effect_id:
            return self.inspect_attempt(evidence.get('id'),actor=actor)
        # Resolve no secret/result before the current resource guard.
        attempt=evidence.get('attempt_id') or evidence.get('payment_id') or evidence.get('id')
        with self.db.transaction('payments',authority_guard=self._guard(actor,'payments.inspect_receipt',attempt)) as tx:
            row=tx.execute('SELECT * FROM payments.effects WHERE id=%s AND attempt_id=%s',(effect_id,attempt)).fetchone()
            if row is None or row['result'] is None:raise ValueError('receipt_not_found')
            return row['result']

    def migrate(self):
        self.db.migrate('payments',1,DDL)
        if self.profile!='local-sandbox':
            self.db.migrate('payments',2,self.PF_DDL)
            self.db.migrate('payments',3,self.PF_FENCE_DDL)

    def refund(self,payment_id,refund_request_id,approved_plan,reason=None,*,actor=None):
        if self.profile=='local-sandbox':return super().refund(payment_id,refund_request_id,approved_plan,reason,actor=actor)
        if not isinstance(approved_plan,dict) or not approved_plan.get('id') or self.approved_plan_port is None:raise ValueError('approved_original_unit_plan_required')
        plan=self.approved_plan_port(approved_plan['id'],actor=actor)
        if plan.get('state') not in ('approved','refund_pending','refunded') or not plan.get('unit_ids') or not plan.get('unit_allocations') or plan.get('currency')!='USD':raise ValueError('approved_original_unit_plan_required')
        amount=plan['amount_minor'];original=sum(u['net_minor']+u['tax_minor'] for u in plan['unit_allocations'])+plan['shipping_refund_minor']+plan['shipping_tax_refund_minor']
        if type(amount) is not int or amount<0 or amount!=original:raise ValueError('refund_plan_conservation_error')
        row=self.inspect_attempt(payment_id,actor=actor)
        if row['order_id']!=plan['order_id']:raise ValueError('refund_order_payment_mismatch')
        request={'object_id':row['provider_id'],'attempt_id':payment_id,'amount_minor':amount,'currency':'USD','plan_id':plan['id'],'order_id':plan['order_id'],'reason':plan['reason'],'unit_ids':plan['unit_ids'],'obligation_id':actor['obligation_id'],'merchant_id':actor['merchant_id']}
        return self._pf_effect(payment_id,'refund',amount,self.effect_id(payment_id,'refund',refund_request_id),request,actor=actor,plan_id=plan['id'],zero_state='ZERO_REFUND' if amount==0 else None)

    def safety_snapshot(self, *, actor):
        with self.db.transaction('payments',authority_guard=self._guard(actor,'payments.safety_snapshot','payments')) as tx:
            attempts={r['state']:r['count'] for r in tx.fetchall('SELECT state,count(*)::bigint AS count FROM payments.attempts GROUP BY state')}
            effects={r['state']:r['count'] for r in tx.fetchall('SELECT state,count(*)::bigint AS count FROM payments.effects GROUP BY state')}
            refunds=tx.fetchone("SELECT count(*)::bigint AS count FROM payments.effects WHERE kind='refund' AND state NOT IN ('SUCCEEDED','FAILED')")['count']
            pending_attempts=sum(n for state,n in attempts.items() if state not in ('CAPTURED','ZERO_SETTLEMENT','CANCELED','DECLINED','FAILED'))
            pending_effects=sum(n for state,n in effects.items() if state not in ('SUCCEEDED','FAILED'))
            return {'owner':'payments','complete':True,'unresolved':pending_attempts+pending_effects,'attempts':attempts,'effects':effects,'pending_refunds':refunds,'failures':[]}
