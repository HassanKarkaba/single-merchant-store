"""Owner integration over real Pg + owned simulator; auth is a test capability.

This layer verifies payment conservation/independent effects, not the complete
application/AAC/browser profile. A separately attested disposable target is mandatory.
"""
from pathlib import Path
import json,os,sys,uuid
import pytest

pytestmark = pytest.mark.native

from app.models import persistence as p
from app.models import disposable as g
from app.models import payments as pay
from app.models import integrations as sim

@pytest.fixture(scope='module')
def setup():
 dsn=os.environ.get('SME_TEST_DSN')
 if not dsn:pytest.fail('explicit attested disposable target required')
 g.DisposableResourceGuard.from_environment().attest_dsn(dsn)
 db=p.PostgresPersistence(dsn);db.migrate_core()
 provider=sim.ExternalIntegrationService(db,authorize=lambda *args:True,profile='demo-test');provider.migrate();provider.initialize();provider.bind_dispatch(lambda *args:True);provider.bind_inspection(lambda *args:True)
 owner=pay.PaymentService(db,provider,authorize=lambda *args:True,profile='demo-test');owner.migrate();owner.initialize()
 db.migrate('pf_money_test',1,'CREATE TABLE pf_money_admissions(id text PRIMARY KEY,receipt jsonb NOT NULL);CREATE TABLE pf_money_receipts(id text PRIMARY KEY,receipt jsonb NOT NULL)')
 def admission(id,*,actor):
  with db.transaction('pf_money_test') as tx:
   row=tx.fetchone('SELECT receipt FROM pf_money_admissions WHERE id=%s',(id,))
   if row is None:raise ValueError('not_found')
   return row['receipt']
 def inspect(evidence,*,actor):
  id=evidence['id'] if isinstance(evidence,dict) else evidence
  with db.transaction('pf_money_test') as tx:
   row=tx.fetchone('SELECT receipt FROM pf_money_receipts WHERE id=%s',(id,))
   if row is None:raise ValueError('not_found')
   return row['receipt']
 owner.admission_port=admission;owner.order_port=inspect;owner.inventory_port=inspect;owner.approved_plan_port=inspect
 return db,provider,owner

def attempt(setup,instrument='demo_success',amount=1000):
 db,provider,owner=setup;id,obligation,merchant,buyer=[str(uuid.uuid4()) for _ in range(4)]
 actor={'id':str(uuid.uuid4()),'kind':'recovery','merchant_id':merchant,'obligation_id':obligation}
 with db.transaction('pf_money_test') as tx:
  deadline=tx.fetchone('SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint+900000 now')['now']
  record={'id':id,'attempt_id':id,'owner_id':buyer,'checkout_id':str(uuid.uuid4()),'accepted_quote_id':str(uuid.uuid4()),'quote_digest':uuid.uuid4().hex,'total_minor':amount,'currency':'USD','demo_instrument':instrument,'obligation_id':obligation,'frozen_quote_until':deadline}
  tx.execute('INSERT INTO pf_money_admissions VALUES(%s,%s::jsonb)',(id,json.dumps(record)))
 return id,actor,record

def capture(setup,id,actor,record):
 db,provider,owner=setup;order,allocation=str(uuid.uuid4()),str(uuid.uuid4())
 records=[{'id':order,'owner':'orders','owner_id':record['owner_id'],'checkout_id':record['checkout_id'],'payment_id':id,'total_minor':record['total_minor']},{'id':allocation,'owner':'inventory','order_id':order,'state':'allocated','expires_at':record['frozen_quote_until']}]
 with db.transaction('pf_money_test') as tx:
  for r in records:tx.execute('INSERT INTO pf_money_receipts VALUES(%s,%s::jsonb)',(r['id'],json.dumps(r)))
 return owner.capture(id,{'id':order},{'id':allocation},actor=actor)

