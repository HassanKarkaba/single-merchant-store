"""Real isolated PostgreSQL tests. Set SME_CORRECTIONS_TEST_DSN explicitly.

Never defaults to or mutates the application sandbox. Each test uses fresh owner
and table identities and retains no expectation of cloud qualification.
"""
from pathlib import Path
import hashlib
import os
import sys
import threading
import time
import uuid
import pytest

from app.models import persistence as p
from app.models import authorization as aac
from app.models import disposable as guard_module
DSN=os.environ.get('SME_CORRECTIONS_TEST_DSN')
pytestmark=[pytest.mark.native,pytest.mark.skipif(not DSN,reason='explicit isolated test database required')]
DIGEST=hashlib.sha256(b'unchanged request').hexdigest()

@pytest.fixture
def db():
    guard_module.DisposableResourceGuard.from_environment().attest_dsn(DSN)
    persistence=p.PostgresPersistence(DSN)
    persistence.migrate_core()
    persistence.initialize()
    return persistence

@pytest.fixture
def owner(db):
    name='pgtest_'+uuid.uuid4().hex[:16]
    db.migrate(name,1,'CREATE TABLE '+name+'(id text PRIMARY KEY, quantity bigint NOT NULL CHECK(quantity>=0))')
    return name

def count(db,owner):
    with db.transaction(owner) as tx:
        return tx.fetchone('SELECT count(*) AS n FROM '+owner)['n']

def make_guard(db,owner,**overrides):
    from datetime import datetime,timedelta,timezone
    authority=aac.HostBoundPgAuthority(db,receipt_verifier=lambda *args: True)
    authority.migrate()
    authority.initialize()
    suffix=uuid.uuid4().hex[:16]
    context={'merchant_id':'m'+suffix,'principal_id':'p'+suffix,'session_id':'s'+suffix,'service_id':'svc'+suffix}
    authority.provision(**context,expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),revision='r1')
    options={**context,'policy_evaluator':lambda snapshots,owner,action: True,**overrides}
    return authority,context,aac.PgAuthorityGuard(**options)

def test_schema_admission_and_exact_immutable_migration(db,owner):
    with pytest.raises(p.PostgresContractError,match='SCHEMA_ADMISSION_CLOSED'):
        with db.transaction('missing_'+uuid.uuid4().hex[:12]):
            pytest.fail('unmigrated owner admitted')
    with pytest.raises(p.PostgresContractError,match='MIGRATION_IDENTITY_CONFLICT'):
        db.migrate(owner,1,'CREATE TABLE should_not_exist(id integer)')
    with db.transaction(owner) as tx:
        assert tx.fetchone('SELECT to_regclass(%s) AS present',('should_not_exist',))['present'] is None

def test_shared_schema_fence_blocks_migration_until_commit(db,owner):
    started,done=threading.Event(),threading.Event()
    failures=[]
    def migrate():
        started.set()
        try: db.migrate(owner,2,'ALTER TABLE '+owner+' ADD COLUMN revision bigint NOT NULL DEFAULT 1')
        except BaseException as error: failures.append(error)
        finally: done.set()
    with db.transaction(owner) as tx:
        tx.fetchone('SELECT * FROM '+owner)
        worker=threading.Thread(target=migrate)
        worker.start()
        assert started.wait(2)
        assert not done.wait(.15),'exclusive migration crossed active shared fence'
    assert done.wait(5)
    worker.join()
    assert not failures
    with db.transaction(owner) as tx:
        tx.execute('INSERT INTO '+owner+'(id,quantity,revision) VALUES (%s,%s,%s)',('a',1,2))
        assert tx.fetchone('SELECT revision FROM '+owner+' WHERE id=%s',('a',))['revision']==2

