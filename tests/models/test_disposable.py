"""New bootstrap-only negatives: no PostgreSQL and no supplied test execution."""
import copy
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from app.models import disposable as m

class Cursor:
    def __init__(self,rows):self.rows=rows
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows

class Connection:
    autocommit=False
    def __init__(self,proof):
        self.proof=proof;self.sql=[];self.mutations=[];self.changes={};self.schemas=[{'name':'public','owner':'pg_database_owner'},{'name':'application','owner':proof['role']}]
    def execute(self,sql,params=None):
        self.sql.append(sql)
        if not sql.lstrip().upper().startswith('SELECT'):
            self.mutations.append(sql);return Cursor([])
        p=self.proof;e=p['endpoint']
        if 'current_database() AS database' in sql:
            row={'database':p['database'],'role':p['role'],'database_oid':p['database_oid'],'database_owner_oid':p['role_oid'],'role_oid':p['role_oid'],'cluster_id':p['cluster_id'],'server_address':e['server_address'],'server_port':e['server_port'],'dangerous_role':False}
            row.update(self.changes.get('identity',{}));return Cursor([row])
        if 'FROM sme_test_control.disposable_fixture' in sql:
            row={'profile':p['profile'],'run_id':p['run_id'],'environment_id':p['environment_id'],'cluster_id':p['cluster_id'],'database_name':p['database'],'role_name':p['role'],'database_oid':p['database_oid'],'role_oid':p['role_oid'],'marker_nonce':p['marker_nonce'],'provisioned_at':datetime.fromisoformat(p['provisioned_at'])}
            row.update(self.changes.get('marker',{}));return Cursor([] if self.changes.get('missing_marker') else [row])
        if "WHERE n.nspname='sme_test_control'" in sql:
            return Cursor([{'owner_name':'independent_fixture_bootstrap','can_create':False,**self.changes.get('control',{})}])
        if sql.startswith('SELECT datname'):return Cursor(self.changes.get('other_databases',[]))
        if 'pg_advisory_xact_lock' in sql:return Cursor([{}])
        if 'SELECT n.nspname AS name' in sql:return Cursor(self.schemas)
        raise AssertionError('unexpected query')
    def close(self):pass

class DisposableGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name).resolve()/'proof.json'
        self.run=str(uuid.uuid4());self.environment=str(uuid.uuid4());name='sme_test_'+uuid.UUID(self.run).hex
        self.data={'format':'sme.disposable-fixture/1','profile':'demo-test','run_id':self.run,'environment_id':self.environment,'database':name,'role':name,'cluster_id':'7561000000000000001','database_oid':12345,'role_oid':12346,'marker_nonce':'a'*64,'endpoint':{'host':'127.0.0.1','connection_port':55432,'server_address':'127.0.0.1','server_port':55432},'provisioned_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),'provisioner':'operations.new-disposable-fixture/1'}
        self.write(self.data)
        self.dsn='postgresql://'+name+':not-logged@127.0.0.1:55432/'+name
    def tearDown(self):self.temp.cleanup()
    def write(self,data):self.path.write_text(json.dumps(data));self.path.chmod(0o600)
    def guard(self,**kwargs):return m.DisposableResourceGuard(self.path,run_id=self.run,environment_id=self.environment,profile=kwargs.get('profile','demo-test'))
    def test_profile_run_and_protected_preexisting_receipt_required(self):
        for profile in (None,'portfolio-demo','live-production','local-sandbox'):
            with self.subTest(profile=profile),self.assertRaises(m.DisposableTargetDenied):self.guard(profile=profile)
        with self.assertRaises(m.DisposableTargetDenied):m.DisposableResourceGuard.from_environment({})
        self.path.chmod(0o644)
        with self.assertRaises(m.DisposableTargetDenied):self.guard()
        self.path.unlink()
        with self.assertRaises(m.DisposableTargetDenied):self.guard()
        self.assertFalse(self.path.exists(),'guard must never create a proof')
    def test_parsed_dsn_cannot_be_authorized_by_password_host_or_query_substring(self):
        guard=self.guard()
        candidates=['postgresql://'+self.data['role']+':'+self.data['database']+'@127.0.0.1:55432/shared_demo',self.dsn.replace('127.0.0.1','other-server'),self.dsn+'?dbname='+self.data['database'],self.dsn+'?user='+self.data['role'],self.dsn+'?options=-csearch_path%3Dpublic',self.dsn.replace('/'+self.data['database'],'/shared_demo')+'?application_name='+self.data['database'],self.dsn+'#'+self.data['database']]
        for dsn in candidates:
            connection=Connection(self.data)
            with self.subTest(dsn_kind=candidates.index(dsn)),self.assertRaises(m.DisposableTargetDenied):guard.verify(connection,dsn)
            self.assertEqual(connection.sql,[]);self.assertEqual(connection.mutations,[])
    def test_wrong_cluster_db_role_oid_server_marker_and_other_database_grants_deny_before_writes(self):
        guard=self.guard()
        cases=[{'identity':{field:value}} for field,value in [('database','shared_demo'),('role','merchant_runtime'),('cluster_id','7561000000000000002'),('database_oid',42),('role_oid',43),('server_address','192.0.2.1'),('server_port',5433),('dangerous_role',True)]]
        cases += [{'marker':{'run_id':str(uuid.uuid4())}},{'marker':{'environment_id':str(uuid.uuid4())}},{'marker':{'marker_nonce':'b'*64}},{'missing_marker':True},{'control':{'can_create':True}},{'control':{'owner_name':self.data['role']}},{'other_databases':[{'datname':'shared_demo'}]}]
        for change in cases:
            connection=Connection(self.data);connection.changes=change
            with self.subTest(change=change),self.assertRaises(m.DisposableTargetDenied):guard.reset_owned_schemas(connection,self.dsn)
            self.assertEqual(connection.mutations,[])
    def test_decoy_is_untouched_and_valid_reset_preserves_independent_control(self):
        decoy={'schemas':{'valuable':{'row':'retain exactly'}}};before=copy.deepcopy(decoy)
        connection=Connection(self.data)
        receipt=self.guard().reset_owned_schemas(connection,self.dsn)
        self.assertEqual(decoy,before)
        self.assertEqual(receipt['state'],'DISPOSABLE_SCHEMAS_RESET')
        self.assertEqual(connection.mutations,['DROP SCHEMA "public" CASCADE','DROP SCHEMA "application" CASCADE','CREATE SCHEMA public'])
        self.assertFalse(any('sme_test_control' in sql for sql in connection.mutations))
    def test_foreign_schema_or_autocommit_rejects_atomic_reset_before_mutation(self):
        for foreign in (True,False):
            connection=Connection(self.data)
            if foreign:connection.schemas.append({'name':'not_owned','owner':'decoy_owner'})
            else:connection.autocommit=True
            with self.assertRaises(m.DisposableTargetDenied):self.guard().reset_owned_schemas(connection,self.dsn)
            self.assertEqual(connection.mutations,[])
    def test_database_and_role_must_be_unique_full_run_names_and_proof_not_symlink(self):
        wrong=dict(self.data,database='sme_tests_application');self.write(wrong)
        with self.assertRaises(m.DisposableTargetDenied):self.guard()
        self.path.unlink();target=self.path.parent/'target';target.write_text(json.dumps(self.data));target.chmod(0o600);self.path.symlink_to(target)
        with self.assertRaises(m.DisposableTargetDenied):self.guard()

if __name__=='__main__':unittest.main()