def test_authorize_capture_replay_independent_simulator_and_two_refunds(setup):
 db,provider,owner=setup;id,actor,r=attempt(setup)
 auth=owner.create_authorization(id,r['quote_digest'],1000,'USD','customer@store.invalid',actor=actor,demo_instrument='demo_success');assert auth['state']=='AUTHORIZED'
 captured=capture(setup,id,actor,r);assert captured['state']=='CAPTURED'
 for amount in (333,667):
  plan_id=str(uuid.uuid4());plan={'id':plan_id,'order_id':captured['order_id'],'owner_id':r['owner_id'],'state':'approved','currency':'USD','kind':'return','reason':'synthetic inspected unit','unit_ids':[str(uuid.uuid4())],'unit_allocations':[{'net_minor':amount,'tax_minor':0}],'amount_minor':amount,'shipping_refund_minor':0,'shipping_tax_refund_minor':0}
  with db.transaction('pf_money_test') as tx:tx.execute('INSERT INTO pf_money_receipts VALUES(%s,%s::jsonb)',(plan_id,json.dumps(plan)))
  key=str(uuid.uuid4());first=owner.refund(id,key,{'id':plan_id},actor=actor);assert first['state']=='SUCCEEDED'
  assert owner.refund(id,key,{'id':plan_id},actor=actor)==first
 actual=owner.inspect_attempt(id,actor=actor);assert actual['captured_minor']==actual['refunded_minor']==1000
 with db.transaction('integrations') as tx:
  effects=tx.fetchall("SELECT kind,amount_minor FROM integrations.money_ledger WHERE object_id=%s",(actual['provider_id'],))
 assert sum(row['amount_minor'] for row in effects if row['kind']=='capture')==1000
 assert sum(row['amount_minor'] for row in effects if row['kind']=='refund')==1000

def test_decline_and_before_birth_fence_never_capture_or_resend(setup):
 db,provider,owner=setup;id,actor,r=attempt(setup,'demo_decline')
 receipt=owner.create_authorization(id,r['quote_digest'],1000,'USD','customer@store.invalid',actor=actor,demo_instrument='demo_decline');assert receipt['state']=='DECLINED'
 assert owner.cancel_authorization(id,actor=actor)['no_future_capture'] is True
 id,actor,r=attempt(setup)
 fence=owner.fence_capture(id,actor=actor);assert fence['state']=='NOT_CREATED' and fence['no_future_capture'] is True
 with pytest.raises(ValueError,match='capture_fenced'):owner.create_authorization(id,r['quote_digest'],1000,'USD','customer@store.invalid',actor=actor,demo_instrument='demo_success')
 with db.transaction('integrations') as tx:assert tx.fetchone("SELECT count(*) n FROM integrations.effects WHERE effect_key=%s",(owner.effect_id(id,'create_authorization'),))['n']==0

def test_zero_settlement_and_zero_refund_are_durable_without_provider_money(setup):
 db,provider,owner=setup;id,actor,r=attempt(setup,amount=0)
 assert owner.create_authorization(id,r['quote_digest'],0,'USD','customer@store.invalid',actor=actor,demo_instrument='demo_success')['state']=='ZERO_TOTAL'
 receipt=capture(setup,id,actor,r);assert receipt['state']=='ZERO_SETTLEMENT'
 plan_id=str(uuid.uuid4());plan={'id':plan_id,'order_id':receipt['order_id'],'state':'approved','currency':'USD','unit_ids':[str(uuid.uuid4())],'unit_allocations':[{'net_minor':0,'tax_minor':0}],'amount_minor':0,'shipping_refund_minor':0,'shipping_tax_refund_minor':0,'reason':'zero priced inspected unit'}
 with db.transaction('pf_money_test') as tx:tx.execute('INSERT INTO pf_money_receipts VALUES(%s,%s::jsonb)',(plan_id,json.dumps(plan)))
 assert owner.refund(id,str(uuid.uuid4()),{'id':plan_id},actor=actor)['state']=='ZERO_REFUND'
 with db.transaction('integrations') as tx:assert tx.fetchone("SELECT count(*) n FROM integrations.objects WHERE body->>'attempt_id'=%s",(id,))['n']==0

def participants(setup,id,actor,r):
 db,_,_=setup;order,allocation=str(uuid.uuid4()),str(uuid.uuid4())
 records=[{'id':order,'owner':'orders','owner_id':r['owner_id'],'checkout_id':r['checkout_id'],'payment_id':id,'total_minor':r['total_minor']},{'id':allocation,'owner':'inventory','order_id':order,'state':'allocated','expires_at':r['frozen_quote_until']}]
 with db.transaction('pf_money_test') as tx:
  for record in records:tx.execute('INSERT INTO pf_money_receipts VALUES(%s,%s::jsonb)',(record['id'],json.dumps(record)))
 return {'id':order},{'id':allocation}

def fault(setup,actor,kind,key,case):
 _,provider,_=setup;run=os.environ['SME_TEST_RUN_ID']
 provider.test_guard=lambda current,run_id,case_id:current==actor and run_id==run and case_id==case
 provider.inject_fault('stripe',kind,'lost_reply',run_id=run,case_id=case,effect_key=key,actor=actor)

