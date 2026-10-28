"""Actual Pg workload expiry/revocation/resource closure on guarded fixtures."""
from datetime import datetime,timedelta,timezone
import os
from pathlib import Path
import secrets
import sys
import uuid
import pytest

pytestmark = pytest.mark.native

from app.models import authorization as a
from app.models import persistence as p
from app.models import disposable as g

@pytest.fixture
def setup():
 dsn=os.environ.get('SME_TEST_DSN')
 if not dsn:pytest.fail('explicit guarded disposable target required')
 g.DisposableResourceGuard.from_environment().attest_dsn(dsn)
 db=p.PostgresPersistence(dsn);db.migrate_core();authority=a.HostBoundPgAuthority(db);authority.migrate()
 db.migrate('pf_authority_test',1,'CREATE TABLE pf_admitted_obligations(id text PRIMARY KEY,resource_id text NOT NULL,operation text NOT NULL,receipt text NOT NULL)')
 merchant,machine,service,obligation,resource=[str(uuid.uuid4()) for _ in range(5)]
 secret=secrets.token_bytes(32);reference='private:'+str(uuid.uuid4())
 authority.workload_secret_provider=lambda ref:secret if ref==reference else b'x'*32
 operation='pf_authority_test.reconcile'
 with db.transaction('pf_authority_test') as tx:tx.execute('INSERT INTO pf_admitted_obligations VALUES(%s,%s,%s,%s)',(obligation,resource,operation,str(uuid.uuid4())))
 def inspect(tx,context):
  row=tx.execute('SELECT * FROM pf_admitted_obligations WHERE id=%s',(context['obligation_id'],)).fetchone()
  if row is None:return None
  return {'merchant_id':merchant,'machine_principal_id':machine,'service_id':service,'resource_id':row['resource_id'],'obligation_id':row['id'],'admission_receipt':row['receipt'],'operations':[row['operation']]}
 authority.obligation_verifier=inspect
 now=datetime.now(timezone.utc)
 receipt=authority.provision_workload(merchant,machine,service,reference,issued_at=now-timedelta(seconds=1),expires_at=now+timedelta(minutes=5))
 context={'merchant_id':merchant,'machine_principal_id':machine,'service_id':service,'credential_ref':reference,'operation':operation,'resource_id':resource,'obligation_id':obligation,'expected_epochs':receipt['expected_epochs']}
 return db,authority,context

def test_workload_is_separate_from_human_session_and_matches_actual_obligation(setup):
 db,authority,context=setup
 with db.transaction('pf_authority_test',authority_guard=authority.workload_guard(**context)) as tx:
  assert tx.fetchone('SELECT receipt FROM pf_admitted_obligations WHERE id=%s',(context['obligation_id'],))['receipt']
 with db.transaction('authority') as tx:
  assert tx.fetchone("SELECT count(*) n FROM commerce_authority_gates WHERE merchant_id=%s AND kind='session'",(context['merchant_id'],))['n']==0
 for changes in ({'operation':'pf_authority_test.delete'},{'resource_id':str(uuid.uuid4())},{'obligation_id':str(uuid.uuid4())},{'credential_ref':'private:wrong'}):
  with pytest.raises(a.AuthorityDenied):
   with db.transaction('pf_authority_test',authority_guard=authority.workload_guard(**dict(context,**changes))):pass

def test_workload_expiry_and_revocation_deny_future_steps(setup):
 db,authority,context=setup
 with db.transaction('authority') as tx:tx.execute('UPDATE commerce_workload_credentials SET expires_at=clock_timestamp() WHERE merchant_id=%s AND credential_ref=%s',(context['merchant_id'],context['credential_ref']))
 with pytest.raises(a.AuthorityDenied):
  with db.transaction('pf_authority_test',authority_guard=authority.workload_guard(**context)):pass
 authority.revoke_workload(context['merchant_id'],context['machine_principal_id'],context['service_id'],context['credential_ref'],expected_revision=1)
 with pytest.raises(a.AuthorityDenied):
  with db.transaction('pf_authority_test',authority_guard=authority.workload_guard(**context)):pass

def test_infinite_grant_and_missing_verifier_never_admit(setup):
 db,authority,context=setup;now=datetime.now(timezone.utc)
 with pytest.raises(a.AuthorityDenied):authority.provision_workload(context['merchant_id'],context['machine_principal_id'],context['service_id'],context['credential_ref'],issued_at=now-timedelta(seconds=1),expires_at=now+timedelta(days=91),expected_revision=1)
 authority.obligation_verifier=lambda tx,c:True
 with pytest.raises(a.AuthorityDenied):
  with db.transaction('pf_authority_test',authority_guard=authority.workload_guard(**context)):pass
