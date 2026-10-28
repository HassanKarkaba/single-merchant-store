from __future__ import annotations
import contextlib
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid


class OwnerDeliveryPort:
    """Private, guarded delivery over Persistence-owned owner-filtered metadata."""

    def __init__(self, database, owner, authority_guard, audit_verifier=None):
        self.db, self.owner = database, database._owner(owner)
        if not callable(authority_guard):
            raise PostgresContractError('DELIVERY_AUTHORITY_REQUIRED')
        self.authority_guard, self.audit_verifier = authority_guard, audit_verifier

    def _audit(self, tx, event_id):
        row=tx.fetchone('SELECT *,lease_until>clock_timestamp() AS lease_active FROM commerce_audit_staging WHERE owner=%s AND event_id=%s FOR UPDATE',(self.owner,event_id))
        if row is None:raise PostgresContractError('AUDIT_FACT_NOT_FOUND')
        return row

    @staticmethod
    def _bounds(batch,lease):
        if type(batch)is not int or not 1<=batch<=100 or type(lease)is not int or not 1<=lease<=30:raise PostgresContractError('BOUNDED_DELIVERY_CLAIM_REQUIRED')
    @staticmethod
    def _digest(payload):
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()

    def _publication(self, tx, event_id, topic):
        rows=tx.fetchall('SELECT *,lease_until>clock_timestamp() AS lease_active FROM commerce_outbox WHERE owner=%s AND message_key=%s'+(' AND topic=%s' if topic is not None else '')+' ORDER BY id FOR UPDATE',(self.owner,event_id,topic) if topic is not None else (self.owner,event_id))
        if len(rows)!=1:raise PostgresContractError('PUBLICATION_FACT_MISSING_OR_AMBIGUOUS')
        return rows[0]

    @contextlib.contextmanager
    def _transaction(self, operation, resource):
        def guard(tx):
            if self.authority_guard(tx,operation,resource) is not True:raise PostgresContractError('DELIVERY_AUTHORITY_DENIED')
            return True
        with self.db.transaction(self.owner,authority_guard=guard) as tx:
            record=tx.fetchone("SELECT r.version,r.digest AS registry_digest,h.digest FROM commerce_schema_registry r JOIN commerce_migration_history h ON h.owner=r.owner AND h.version=1 WHERE r.owner='persistence_delivery'")
            if not record or record['version']!=1 or any(record[k]!=hashlib.sha256(self.db.DELIVERY_DDL.encode()).hexdigest() for k in ('digest','registry_digest')):raise PostgresContractError('DELIVERY_MIGRATION_REQUIRED')
            yield tx

    def ack_audit(self, event_id, fence, receipt):
        with self._transaction('ack_audit',event_id) as tx:
            row=self._audit(tx,event_id)
            if row['state']=='RECORDED':
                if row['receipt']!=receipt:raise PostgresContractError('AUDIT_RECEIPT_CONFLICT')
                return row['receipt']
            if row['lease_id']!=fence or row['lease_active'] is not True:raise PostgresContractError('CURRENT_AUDIT_LEASE_REQUIRED')
            if not callable(self.audit_verifier):raise PostgresContractError('ACTUAL_AUDIT_RECEIPT_VERIFIER_REQUIRED')
            actual=self.audit_verifier(tx,self.owner,event_id,row['payload'],receipt)
            fields={'owner','event_id','payload_digest','receipt_id','state'}
            if not isinstance(actual,dict) or set(actual)!=fields or actual['owner']!=self.owner or actual['event_id']!=event_id or actual['payload_digest']!=self._digest(row['payload']) or not isinstance(actual['receipt_id'],str) or not actual['receipt_id'] or actual['state']!='DEMO_RECORDED':raise PostgresContractError('ACTUAL_DEMO_AUDIT_RECEIPT_REQUIRED')
            tx.execute("UPDATE commerce_audit_staging SET state='RECORDED',receipt=%s::jsonb,lease_until=NULL WHERE owner=%s AND event_id=%s",(json.dumps(actual),self.owner,event_id))
            return actual

    def ack_publication(self, event_id, fence, *, topic=None):
        with self._transaction('ack_publication',event_id) as tx:
            row=self._publication(tx,event_id,topic)
            if row['state']=='DELIVERED':
                if row['lease_id']!=fence:raise PostgresContractError('PUBLICATION_RECEIPT_CONFLICT')
                return {'owner':self.owner,'event_id':event_id,'topic':row['topic'],'state':'DELIVERED'}
            if row['state']!='CLAIMED' or row['lease_id']!=fence or row['lease_active'] is not True:raise PostgresContractError('CURRENT_PUBLICATION_LEASE_REQUIRED')
            tx.execute("UPDATE commerce_outbox SET state='DELIVERED',lease_until=NULL WHERE id=%s",(row['id'],))
            return {'owner':self.owner,'event_id':event_id,'topic':row['topic'],'state':'DELIVERED'}

    def claim_audit(self, batch=100, lease=30):
        return self.claim_audits(batch,lease)

    def claim_audits(self, batch=100, lease=30):
        self._bounds(batch,lease)
        with self._transaction('claim_audits',self.owner) as tx:
            rows=tx.fetchall("SELECT * FROM commerce_audit_staging WHERE owner=%s AND state<>'RECORDED' AND attempts<8 AND (lease_until IS NULL OR lease_until<=clock_timestamp()) ORDER BY created_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED",(self.owner,batch))
            result=[]
            for row in rows:
                fence=str(uuid.uuid4());tx.execute('UPDATE commerce_audit_staging SET lease_id=%s,lease_until=clock_timestamp()+(%s*interval \'1 second\'),attempts=attempts+1 WHERE owner=%s AND event_id=%s',(fence,lease,self.owner,row['event_id']))
                result.append({'owner':self.owner,'event_id':row['event_id'],'payload':row['payload'],'payload_digest':self._digest(row['payload']),'fence':fence})
            return result

    def claim_publications(self, batch=100, lease=30):
        self._bounds(batch,lease)
        with self._transaction('claim_publications',self.owner) as tx:
            rows=tx.fetchall("SELECT * FROM commerce_outbox WHERE owner=%s AND (state IN ('PENDING','UNKNOWN') OR (state='CLAIMED' AND lease_until<=clock_timestamp())) AND attempts<8 AND available_at<=clock_timestamp() ORDER BY available_at,id LIMIT %s FOR UPDATE SKIP LOCKED",(self.owner,batch))
            result=[]
            for row in rows:
                fence=str(uuid.uuid4());tx.execute("UPDATE commerce_outbox SET state='CLAIMED',lease_id=%s,lease_until=clock_timestamp()+(%s*interval '1 second'),attempts=attempts+1 WHERE id=%s",(fence,lease,row['id']))
                result.append({'owner':self.owner,'event_id':row['message_key'],'topic':row['topic'],'publication_id':row['id'],'payload':row['payload'],'event':row['payload'],'created_at':row['created_at'],'fence':fence})
            return result

    def defer_audit(self, event_id, fence, reason):
        if not isinstance(reason,str) or not 1<=len(reason)<=300:raise PostgresContractError('BOUNDED_DELIVERY_REASON_REQUIRED')
        with self._transaction('defer_audit',event_id) as tx:
            row=self._audit(tx,event_id)
            if row['state']=='RECORDED':raise PostgresContractError('AUDIT_ALREADY_RECORDED')
            if row['lease_id']!=fence or row['lease_active'] is not True:raise PostgresContractError('CURRENT_AUDIT_LEASE_REQUIRED')
            tx.execute("UPDATE commerce_audit_staging SET state='UNKNOWN',lease_until=NULL WHERE owner=%s AND event_id=%s",(self.owner,event_id))
            return {'owner':self.owner,'event_id':event_id,'state':'UNKNOWN'}

    def defer_publication(self, event_id, fence, reason, *, topic=None):
        if not isinstance(reason,str) or not 1<=len(reason)<=300:raise PostgresContractError('BOUNDED_DELIVERY_REASON_REQUIRED')
        with self._transaction('defer_publication',event_id) as tx:
            row=self._publication(tx,event_id,topic)
            if row['state']!='CLAIMED' or row['lease_id']!=fence or row['lease_active'] is not True:raise PostgresContractError('CURRENT_PUBLICATION_LEASE_REQUIRED')
            state='DEAD' if row['attempts']>=8 else 'PENDING';tx.execute("UPDATE commerce_outbox SET state=%s,lease_until=NULL,available_at=clock_timestamp()+interval '1 second' WHERE id=%s",(state,row['id']))
            return {'owner':self.owner,'event_id':event_id,'topic':row['topic'],'state':state}

    def initialize(self):
        return self.db.verify_migration('persistence_delivery',1,self.db.DELIVERY_DDL)

    def inspect_audit(self, event_id):
        with self._transaction('inspect_audit',event_id) as tx:
            row=self._audit(tx,event_id)
            return {'owner':self.owner,'event_id':event_id,'payload':row['payload'],'payload_digest':self._digest(row['payload']),'state':row['state'],'receipt':row['receipt'],'attempts':row['attempts'],'lease_id':row['lease_id'],'lease_active':row['lease_active'] is True,'lease_until':row['lease_until']}

    def inspect_publication(self, event_id, *, topic=None):
        with self._transaction('inspect_publication',event_id) as tx:
            row=self._publication(tx,event_id,topic)
            return {'owner':self.owner,'event_id':event_id,'topic':row['topic'],'publication_id':row['id'],'payload':row['payload'],'event':row['payload'],'state':row['state'],'attempts':row['attempts'],'lease_id':row['lease_id'],'lease_active':row['lease_active'] is True,'lease_until':row['lease_until']}

    def safety_snapshot(self):
        with self._transaction('safety_snapshot',self.owner) as tx:
            outbox={row['state']:row['count'] for row in tx.fetchall('SELECT state,count(*)::bigint AS count FROM commerce_outbox WHERE owner=%s GROUP BY state',(self.owner,))}
            audit={row['state']:row['count'] for row in tx.fetchall('SELECT state,count(*)::bigint AS count FROM commerce_audit_staging WHERE owner=%s GROUP BY state',(self.owner,))}
            return {'owner':self.owner,'complete':True,'unresolved':sum(count for state,count in outbox.items() if state!='DELIVERED')+sum(count for state,count in audit.items() if state!='RECORDED'),'outbox':outbox,'audit':audit,'failures':[]}

