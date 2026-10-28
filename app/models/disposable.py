from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qsl, unquote, urlsplit
import uuid


class DisposableResourceGuard:
    """One source-owned guard shared by Operations and destructive fixtures."""
    IDENTITY_SQL = '''SELECT current_database() AS database, current_user AS role,
      (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS database_oid,
      (SELECT datdba::bigint FROM pg_database WHERE datname=current_database()) AS database_owner_oid,
      (SELECT oid::bigint FROM pg_roles WHERE rolname=current_user) AS role_oid,
      (SELECT system_identifier::text FROM pg_control_system()) AS cluster_id,
      host(inet_server_addr()) AS server_address, inet_server_port() AS server_port,
      (SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
       FROM pg_roles WHERE rolname=current_user) AS dangerous_role'''
    MARKER_SQL = '''SELECT profile,run_id::text,environment_id::text,cluster_id,
      database_name,role_name,database_oid::bigint,role_oid::bigint,marker_nonce,
      provisioned_at FROM sme_test_control.disposable_fixture WHERE singleton=1'''
    CONTROL_SQL = '''SELECT r.rolname AS owner_name,
      has_schema_privilege(current_user,n.oid,'CREATE') AS can_create
      FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner
      WHERE n.nspname='sme_test_control' '''
    OTHER_DB_SQL = '''SELECT datname FROM pg_database WHERE NOT datistemplate
      AND datname<>current_database() AND
      (datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user)
       OR has_database_privilege(current_user,oid,'CREATE'))'''

    def __init__(self, proof_file, *, run_id, environment_id, profile):
        if profile != 'demo-test':
            raise DisposableTargetDenied('TEST_PROFILE_REQUIRED')
        self.run_id=self._uuid(run_id);self.environment_id=self._uuid(environment_id)
        self.proof=self._load_proof(proof_file)

    def _load_proof(self, value):
        path=Path(value)
        if not path.is_absolute():raise DisposableTargetDenied('ABSOLUTE_PROOF_PATH_REQUIRED')
        try:
            # Refuse symlinks in every supplied path component, not just its leaf.
            current=Path(path.anchor)
            for part in path.parts[1:]:
                current=current/part
                if current.is_symlink():raise DisposableTargetDenied('PROOF_SYMLINK_DENIED')
            flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
            fd=os.open(path,flags)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.getuid() or info.st_size>16384:
                    raise DisposableTargetDenied('PROTECTED_PREEXISTING_PROOF_REQUIRED')
                raw=os.read(fd,16385)
            finally:os.close(fd)
            data=json.loads(raw,object_pairs_hook=self._unique)
        except DisposableTargetDenied:raise
        except Exception:raise DisposableTargetDenied('PREEXISTING_PROOF_UNAVAILABLE') from None
        fields={'format','profile','run_id','environment_id','database','role','cluster_id','database_oid','role_oid','marker_nonce','endpoint','provisioned_at','provisioner'}
        if not isinstance(data,dict) or set(data)!=fields:raise DisposableTargetDenied('CLOSED_PROOF_SCHEMA_REQUIRED')
        if data['format']!='sme.disposable-fixture/1' or data['profile']!='demo-test' or data['provisioner']!='operations.new-disposable-fixture/1':raise DisposableTargetDenied('INDEPENDENT_FIXTURE_RECEIPT_REQUIRED')
        if data['run_id']!=self.run_id or data['environment_id']!=self.environment_id:raise DisposableTargetDenied('PROOF_RUN_ENVIRONMENT_MISMATCH')
        # The complete UUID is the resource suffix: no shared names or substring.
        expected='sme_test_'+uuid.UUID(self.run_id).hex
        if data['database']!=expected or data['role']!=expected:raise DisposableTargetDenied('UNIQUE_DATABASE_AND_ROLE_REQUIRED')
        if not isinstance(data['cluster_id'],str) or not re.fullmatch('[0-9]{10,24}',data['cluster_id']):raise DisposableTargetDenied('CLUSTER_ID_REQUIRED')
        if any(type(data[name]) is not int or data[name]<1 for name in ('database_oid','role_oid')):raise DisposableTargetDenied('EXACT_RESOURCE_OIDS_REQUIRED')
        if not isinstance(data['marker_nonce'],str) or not re.fullmatch('[0-9a-f]{64}',data['marker_nonce']):raise DisposableTargetDenied('INDEPENDENT_MARKER_NONCE_REQUIRED')
        try:
            issued=datetime.fromisoformat(data['provisioned_at'])
            if issued.tzinfo is None or issued>datetime.now(timezone.utc):raise ValueError
        except Exception:raise DisposableTargetDenied('PREEXISTING_PROVISION_TIME_REQUIRED') from None
        self.provisioned_at=issued
        endpoint=data['endpoint']
        if not isinstance(endpoint,dict) or set(endpoint)!={'host','connection_port','server_address','server_port'}:raise DisposableTargetDenied('EXACT_ENDPOINT_REQUIRED')
        if not isinstance(endpoint['host'],str) or not endpoint['host'] or ',' in endpoint['host']:raise DisposableTargetDenied('SINGLE_ENDPOINT_REQUIRED')
        if any(type(endpoint[n]) is not int or not 1<=endpoint[n]<=65535 for n in ('connection_port','server_port')):raise DisposableTargetDenied('EXACT_PORT_REQUIRED')
        try:ipaddress.ip_address(endpoint['server_address'])
        except Exception:raise DisposableTargetDenied('NUMERIC_SERVER_ADDRESS_REQUIRED') from None
        return DisposableTargetProof(self.run_id,self.environment_id,data['database'],data['role'],data['cluster_id'],data['database_oid'],data['role_oid'],data['marker_nonce'],endpoint['host'],endpoint['connection_port'],endpoint['server_address'],endpoint['server_port'],hashlib.sha256(raw).hexdigest())

    @staticmethod
    def _one(connection, sql):
        cursor=connection.execute(sql)
        row=cursor.fetchone()
        if row is None:raise DisposableTargetDenied('REQUIRED_TARGET_PROOF_MISSING')
        if isinstance(row,dict):return row
        names=[item.name if hasattr(item,'name') else item[0] for item in cursor.description]
        return dict(zip(names,row))

    @staticmethod
    def _unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise DisposableTargetDenied('DUPLICATE_PROOF_FIELD')
            result[key]=value
        return result

    @staticmethod
    def _uuid(value):
        try:
            parsed=uuid.UUID(value)
            if str(parsed)!=value:raise ValueError
            return value
        except Exception:raise DisposableTargetDenied('CANONICAL_RUN_ENVIRONMENT_UUID_REQUIRED') from None

    def attest_dsn(self, dsn, *, connect=None):
        connection=self.connect_verified(dsn,connect=connect)
        try:return self.verify(connection,dsn)
        finally:connection.close()

    def connect_verified(self, dsn, *, connect=None):
        self.validate_dsn(dsn)
        if connect is None:
            import psycopg
            from psycopg.rows import dict_row
            connection=psycopg.connect(dsn,row_factory=dict_row,autocommit=False,connect_timeout=5)
        else:connection=connect(dsn)
        try:self.verify(connection,dsn)
        except BaseException:
            connection.close()
            raise
        return connection

    @classmethod
    def from_environment(cls, environment=None):
        env=os.environ if environment is None else environment
        required=('SME_DISPOSABLE_PROOF_FILE','SME_TEST_RUN_ID','SME_TEST_ENVIRONMENT_ID','SME_TEST_PROFILE')
        if any(not env.get(name) for name in required):
            raise DisposableTargetDenied('EXPLICIT_DISPOSABLE_FIXTURE_REQUIRED')
        return cls(env['SME_DISPOSABLE_PROOF_FILE'],run_id=env['SME_TEST_RUN_ID'],environment_id=env['SME_TEST_ENVIRONMENT_ID'],profile=env['SME_TEST_PROFILE'])

    def reset_owned_schemas(self, connection, dsn):
        """Reset only this proven disposable DB; preserve bootstrap-owned control."""
        if getattr(connection,'autocommit',None) is not False:raise DisposableTargetDenied('ATOMIC_RESET_TRANSACTION_REQUIRED')
        self.verify(connection,dsn)
        connection.execute('SELECT pg_advisory_xact_lock(%s)',(self.proof.database_oid,))
        self.verify(connection,dsn)
        cursor=connection.execute("SELECT n.nspname AS name,r.rolname AS owner FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname NOT IN ('information_schema','sme_test_control') ORDER BY n.nspname")
        rows=cursor.fetchall();names=[]
        for row in rows:
            name,owner=(row['name'],row['owner']) if isinstance(row,dict) else row
            if owner!=self.proof.role and not (name=='public' and owner=='pg_database_owner'):raise DisposableTargetDenied('FOREIGN_SCHEMA_RESET_DENIED')
            if not isinstance(name,str) or not name or len(name.encode())>63 or '\x00' in name:raise DisposableTargetDenied('INVALID_SCHEMA_IDENTITY')
            names.append(name)
        # No mutation occurs until every protected identity and schema is checked.
        for name in names:connection.execute('DROP SCHEMA "'+name.replace('"','""')+'" CASCADE')
        connection.execute('CREATE SCHEMA public')
        return {'state':'DISPOSABLE_SCHEMAS_RESET','run_id':self.run_id,'environment_id':self.environment_id,'schema_count':len(names),'proof_sha256':self.proof.receipt_sha256}
    def validate_dsn(self, dsn):
        try:
            uri=urlsplit(dsn)
            if uri.scheme not in ('postgresql','postgres') or uri.fragment or not uri.username or not uri.hostname:raise ValueError
            query=parse_qsl(uri.query,keep_blank_values=True,strict_parsing=True)
            if len({k for k,v in query})!=len(query) or any(k not in ('sslmode','application_name','connect_timeout') for k,v in query):raise ValueError
            params=dict(query)
            if 'sslmode' in params and params['sslmode'] not in ('disable','require','verify-ca','verify-full'):raise ValueError
            if 'connect_timeout' in params and params['connect_timeout'] not in ('1','2','3','4','5','10'):raise ValueError
            database=unquote(uri.path[1:]) if uri.path.startswith('/') else ''
            role=unquote(uri.username)
            if (database,role,uri.hostname,uri.port or 5432)!=(self.proof.database,self.proof.role,self.proof.host,self.proof.connection_port):raise ValueError
        except Exception:raise DisposableTargetDenied('EXACT_PARSED_DSN_IDENTITY_REQUIRED') from None
        return True

    def verify(self, connection, dsn):
        """Read-only verification; missing proof is never created or repaired."""
        self.validate_dsn(dsn)
        p=self.proof
        try:
            identity=self._one(connection,self.IDENTITY_SQL)
            expected={'database':p.database,'role':p.role,'database_oid':p.database_oid,'role_oid':p.role_oid,'database_owner_oid':p.role_oid,'cluster_id':p.cluster_id,'server_address':p.server_address,'server_port':p.server_port,'dangerous_role':False}
            if any(identity.get(k)!=v for k,v in expected.items()):raise DisposableTargetDenied('CONNECTED_TARGET_IDENTITY_MISMATCH')
            control=self._one(connection,self.CONTROL_SQL)
            if control['owner_name']==p.role or control['can_create'] is not False:raise DisposableTargetDenied('INDEPENDENT_CONTROL_OWNER_REQUIRED')
            marker=self._one(connection,self.MARKER_SQL)
            expected_marker={'profile':'demo-test','run_id':p.run_id,'environment_id':p.environment_id,'cluster_id':p.cluster_id,'database_name':p.database,'role_name':p.role,'database_oid':p.database_oid,'role_oid':p.role_oid,'marker_nonce':p.marker_nonce}
            if any(marker.get(k)!=v for k,v in expected_marker.items()):raise DisposableTargetDenied('PREEXISTING_MARKER_MISMATCH')
            if marker.get('provisioned_at')!=self.provisioned_at:raise DisposableTargetDenied('PREEXISTING_MARKER_TIME_MISMATCH')
            if connection.execute(self.OTHER_DB_SQL).fetchone() is not None:raise DisposableTargetDenied('ROLE_HAS_OTHER_DATABASE_DDL_AUTHORITY')
        except DisposableTargetDenied:raise
        except Exception:raise DisposableTargetDenied('TARGET_PROOF_QUERY_FAILED') from None
        return {'profile':'demo-test','run_id':p.run_id,'environment_id':p.environment_id,'database':p.database,'role':p.role,'cluster_id':p.cluster_id,'proof_sha256':p.receipt_sha256,'state':'DISPOSABLE_VERIFIED'}

class DisposableTargetDenied(RuntimeError):
    """Safe reason codes contain neither credentials nor the supplied DSN."""

@dataclass(frozen=True)
class DisposableTargetProof:
    run_id: str
    environment_id: str
    database: str
    role: str
    cluster_id: str
    database_oid: int
    role_oid: int
    marker_nonce: str
    host: str
    connection_port: int
    server_address: str
    server_port: int
    receipt_sha256: str