def test_command_effect_outbox_audit_rollback_then_replay(db,owner):
    with pytest.raises(RuntimeError,match='abort'):
        with db.transaction(owner,command_id='command',request_digest=DIGEST) as tx:
            assert tx.command_lookup() is None
            tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('a',1))
            tx.stage_outbox('order.created','event',{'id':'a','amount':9007199254740993})
            tx.stage_audit('audit',{'effect':'created'})
            tx.command_complete({'id':'a'})
            raise RuntimeError('abort')
    assert count(db,owner)==0
    assert db.lookup_command(owner,'command') is None
    with db.transaction(owner) as tx:
        assert tx.fetchone('SELECT count(*) AS n FROM commerce_outbox WHERE owner=%s',(owner,))['n']==0
        assert tx.fetchone('SELECT count(*) AS n FROM commerce_audit_staging WHERE owner=%s',(owner,))['n']==0
    with db.transaction(owner,command_id='command',request_digest=DIGEST) as tx:
        tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('a',1))
        tx.stage_outbox('order.created','event',{'id':'a','amount':9007199254740993})
        assert tx.stage_audit('audit',{'effect':'created'})['state']=='SOURCE_ACCEPTED'
        tx.command_complete({'id':'a'})
    with db.transaction(owner,command_id='command',request_digest=DIGEST) as tx:
        assert tx.command_lookup()=={'id':'a'}
        assert tx.replayed is True
        with pytest.raises(p.PostgresContractError,match='READ_RECEIPT_ONLY'):
            tx.execute('DELETE FROM '+owner)
    assert count(db,owner)==1
    with pytest.raises(p.PostgresContractError,match='IDEMPOTENCY_CONFLICT'):
        with db.transaction(owner,command_id='command',request_digest='f'*64): pass
    with db.transaction(owner) as tx:
        assert tx.fetchone('SELECT payload FROM commerce_outbox WHERE owner=%s',(owner,))['payload']['amount']==9007199254740993
        assert tx.fetchone('SELECT state FROM commerce_audit_staging WHERE owner=%s',(owner,))['state']=='SOURCE_ACCEPTED'

def test_missing_command_result_and_stale_handle_cannot_commit(db,owner):
    with pytest.raises(p.PostgresContractError,match='RESULT_REQUIRED'):
        with db.transaction(owner,command_id='incomplete',request_digest=DIGEST) as tx:
            tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('a',1))
    assert count(db,owner)==0
    with pytest.raises(p.PostgresContractError,match='TRANSACTION_CLOSED'):
        tx.execute('SELECT 1')

def test_changed_outbox_and_audit_identity_rollback(db,owner):
    for method,args in [('stage_outbox',('topic','key')),('stage_audit',('key',))]:
        with db.transaction(owner) as tx:
            getattr(tx,method)(*args,{'value':1})
        with pytest.raises(p.PostgresContractError,match='IDENTITY_CONFLICT'):
            with db.transaction(owner) as tx:
                tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',(method,1))
                getattr(tx,method)(*args,{'value':2})
    assert count(db,owner)==0

def test_lost_commit_ack_preserves_durable_receipt_without_retry(db,owner):
    import psycopg
    from psycopg.rows import dict_row
    commits=[]
    class LostAck:
        def __init__(self,dsn): self.connection=psycopg.connect(dsn,row_factory=dict_row)
        def execute(self,*args,**kwargs): return self.connection.execute(*args,**kwargs)
        def rollback(self): return self.connection.rollback()
        def close(self): return self.connection.close()
        def commit(self):
            commits.append(1)
            self.connection.commit()
            raise OSError('connection lost after physical commit')
    uncertain=p.PostgresPersistence(DSN,connect=LostAck)
    with pytest.raises(p.PostgresCommitUnknown) as captured:
        with uncertain.transaction(owner,command_id='unknown',request_digest=DIGEST) as tx:
            tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('a',1))
            tx.command_complete({'id':'a'})
    assert captured.value.command_id=='unknown'
    assert commits==[1]
    assert db.lookup_command(owner,'unknown')['result']=={'id':'a'}
    assert count(db,owner)==1