class OwnerTransaction:
    """Trusted server owner port; one connection, schema fence and authority fence.

    This port is never a browser interface. Commands, owner writes, staged audit
    facts and outbox messages share its PostgreSQL transaction. No network effect
    or implicit retry belongs in this scope.
    """
    def __init__(self, persistence, owner, connection, command_id=None, request_digest=None):
        self._persistence, self.owner, self._connection = persistence, owner, connection
        self.command_id, self.request_digest = command_id, request_digest
        self.active, self.replayed, self._completed = True, False, False
        self.receipt = None
        self._precommit = []

    def _check(self):
        if not self.active:
            raise PostgresContractError('TRANSACTION_CLOSED')

    def _enroll(self):
        if self.command_id is None:
            return
        self._connection.execute('INSERT INTO commerce_commands(owner,command_id,request_digest,state) VALUES (%s,%s,%s,\'PENDING\') ON CONFLICT DO NOTHING', (self.owner, self.command_id, self.request_digest))
        row = self._connection.execute('SELECT * FROM commerce_commands WHERE owner=%s AND command_id=%s FOR UPDATE', (self.owner, self.command_id)).fetchone()
        if row['request_digest'] != self.request_digest:
            raise PostgresContractError('IDEMPOTENCY_CONFLICT')
        if row['state'] == 'COMPLETED':
            self.replayed, self._completed, self.receipt = True, True, row
        elif row['state'] != 'PENDING':
            raise PostgresContractError('COMMAND_UNRESOLVED')

    def add_precommit(self, check):
        self._check()
        if not callable(check):
            raise PostgresContractError('PRECOMMIT_CHECK_REQUIRED')
        self._precommit.append(check)

    def command_complete(self, *args):
        """Complete enrolled command(result), or explicit command(id,digest,result)."""
        self._check()
        if len(args) == 1:
            command_id, request_digest, result = self.command_id, self.request_digest, args[0]
        elif len(args) == 3:
            command_id, request_digest, result = args
        else:
            raise TypeError('command_complete expects result or command_id, request_digest, result')
        self._persistence._command_identity(command_id, request_digest)
        encoded = self._persistence._json(result)
        if self.command_id is not None and (command_id, request_digest) != (self.command_id, self.request_digest):
            raise PostgresContractError('COMMAND_SCOPE_MISMATCH')
        row = self._connection.execute('SELECT * FROM commerce_commands WHERE owner=%s AND command_id=%s FOR UPDATE', (self.owner, command_id)).fetchone()
        if row and row['request_digest'] != request_digest:
            raise PostgresContractError('IDEMPOTENCY_CONFLICT')
        if row and row['state'] == 'COMPLETED':
            if self._persistence._json(row['result']) != encoded:
                raise PostgresContractError('REPLAY_RESULT_IMMUTABLE')
            self.receipt = row
        elif row:
            self.receipt = self._connection.execute('UPDATE commerce_commands SET state=\'COMPLETED\',result=%s::jsonb,completed_at=clock_timestamp() WHERE owner=%s AND command_id=%s RETURNING *', (encoded, self.owner, command_id)).fetchone()
        else:
            self.receipt = self._connection.execute('INSERT INTO commerce_commands(owner,command_id,request_digest,state,result,completed_at) VALUES (%s,%s,%s,\'COMPLETED\',%s::jsonb,clock_timestamp()) RETURNING *', (self.owner, command_id, request_digest, encoded)).fetchone()
        self._completed = True
        return self.receipt

    def command_lookup(self, command_id=None):
        row = self.command_record(command_id)
        return row['result'] if row is not None and row['state'] == 'COMPLETED' else None

    def command_record(self, command_id=None):
        self._check()
        command_id = self.command_id if command_id is None else command_id
        self._persistence._identity(command_id, 'COMMAND_ID')
        return self._connection.execute('SELECT * FROM commerce_commands WHERE owner=%s AND command_id=%s', (self.owner, command_id)).fetchone()

    def execute(self, query, params=()):
        self._check()
        if self.replayed:
            raise PostgresContractError('COMPLETED_COMMAND_READ_RECEIPT_ONLY')
        self._persistence._owner_statement(query)
        return self._connection.execute(query, params)

    def fetchall(self, query, params=()):
        return self.execute(query, params).fetchall()

    def fetchone(self, query, params=()):
        return self.execute(query, params).fetchone()

    def stage_audit(self, event_id, payload):
        self._check()
        if self.replayed:
            raise PostgresContractError('COMPLETED_COMMAND_READ_RECEIPT_ONLY')
        self._persistence._identity(event_id, 'EVENT_ID')
        encoded = self._persistence._json(payload)
        row = self._connection.execute('INSERT INTO commerce_audit_staging(owner,event_id,payload) VALUES (%s,%s,%s::jsonb) ON CONFLICT(owner,event_id) DO UPDATE SET payload=commerce_audit_staging.payload RETURNING payload', (self.owner, event_id, encoded)).fetchone()
        if self._persistence._json(row['payload']) != encoded:
            raise PostgresContractError('AUDIT_IDENTITY_CONFLICT')
        return {'owner': self.owner, 'event_id': event_id, 'state': 'SOURCE_ACCEPTED'}

    def stage_outbox(self, topic, key, payload):
        self._check()
        if self.replayed:
            raise PostgresContractError('COMPLETED_COMMAND_READ_RECEIPT_ONLY')
        self._persistence._identity(topic, 'TOPIC')
        self._persistence._identity(key, 'OUTBOX_KEY')
        encoded = self._persistence._json(payload)
        row = self._connection.execute('INSERT INTO commerce_outbox(owner,topic,message_key,payload) VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT(owner,topic,message_key) DO UPDATE SET payload=commerce_outbox.payload RETURNING *', (self.owner, topic, key, encoded)).fetchone()
        if self._persistence._json(row['payload']) != encoded:
            raise PostgresContractError('OUTBOX_IDENTITY_CONFLICT')
        return row

