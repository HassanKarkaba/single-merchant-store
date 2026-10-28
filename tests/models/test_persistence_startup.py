"""PF bootstrap-only: constructors/probes must never migrate missing schemas."""
from pathlib import Path
import hashlib
import sys
import unittest
from app.models import persistence as p
class Cursor:
    def __init__(self,row):self.row=row
    def fetchone(self):return self.row
class ReadOnlyConnection:
    def __init__(self,*,missing=False,version=1,digest=None):self.missing,self.version,self.digest=missing,version,digest;self.sql=[]
    def execute(self,query,params=()):
        self.sql.append(query)
        if query.startswith('SHOW '):return Cursor({'server_encoding':'UTF8'})
        if not query.lstrip().startswith('SELECT ') or 'set_config(' in query:raise AssertionError('probe attempted mutating SQL')
        if 'to_regclass(' in query:return Cursor({name:None if self.missing else name for name in ('registry','history','commands','outbox','audit')})
        if "owner='persistence_core'" in query:return Cursor({'version':1,'digest':hashlib.sha256(p.PostgresPersistence.CORE_DDL.encode()).hexdigest()})
        if 'commerce_schema_registry' in query:return Cursor({'version':self.version,'digest':self.digest})
        if 'commerce_migration_history' in query:return Cursor({'digest':self.digest})
        raise AssertionError(query)
    def close(self):pass
class ReadOnlyStartupTests(unittest.TestCase):
    def test_constructor_has_no_connection_or_sql(self):
        calls=[]
        p.PostgresPersistence('postgresql://unused',connect=lambda dsn:calls.append(dsn))
        self.assertEqual(calls,[])
    def test_missing_core_check_and_transaction_fail_without_mutation(self):
        connections=[]
        def connector(dsn):
            c=ReadOnlyConnection(missing=True);connections.append(c);return c
        db=p.PostgresPersistence('postgresql://unused',connect=connector)
        for operation in (db.initialize,lambda:db.transaction('orders').__enter__()):
            with self.assertRaises(p.PostgresContractError):operation()
        self.assertTrue(all(q.startswith(('SELECT ','SHOW ')) for c in connections for q in c.sql))
    def test_disposable_attestation_rejects_caller_boolean_or_forged_guard(self):
        db=p.PostgresPersistence('postgresql://unused',connect=lambda dsn:self.fail('must reject before connect'))
        class DisposableResourceGuard:
            def attest_dsn(self,dsn):return True
        for fake in (True,DisposableResourceGuard()):
            with self.assertRaisesRegex(p.PostgresContractError,'SOURCE_OWNED_DISPOSABLE_GUARD_REQUIRED'):db.attest_disposable(fake)
    def test_exact_readonly_owner_receipt_and_newer_version_denial(self):
        ddl='CREATE TABLE demo(id bigint)';digest=hashlib.sha256(ddl.encode()).hexdigest()
        db=p.PostgresPersistence('postgresql://unused',connect=lambda dsn:ReadOnlyConnection(digest=digest))
        self.assertEqual(db.initialize()['state'],'VERIFIED')
        self.assertEqual(db.verify_migration('orders',1,ddl)['state'],'VERIFIED')
        db=p.PostgresPersistence('postgresql://unused',connect=lambda dsn:ReadOnlyConnection(version=2,digest=digest))
        with self.assertRaisesRegex(p.PostgresContractError,'VERSION_MISMATCH'):db.verify_migration('orders',1,ddl)
        self.assertEqual(db.verify_migration('orders',1,ddl,allow_newer=True)['current_version'],2)
if __name__=='__main__':unittest.main()
