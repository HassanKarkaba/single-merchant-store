"""Actual Pg owner-filtered delivery metadata, independent of native Audit claims."""
from pathlib import Path
import hashlib,json,os,sys,uuid
import pytest

pytestmark = pytest.mark.native

from app.models import persistence as p
from app.models import disposable as g

@pytest.fixture
def setup():
 dsn=os.environ.get('SME_TEST_DSN');assert dsn,'explicit attested disposable target required'
 guard=g.DisposableResourceGuard.from_environment();guard.attest_dsn(dsn)
 db=p.PostgresPersistence(dsn);db.migrate_core();db.migrate_delivery()
 own='pfdelivery_'+uuid.uuid4().hex[:16];foreign='pfdelivery_'+uuid.uuid4().hex[:16]
 for owner in (own,foreign):db.migrate(owner,1,'CREATE TABLE '+owner+'(id text PRIMARY KEY)')
 db.migrate('pf_delivery_archive',1,'CREATE TABLE pf_delivery_archive(id text PRIMARY KEY,owner text NOT NULL,event_id text NOT NULL,payload_digest text NOT NULL)')
 current={'open':True};calls=[]
 def authority(tx,operation,resource):
  calls.append((tx.owner,operation,resource));assert tx.owner==own
  return current['open']
 port=db.owner_delivery(own,authority);port.initialize()
 event=str(uuid.uuid4());payload={'kind':'synthetic_fact','amount_minor':101,'label':'résumé'}
 with db.transaction(own) as tx:tx.stage_outbox('synthetic',event,payload);tx.stage_audit(event,payload)
 return db,port,own,foreign,event,payload,current,calls

def test_owner_filter_claim_fence_replay_and_revoked_admission(setup):
 db,port,own,foreign,event,payload,current,calls=setup
 other=db.owner_delivery(foreign,lambda *args:True)
 assert other.safety_snapshot()['unresolved']==0 and port.safety_snapshot()['unresolved']==2
 with pytest.raises(p.PostgresContractError,match='MISSING_OR_AMBIGUOUS'):other.inspect_publication(event)
 claim=port.claim_publications()[0];assert claim['event']==payload and claim['event_id']==event
 actual=port.inspect_publication(event);assert actual['lease_id']==claim['fence'] and actual['lease_active'] is True and actual['lease_until'].tzinfo is not None
 assert port.claim_publications()==[]
 with pytest.raises(p.PostgresContractError,match='CURRENT_PUBLICATION_LEASE_REQUIRED'):port.ack_publication(event,str(uuid.uuid4()))
 receipt=port.ack_publication(event,claim['fence'],topic=claim['topic']);assert receipt['state']=='DELIVERED'
 assert port.ack_publication(event,claim['fence'],topic=claim['topic'])==receipt
 settled=port.inspect_publication(event);assert settled['state']=='DELIVERED' and settled['lease_id']==claim['fence'] and settled['lease_active'] is False
 current['open']=False
 with pytest.raises(p.PostgresContractError,match='DELIVERY_AUTHORITY_DENIED'):port.ack_publication(event,claim['fence'])
 assert all(owner==own for owner,operation,resource in calls)

def test_audit_requires_actual_retained_archive_receipt_not_boolean(setup):
 db,port,own,foreign,event,payload,current,calls=setup;claim=port.claim_audit()[0]
 observed=port.inspect_audit(event);assert observed['lease_id']==claim['fence'] and observed['lease_active'] is True
 port.audit_verifier=lambda *args:True
 with pytest.raises(p.PostgresContractError,match='ACTUAL_DEMO_AUDIT_RECEIPT_REQUIRED'):port.ack_audit(event,claim['fence'],{'receipt_id':'forged'})
 assert port.inspect_audit(event)['state']=='SOURCE_ACCEPTED'
 archive_id=str(uuid.uuid4());digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()
 with db.transaction('pf_delivery_archive') as tx:tx.execute('INSERT INTO pf_delivery_archive VALUES(%s,%s,%s,%s)',(archive_id,own,event,digest))
 actual={'owner':own,'event_id':event,'payload_digest':digest,'receipt_id':archive_id,'state':'DEMO_RECORDED'}
 def verify(tx,owner,event_id,body,evidence):
  row=tx.fetchone('SELECT * FROM pf_delivery_archive WHERE id=%s',(evidence.get('receipt_id'),))
  if not row or (row['owner'],row['event_id'],row['payload_digest'])!=(owner,event_id,digest):return None
  return actual
 port.audit_verifier=verify
 assert port.ack_audit(event,claim['fence'],actual)==actual
 assert port.ack_audit(event,claim['fence'],actual)==actual
 assert port.inspect_audit(event)['state']=='RECORDED'
 assert port.safety_snapshot()['unresolved']==1

def test_expired_lease_reclaim_and_dead_facts_remain_unresolved(setup):
 db,port,own,foreign,event,payload,current,calls=setup;old=port.claim_publications()[0]
 with db.transaction(own) as tx:tx.execute('UPDATE commerce_outbox SET lease_until=clock_timestamp() WHERE owner=%s AND message_key=%s',(own,event))
 expired=port.inspect_publication(event);assert expired['lease_id']==old['fence'] and expired['lease_active'] is False
 with pytest.raises(p.PostgresContractError,match='CURRENT_PUBLICATION_LEASE_REQUIRED'):port.ack_publication(event,old['fence'])
 new=port.claim_publications()[0];assert new['fence']!=old['fence'] and new['event']==old['event']
 with db.transaction(own) as tx:tx.execute('UPDATE commerce_outbox SET attempts=8 WHERE owner=%s AND message_key=%s',(own,event))
 assert port.defer_publication(event,new['fence'],'bounded synthetic delivery failure')['state']=='DEAD'
 assert port.claim_publications()==[]
 assert port.safety_snapshot()['outbox']['DEAD']==1 and port.safety_snapshot()['unresolved']==2