class PostgresCommitUnknown(RuntimeError):
    """Commit acknowledgement was not observed. Reconcile durable owner identity."""
    def __init__(self, owner, command_id=None):
        super().__init__('POSTGRES_COMMIT_OUTCOME_UNKNOWN')
        self.owner, self.command_id, self.certainty = owner, command_id, 'UNKNOWN'

class PostgresContractError(ValueError):
    pass

class PostgresPersistence:
    """Additive pg-commerce/1 profile; existing sqlite-local stays unchanged.

    Application domain SQL is trusted owner code, never browser text. Configure
    database roles separately for runtime and migration. The injected connector
    is a test/qualified-host seam, not a second provider or automatic fallback.
    """
    CORE_DDL = '''
    CREATE TABLE IF NOT EXISTS commerce_schema_registry(
      owner text PRIMARY KEY,version bigint NOT NULL CHECK(version>0),
      digest text NOT NULL,applied_at timestamptz NOT NULL DEFAULT clock_timestamp());
    CREATE TABLE IF NOT EXISTS commerce_migration_history(
      owner text NOT NULL,version bigint NOT NULL CHECK(version>0),digest text NOT NULL,
      applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(owner,version));
    CREATE TABLE IF NOT EXISTS commerce_commands(
      owner text NOT NULL,command_id text NOT NULL,request_digest text NOT NULL,
      state text NOT NULL CHECK(state IN ('PENDING','COMPLETED')),result jsonb,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),completed_at timestamptz,
      PRIMARY KEY(owner,command_id),CHECK(state<>'COMPLETED' OR completed_at IS NOT NULL));
    CREATE TABLE IF NOT EXISTS commerce_outbox(
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,owner text NOT NULL,
      topic text NOT NULL,message_key text NOT NULL,payload jsonb NOT NULL,
      state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','CLAIMED','DELIVERED','UNKNOWN','DEAD')),
      attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      lease_id text,lease_until timestamptz,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(owner,topic,message_key));
    CREATE INDEX IF NOT EXISTS commerce_outbox_dispatch ON commerce_outbox(state,available_at,id);
    CREATE TABLE IF NOT EXISTS commerce_audit_staging(
      owner text NOT NULL,event_id text NOT NULL,payload jsonb NOT NULL,
      state text NOT NULL DEFAULT 'SOURCE_ACCEPTED' CHECK(state IN ('SOURCE_ACCEPTED','RECORDED','UNKNOWN')),
      receipt jsonb,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(owner,event_id));
    '''
    DELIVERY_DDL = """ALTER TABLE commerce_audit_staging ADD COLUMN IF NOT EXISTS lease_id text;
    ALTER TABLE commerce_audit_staging ADD COLUMN IF NOT EXISTS lease_until timestamptz;
    ALTER TABLE commerce_audit_staging ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0);"""
    SCHEMA_LOCK = 75349432909101

    def __init__(self, dsn, *, connect=None, required_schema_version=1, migration_dsn=None, statement_timeout_ms=10000, lock_timeout_ms=5000):
        if not isinstance(dsn, str) or not dsn:
            raise PostgresContractError('DSN_REQUIRED')
        if type(required_schema_version) is not int or required_schema_version < 1:
            raise PostgresContractError('SCHEMA_VERSION_REQUIRED')
        for value in (statement_timeout_ms, lock_timeout_ms):
            if type(value) is not int or not 1 <= value <= 60000:
                raise PostgresContractError('INVALID_TIMEOUT')
        self._dsn, self._migration_dsn, self._connector = dsn, migration_dsn or dsn, connect
        self.required_schema_version = required_schema_version
        self.statement_timeout_ms, self.lock_timeout_ms = statement_timeout_ms, lock_timeout_ms
        self._initialized, self._initialize_lock = False, threading.Lock()

    def _command_identity(self, command_id, request_digest):
        self._identity(command_id, 'COMMAND_ID')
        if not isinstance(request_digest, str) or not re.fullmatch('[0-9a-f]{64}', request_digest):
            raise PostgresContractError('REQUEST_DIGEST_REQUIRED')

    def _connect(self, *, migration=False):
        dsn = self._migration_dsn if migration else self._dsn
        if self._connector is not None:
            connection = self._connector(dsn)
        else:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as error:
                raise PostgresContractError('PSYCOPG3_REQUIRED') from error
            connection = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5, autocommit=False)
        try:
            if connection.execute('SHOW server_encoding').fetchone()['server_encoding'] != 'UTF8':
                raise PostgresContractError('UTF8_DATABASE_REQUIRED')
        except BaseException:
            connection.close()
            raise
        return connection

    def _identity(self, value, field):
        if not isinstance(value, str) or not 1 <= len(value.encode('utf-8')) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise PostgresContractError('INVALID_' + field)
        return value

    def _json(self, value):
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > 1048576:
            raise PostgresContractError('JSON_TOO_LARGE')
        return encoded

    def _owner(self, owner):
        if not isinstance(owner, str) or not re.fullmatch('[a-z][a-z0-9_]{0,62}', owner):
            raise PostgresContractError('INVALID_OWNER')
        return owner

    def _owner_statement(self, query):
        # Psycopg's extended protocol rejects multi-command prepared statements.
        # Lexical admission additionally excludes owner transaction/schema control.
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 1048576:
            raise PostgresContractError('INVALID_OWNER_SQL')
        stripped = re.sub(r'/\*.*?\*/|--[^\n]*', ' ', query, flags=re.S).lstrip()
        first = re.match('[A-Za-z]+', stripped)
        if first is None or first.group().upper() not in {'SELECT','INSERT','UPDATE','DELETE','WITH'}:
            raise PostgresContractError('OWNER_TRANSACTION_CONTROL_DENIED')
        # No semicolon is needed by any owner statement. This conservative bound
        # also rejects a semicolon inside quoted data: send values as parameters.
        if ';' in query:
            raise PostgresContractError('MULTIPLE_OR_TERMINATED_OWNER_SQL_DENIED')

    def attest_disposable(self, guard):
        """Private operator verification; never expose the configured DSN."""
        import inspect
        expected = Path(__file__).resolve().parents[1] / 'disposable.py'
        if type(guard).__name__ != 'DisposableResourceGuard' or Path(inspect.getfile(type(guard))).resolve() != expected.resolve():
            raise PostgresContractError('SOURCE_OWNED_DISPOSABLE_GUARD_REQUIRED')
        return type(guard).attest_dsn(guard, self._dsn)

    def initialize(self):
        """Read-only startup check; an operator must call migrate_core first."""
        connection = self._connect()
        try:
            row=connection.execute("SELECT to_regclass('public.commerce_schema_registry') AS registry,to_regclass('public.commerce_migration_history') AS history,to_regclass('public.commerce_commands') AS commands,to_regclass('public.commerce_outbox') AS outbox,to_regclass('public.commerce_audit_staging') AS audit").fetchone()
            if row is None or any(value is None for value in row.values()):
                raise PostgresContractError('CORE_SCHEMA_MISSING_RUN_EXPLICIT_MIGRATION')
            receipt=connection.execute("SELECT version,digest FROM public.commerce_schema_registry WHERE owner='persistence_core'").fetchone()
            if receipt is None or receipt['version']!=1 or receipt['digest']!=hashlib.sha256(self.CORE_DDL.encode('utf-8')).hexdigest():
                raise PostgresContractError('CORE_MIGRATION_RECEIPT_MISMATCH')
            self._initialized=True
            return {'owner':'persistence_core','version':1,'state':'VERIFIED'}
        finally:
            connection.close()

    def lookup_command(self, owner, command_id):
        """Missing receipt is unresolved absence, never proof of no remote effect."""
        self._owner(owner)
        self._identity(command_id, 'COMMAND_ID')
        connection = self._connect()
        try:
            return connection.execute('SELECT * FROM commerce_commands WHERE owner=%s AND command_id=%s', (owner, command_id)).fetchone()
        finally:
            connection.close()

    def migrate(self, owner, version, ddl):
        self._owner(owner)
        if type(version) is not int or version < 1 or not isinstance(ddl, str) or not ddl.strip():
            raise PostgresContractError('INVALID_MIGRATION')
        # Owner DDL is a retained trusted artifact. Transaction control is forbidden.
        if re.search(r'(^|;)\s*(BEGIN|COMMIT|ROLLBACK|END|PREPARE|SAVEPOINT|RELEASE)\b', ddl, flags=re.I):
            raise PostgresContractError('MIGRATION_TRANSACTION_CONTROL_DENIED')
        identity = hashlib.sha256(ddl.encode('utf-8')).hexdigest()
        self.migrate_core()
        connection = self._connect(migration=True)
        try:
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.SCHEMA_LOCK,))
            previous = connection.execute('SELECT version,digest FROM commerce_schema_registry WHERE owner=%s FOR UPDATE', (owner,)).fetchone()
            history = connection.execute('SELECT digest FROM commerce_migration_history WHERE owner=%s AND version=%s', (owner, version)).fetchone()
            if history:
                if history['digest'] != identity:
                    raise PostgresContractError('MIGRATION_IDENTITY_CONFLICT')
                connection.rollback()
                return {'owner':owner,'version':version,'digest':identity,'state':'APPLIED','replayed':True}
            expected = 1 if previous is None else previous['version'] + 1
            if version != expected:
                raise PostgresContractError('MIGRATION_SEQUENCE_REQUIRED')
            connection.execute(ddl)
            connection.execute('INSERT INTO commerce_migration_history(owner,version,digest) VALUES (%s,%s,%s)', (owner,version,identity))
            connection.execute('INSERT INTO commerce_schema_registry(owner,version,digest) VALUES (%s,%s,%s) ON CONFLICT(owner) DO UPDATE SET version=EXCLUDED.version,digest=EXCLUDED.digest,applied_at=clock_timestamp()', (owner,version,identity))
            try:
                connection.commit()
            except Exception as error:
                raise PostgresCommitUnknown('migration:' + owner) from error
            return {'owner':owner,'version':version,'digest':identity,'state':'APPLIED','replayed':False}
        except PostgresCommitUnknown:
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate_core(self):
        """Explicit operator migration; never called by constructor/readiness."""
        with self._initialize_lock:
            connection = self._connect(migration=True)
            try:
                connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.SCHEMA_LOCK,))
                connection.execute(self.CORE_DDL)
                identity=hashlib.sha256(self.CORE_DDL.encode('utf-8')).hexdigest()
                existing=connection.execute("SELECT version,digest FROM public.commerce_schema_registry WHERE owner='persistence_core' FOR UPDATE").fetchone()
                if existing is not None and (existing['version']!=1 or existing['digest']!=identity):
                    raise PostgresContractError('CORE_MIGRATION_IDENTITY_CONFLICT')
                connection.execute("INSERT INTO public.commerce_migration_history(owner,version,digest) VALUES ('persistence_core',1,%s) ON CONFLICT DO NOTHING",(identity,))
                connection.execute("INSERT INTO public.commerce_schema_registry(owner,version,digest) VALUES ('persistence_core',1,%s) ON CONFLICT DO NOTHING",(identity,))
                try:connection.commit()
                except Exception as error:raise PostgresCommitUnknown('migration:persistence_core') from error
                self._initialized = True
            except BaseException:
                try:
                    connection.rollback()
                finally:
                    connection.close()
                raise
            else:
                connection.close()

    def migrate_delivery(self):
        return self.migrate('persistence_delivery',1,self.DELIVERY_DDL)

    def owner_delivery(self, owner, authority_guard, *, audit_verifier=None):
        return OwnerDeliveryPort(self,owner,authority_guard,audit_verifier)

    def register_migration(self, owner, version, ddl):
        return self.migrate(owner, version, ddl)

    @contextlib.contextmanager
    def transaction(self, owner, *, command_id=None, request_digest=None, authority_guard=None, deadline_monotonic=None):
        self._owner(owner)
        if not self._initialized:
            self.initialize()
        if command_id is not None:
            self._command_identity(command_id, request_digest)
        elif request_digest is not None:
            raise PostgresContractError('COMMAND_ID_REQUIRED')
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise PostgresContractError('ADMISSION_DEADLINE_EXPIRED')
        connection, tx = self._connect(), None
        committing = False
        try:
            connection.execute('SELECT set_config(\'statement_timeout\',%s,true),set_config(\'lock_timeout\',%s,true)', (str(self.statement_timeout_ms),str(self.lock_timeout_ms)))
            connection.execute('SELECT pg_advisory_xact_lock_shared(%s)', (self.SCHEMA_LOCK,))
            schema = connection.execute('SELECT version,digest FROM commerce_schema_registry WHERE owner=%s FOR SHARE', (owner,)).fetchone()
            if schema is None or schema['version'] < self.required_schema_version:
                raise PostgresContractError('SCHEMA_ADMISSION_CLOSED')
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise PostgresContractError('ADMISSION_DEADLINE_EXPIRED')
            tx = OwnerTransaction(self, owner, connection, command_id, request_digest)
            if authority_guard is not None:
                admission = authority_guard.admit if hasattr(authority_guard, 'admit') else authority_guard
                if admission(tx) is not True:
                    raise PostgresContractError('AUTHORITY_ADMISSION_DENIED')
                tx.add_precommit(admission)
            tx._enroll()
            yield tx
            if command_id is not None and not tx._completed:
                raise PostgresContractError('COMMAND_RESULT_REQUIRED_BEFORE_COMMIT')
            if not tx.replayed:
                for check in tx._precommit:
                    if check(tx) is not True:
                        raise PostgresContractError('PRECOMMIT_AUTHORITY_DENIED')
            committing = True
            try:
                connection.commit()
            except Exception as error:
                raise PostgresCommitUnknown(owner, command_id) from error
        except BaseException:
            if not committing:
                try:
                    connection.rollback()
                except Exception as error:
                    raise PostgresCommitUnknown(owner, command_id) from error
            raise
        finally:
            if tx is not None:
                tx.active = False
            connection.close()


    def verify_migration(self, owner, version, ddl, *, allow_newer=False):
        """Read-only exact owner version and immutable artifact receipt check."""
        self._owner(owner)
        if type(version) is not int or version<1 or not isinstance(ddl,str) or not ddl.strip():
            raise PostgresContractError('INVALID_MIGRATION')
        self.initialize()
        identity=hashlib.sha256(ddl.encode('utf-8')).hexdigest()
        connection=self._connect()
        try:
            registry=connection.execute('SELECT version,digest FROM public.commerce_schema_registry WHERE owner=%s',(owner,)).fetchone()
            history=connection.execute('SELECT digest FROM public.commerce_migration_history WHERE owner=%s AND version=%s',(owner,version)).fetchone()
            if registry is None or history is None or registry['version']<version or history['digest']!=identity:
                raise PostgresContractError('OWNER_MIGRATION_NOT_READY')
            if registry['version']!=version and allow_newer is not True:
                raise PostgresContractError('OWNER_SCHEMA_VERSION_MISMATCH')
            if registry['version']==version and registry['digest']!=identity:
                raise PostgresContractError('OWNER_MIGRATION_IDENTITY_CONFLICT')
            return {'owner':owner,'version':version,'digest':identity,'current_version':registry['version'],'state':'VERIFIED'}
        finally:connection.close()