def test_lost_capture_ack_recovers_same_object_and_effect_once(setup):
 db,provider,owner=setup;id,actor,r=attempt(setup)
 owner.create_authorization(id,r['quote_digest'],1000,'USD','buyer@shop.invalid',actor=actor,demo_instrument='demo_success')
 evidence=participants(setup,id,actor,r);fault(setup,actor,'capture',owner.effect_id(id,'capture'),'PAC20-capture-ack')
 first=owner.capture(id,*evidence,actor=actor);assert first['state']=='UNKNOWN'
 second=owner.capture(id,*evidence,actor=actor);assert second['state']=='CAPTURED'
 assert owner.capture(id,*evidence,actor=actor)==second
 with db.transaction('integrations') as tx:assert tx.fetchone("SELECT count(*) n FROM integrations.money_ledger WHERE effect_key=%s AND kind='capture'",(owner.effect_id(id,'capture'),))['n']==1
 foreign={**actor,'id':str(uuid.uuid4()),'kind':'customer'}
 with pytest.raises(ValueError,match='not_found'):owner.inspect_receipt(second,actor=foreign)

def test_lost_authorization_ack_can_be_fenced_and_canceled_by_original_identity(setup):
 db,provider,owner=setup;id,actor,r=attempt(setup)
 fault(setup,actor,'create_authorization',owner.effect_id(id,'create_authorization'),'PAC20-auth-ack')
 assert owner.create_authorization(id,r['quote_digest'],1000,'USD','buyer@shop.invalid',actor=actor,demo_instrument='demo_success')['state']=='UNKNOWN'
 receipt=owner.fence_capture(id,actor=actor);assert receipt['state']=='CANCELED' and receipt['no_future_capture'] is True
 with db.transaction('integrations') as tx:assert tx.fetchone("SELECT count(*) n FROM integrations.effects WHERE effect_key=%s",(owner.effect_id(id,'create_authorization'),))['n']==1

def test_new_capture_after_original_deadline_is_denied_without_provider_effect(setup):
 db,_,owner=setup;id,actor,r=attempt(setup)
 owner.create_authorization(id,r['quote_digest'],1000,'USD','buyer@shop.invalid',actor=actor,demo_instrument='demo_success')
 with db.transaction('pf_money_test') as tx:
  r['frozen_quote_until']=tx.fetchone('SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint-1 now')['now'];tx.execute('UPDATE pf_money_admissions SET receipt=%s::jsonb WHERE id=%s',(json.dumps(r),id))
 evidence=participants(setup,id,actor,r)
 with pytest.raises(ValueError,match='capture_deadline_expired'):owner.capture(id,*evidence,actor=actor)
 with db.transaction('integrations') as tx:assert tx.fetchone("SELECT count(*) n FROM integrations.effects WHERE effect_key=%s",(owner.effect_id(id,'capture'),))['n']==0

def test_pending_authorization_and_capture_reconcile_without_new_send_after_deadline(setup):
 import time
 db,_,owner=setup;id,actor,r=attempt(setup,'demo_pending')
 assert owner.create_authorization(id,r['quote_digest'],1000,'USD','buyer@shop.invalid',actor=actor,demo_instrument='demo_pending')['state']=='PENDING'
 time.sleep(5.1)
 assert owner.create_authorization(id,r['quote_digest'],1000,'USD','buyer@shop.invalid',actor=actor,demo_instrument='demo_pending')['state']=='AUTHORIZED'
 with db.transaction('pf_money_test') as tx:
  r['frozen_quote_until']=tx.fetchone('SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint+1000 now')['now'];tx.execute('UPDATE pf_money_admissions SET receipt=%s::jsonb WHERE id=%s',(json.dumps(r),id))
 evidence=participants(setup,id,actor,r);assert owner.capture(id,*evidence,actor=actor)['state']=='PENDING'
 time.sleep(5.1)
 assert owner.capture(id,*evidence,actor=actor)['state']=='CAPTURED'
 with db.transaction('integrations') as tx:assert tx.fetchone("SELECT count(*) n FROM integrations.effects WHERE effect_key=%s",(owner.effect_id(id,'capture'),))['n']==1

def test_native_safety_snapshot_tracks_unsettled_authorization_until_fenced(setup):
 db,_,owner=setup;id,actor,r=attempt(setup,amount=0);before=owner.safety_snapshot(actor=actor)['unresolved']
 owner.create_authorization(id,r['quote_digest'],0,'USD','buyer@shop.invalid',actor=actor,demo_instrument='demo_success')
 current=owner.safety_snapshot(actor=actor);assert current['complete'] is True and current['unresolved']==before+1 and current['attempts']['ZERO_TOTAL']>=1
 owner.fence_capture(id,actor=actor)
 assert owner.safety_snapshot(actor=actor)['unresolved']==before