def test_guard_admission_blocks_revocation_until_same_transaction_commits(db,owner):
    authority,context,guard=make_guard(db,owner)
    started,done=threading.Event(),threading.Event()
    results=[]
    def revoke():
        started.set()
        results.append(authority.prepare_change(context['merchant_id'],[('session',context['session_id'])],'logout'))
        done.set()
    with db.transaction(owner,authority_guard=guard) as tx:
        tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('admitted',1))
        worker=threading.Thread(target=revoke)
        worker.start()
        assert started.wait(2)
        assert not done.wait(.15),'revocation crossed protected transaction shared lock'
    assert done.wait(5)
    worker.join()
    assert results[0]['state']=='CLOSED'
    with pytest.raises(aac.AuthorityDenied,match='CLOSED_OR_EXPIRED'):
        with db.transaction(owner,authority_guard=guard) as tx:
            tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('denied',1))
    assert count(db,owner)==1

def test_revocation_before_admission_denies_and_old_publication_cannot_reopen(db,owner):
    authority,context,guard=make_guard(db,owner)
    merchant,session=context['merchant_id'],context['session_id']
    authority.prepare_change(merchant,[('session',session)],'first')
    with pytest.raises(aac.AuthorityDenied):
        with db.transaction(owner,authority_guard=guard): pass
    authority.publish_change(merchant,'first',native_receipt={'id':'r1'},revision='r2')
    authority.prepare_change(merchant,[('session',session)],'second')
    # Exact predecessor replay stays inert and cannot reopen the successor gate.
    assert authority.publish_change(merchant,'first',native_receipt={'id':'r1'},revision='r2')['replayed'] is True
    with pytest.raises(aac.AuthorityDenied):
        with db.transaction(owner,authority_guard=guard): pass
    assert count(db,owner)==0

def test_missing_policy_and_expiring_session_fail_closed_before_commit(db,owner):
    authority,context,guard=make_guard(db,owner,policy_evaluator=None)
    with pytest.raises(aac.AuthorityDenied,match='NATIVE_POLICY'):
        with db.transaction(owner,authority_guard=guard): pass
    authority,context,guard=make_guard(db,owner)
    # Native expiry bound is rechecked on the exact connection before commit.
    with db.transaction('authority') as tx:
        tx.execute("UPDATE commerce_authority_gates SET expires_at=clock_timestamp()+interval '0.15 seconds' WHERE merchant_id=%s AND kind='session'",(context['merchant_id'],))
    with pytest.raises(aac.AuthorityDenied,match='CLOSED_OR_EXPIRED'):
        with db.transaction(owner,authority_guard=guard) as tx:
            tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('expired',1))
            tx.execute('SELECT pg_sleep(%s)',(.20,))
    assert count(db,owner)==0

def test_runtime_port_rejects_transaction_escape(db,owner):
    for query in ['COMMIT','BEGIN','SELECT 1; DELETE FROM '+owner,'/*comment*/ ROLLBACK','CREATE TABLE escaped(id int)']:
        with pytest.raises(p.PostgresContractError):
            with db.transaction(owner) as tx: tx.execute(query)

def test_distinct_process_waits_for_shared_authority_fence(db,owner):
    import subprocess
    authority,context,guard=make_guard(db,owner)
    script='''
import json,sys
from app.models.persistence import PostgresPersistence
from app.models.authorization import HostBoundPgAuthority
from app.models.disposable import DisposableResourceGuard
DisposableResourceGuard.from_environment().attest_dsn(sys.argv[1])
a=HostBoundPgAuthority(PostgresPersistence(sys.argv[1]))
print('READY',flush=True)
r=a.prepare_change(sys.argv[2],[('session',sys.argv[3])],'process_logout')
print(json.dumps(r),flush=True)
'''
    with db.transaction(owner,authority_guard=guard) as tx:
        tx.execute('INSERT INTO '+owner+' VALUES (%s,%s)',('process',1))
        process=subprocess.Popen([sys.executable,'-c',script,DSN,context['merchant_id'],context['session_id']],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        assert process.stdout.readline().strip()=='READY'
        try:
            process.wait(timeout=.15)
            pytest.fail('second process crossed shared authority fence')
        except subprocess.TimeoutExpired:
            pass
    stdout,stderr=process.communicate(timeout=10)
    assert process.returncode==0,stderr
    assert '"state": "CLOSED"' in stdout
    with pytest.raises(aac.AuthorityDenied):
        with db.transaction(owner,authority_guard=guard): pass
    assert count(db,owner)==1
