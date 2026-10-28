"""Runtime configuration, protected references and actual database admission."""
from __future__ import annotations
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import uuid
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]


def strict_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_json_key')
        result[key]=value
    return result


def read_secret(reference):
    if not isinstance(reference,str) or not reference.startswith('file:/'):raise ValueError('protected_secret_reference_required')
    from app.models.identity import read_protected_file
    return read_protected_file(reference[5:])

def load_config(path,profile,*,bootstrap=False,unit=None):
    if profile not in ('local-sandbox','demo-test','portfolio-demo'):raise ValueError('explicit_profile_required')
    if not path:raise ValueError('profile_file_required')
    file=Path(path)
    if not file.is_file() or file.is_symlink():raise ValueError('profile_file_required')
    value=json.loads(file.read_text(),object_pairs_hook=strict_object)
    allowed={'format','profile','providers','environment_id','merchant_id','machine_principal_id','release_id','origin','proxy_peer','database_ref','identity_receipt_key_ref','simulator_callback_key_ref','workload_credential_ref','totp_root','renderer_socket','assets','seed_credentials_ref','disposable_proof_file','migration_database_ref','backup_database_ref','test_accounts_ref','backup_age_recipient','age_binary','pg_dump_binary','pg_restore_binary','replacement_identity_inputs','mail'}
    if type(value) is not dict or set(value)-allowed:raise ValueError('closed_profile_schema_required')
    if value.get('format')!='sme.portfolio-profile/1' or value.get('profile')!=profile:raise ValueError('profile_identity_mismatch')
    if value.get('providers')!={'mode':'deterministic','outbound_network':False}:raise ValueError('deterministic_providers_required')
    if any(os.environ.get(k) for k in ('STRIPE_SECRET_KEY','EASYPOST_API_KEY','SES_ACCESS_KEY')):raise ValueError('live_provider_credentials_denied')
    required={'environment_id','merchant_id','machine_principal_id','release_id','database_ref','simulator_callback_key_ref','workload_credential_ref'}
    if unit=='serve-demo' or (unit is None and 'origin' in value):required|={'origin','proxy_peer','identity_receipt_key_ref','totp_root','renderer_socket'}
    if not required<=set(value):raise ValueError('profile_declarations_incomplete')
    def declared(text):return isinstance(text,str) and re.fullmatch(r'\$\{SME_[A-Z0-9_]+\}',text) is not None
    def expand(item):
        if type(item) is dict:return {k:expand(v) for k,v in item.items()}
        if type(item) is list:return [expand(v) for v in item]
        if declared(item):
            if bootstrap:return item
            name=item[2:-1]
            if not os.environ.get(name):raise ValueError('required_runtime_binding_missing')
            return os.environ[name]
        return item
    if unit in ('worker-demo','worker'):
        value={k:v for k,v in value.items() if k not in ('origin','proxy_peer','identity_receipt_key_ref','totp_root','renderer_socket','seed_credentials_ref','test_accounts_ref')}
    value=expand(value)
    for key in ('environment_id','merchant_id','machine_principal_id'):
        if bootstrap and declared(value[key]):continue
        if type(value[key]) is not str or str(uuid.UUID(value[key]))!=value[key]:raise ValueError('canonical_instance_identity_required')
    for key in ('database_ref','migration_database_ref','backup_database_ref','identity_receipt_key_ref','simulator_callback_key_ref','workload_credential_ref','seed_credentials_ref','test_accounts_ref'):
        if key not in value:continue
        ref=value[key]
        if bootstrap and declared(ref):continue
        if not isinstance(ref,str) or not ref.startswith('file:/') or '\x00' in ref or '..' in Path(ref[5:]).parts:raise ValueError('protected_secret_reference_required')
    if 'origin' in value and not (bootstrap and declared(value['origin'])):
        origin=urlsplit(value['origin'])
        if origin.scheme!='https' or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment or value['origin'].endswith('/'):raise ValueError('exact_https_origin_required')
    if 'proxy_peer' in value:
        peer=ipaddress.ip_address(value['proxy_peer'])
        if not peer.is_private or peer.is_unspecified:raise ValueError('fixed_private_proxy_peer_required')
    if profile!='demo-test' and 'disposable_proof_file' in value:raise ValueError('disposable_proof_only_test_profile')
    for key in ('age_binary','pg_dump_binary','pg_restore_binary'):
        if key in value and not (bootstrap and declared(value[key])):
            binary=Path(value[key])
            if not binary.is_absolute() or binary.is_symlink() or not binary.is_file() or not os.access(binary,os.X_OK):raise ValueError('absolute_maintenance_binary_required')
    if 'replacement_identity_inputs' in value:
        paths=value['replacement_identity_inputs']
        if type(paths) is not list or not paths or any(type(item) is not str or not Path(item).is_absolute() or Path(item).is_symlink() for item in paths):raise ValueError('private_replacement_identity_manifest_required')
    if 'mail' in value:
        from app.models.notifications.mail_config import validate_mail_config
        validate_mail_config(value['mail'],bootstrap=bootstrap)
    if bootstrap:return value
    if 'assets' not in value:value['assets']=json.loads((ROOT/'config/assets.json').read_text(),object_pairs_hook=strict_object)
    if sys.version_info[:3]!=(3,12,15):raise ValueError('python_3_12_15_required')
    if unit=='serve-demo':
        for key in ('identity_receipt_key_ref','simulator_callback_key_ref','workload_credential_ref'):read_secret(value[key])
        root=Path(value['totp_root'])
        if root.is_symlink() or not root.is_dir() or root.stat().st_mode&0o077:raise ValueError('protected_totp_root_required')
    elif unit in ('worker-demo','worker'):
        # Never resolve web identity/origin/MFA secrets in the worker process.
        for key in ('simulator_callback_key_ref','workload_credential_ref'):read_secret(value[key])
    return value

def resolve_database(config):return read_secret(config['database_ref']).strip()

def guarded_test_context(config,run_id):
    proof=config.get('disposable_proof_file');environment=config.get('environment_id')
    if config.get('profile')!='demo-test' or not proof or not run_id:raise ValueError('explicit_disposable_test_context_required')
    from app.models.disposable import DisposableResourceGuard
    guard=DisposableResourceGuard(proof,run_id=run_id,environment_id=environment,profile='demo-test');guard.attest_dsn(resolve_database(config));return guard

def inspect_database_connection(connection,*,include_instance=True):
    identity=connection.execute("""SELECT current_database() AS database,session_user AS session_role,current_user AS role,
      (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS database_oid,
      (SELECT datdba::bigint FROM pg_database WHERE datname=current_database()) AS database_owner_oid,
      (SELECT oid::bigint FROM pg_roles WHERE rolname=current_user) AS role_oid,
      system_identifier::text AS cluster_id,host(inet_server_addr()) AS server_address,
      inet_server_port() AS server_port,(SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user) AS dangerous_role,
      has_database_privilege(current_user,current_database(),'CREATE') AS database_create,
      EXISTS(SELECT 1 FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND (has_schema_privilege(current_user,n.oid,'CREATE') OR pg_has_role(current_user,n.nspowner,'MEMBER'))) AS schema_ddl,
      EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND pg_has_role(current_user,c.relowner,'MEMBER')) AS object_owner,
      EXISTS(SELECT 1 FROM pg_roles r WHERE (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls) AND pg_has_role(current_user,r.oid,'MEMBER')) AS dangerous_membership,
      (SELECT oid::bigint FROM pg_namespace WHERE nspname='sme_test_control') AS control_schema_oid
      FROM pg_control_system()""").fetchone()
    if not identity:raise ValueError('actual_database_identity_required')
    identity=dict(identity)
    if not include_instance:identity['instance']=None
    elif connection.execute("SELECT to_regclass('application.instance') AS instance").fetchone()['instance'] is None:identity['instance']=None
    else:
        row=connection.execute('SELECT environment_id,merchant_id,profile FROM application.instance WHERE singleton=1').fetchone()
        identity['instance']={key:str(value) if isinstance(value,uuid.UUID) else value for key,value in dict(row).items()} if row else None
    return identity

def observe_database(dsn,*,include_instance=True):
    import psycopg
    from psycopg.rows import dict_row
    try:
        with psycopg.connect(dsn,row_factory=dict_row,autocommit=False,connect_timeout=5) as connection:return inspect_database_connection(connection,include_instance=include_instance)
    except ValueError:raise
    except Exception:raise ValueError('database_binding_inspection_unavailable') from None

def verify_database_instance(binding,config,*,allow_new=False):
    expected={key:config[key] for key in ('environment_id','merchant_id','profile')}
    if binding.get('instance') is None:
        if not allow_new:raise ValueError('existing_database_instance_required')
    elif binding['instance']!=expected:raise ValueError('database_environment_binding_mismatch')
    if not binding.get('database_oid') or not binding.get('cluster_id') or not binding.get('server_address') or not binding.get('server_port'):raise ValueError('actual_database_identity_required')
    return binding

def verify_same_database(expected,actual):
    fields=('database','database_oid','cluster_id','server_address','server_port','control_schema_oid')
    if any(expected.get(key)!=actual.get(key) for key in fields):raise ValueError('maintenance_or_backup_target_mismatch')
    return actual

def validate_runtime_binding(config):
    binding=verify_database_instance(observe_database(resolve_database(config)),config)
    if config['profile']=='demo-test':
        guard=guarded_test_context(config,__import__('json').loads(Path(config['disposable_proof_file']).read_text())['run_id'])
        if binding['database_oid']!=guard.proof.database_oid:raise ValueError('disposable_runtime_identity_mismatch')
        return dict(binding,qualification='DISPOSABLE_OWNING_ROLE_EXCEPTION_NOT_HOSTED_LEAST_PRIVILEGE')
    if binding['dangerous_role'] or binding['dangerous_membership'] or binding['database_owner_oid']==binding['role_oid'] or binding['database_create'] or binding['schema_ddl'] or binding['object_owner']:raise ValueError('runtime_role_has_ddl_authority')
    return dict(binding,qualification='OBSERVED_NONOWNING_NO_DDL_RUNTIME')

def resolve_migration_binding(config):
    runtime=verify_database_instance(observe_database(resolve_database(config),include_instance=False),config,allow_new=True)
    reference=config.get('migration_database_ref')
    if not reference:
        if config['profile']!='demo-test':raise ValueError('distinct_maintenance_database_reference_required')
        proof=json.loads(Path(config['disposable_proof_file']).read_text());guard=guarded_test_context(config,proof['run_id'])
        if runtime['database_oid']!=guard.proof.database_oid:raise ValueError('disposable_migration_identity_mismatch')
        return resolve_database(config),dict(runtime,qualification='EXPLICIT_DISPOSABLE_OWNER_MIGRATION_EXCEPTION')
    dsn=read_secret(reference).strip();maintenance=verify_database_instance(verify_same_database(runtime,observe_database(dsn)),config,allow_new=True)
    if maintenance['session_role']==runtime['session_role']:raise ValueError('distinct_maintenance_login_required')
    if maintenance['dangerous_role'] or maintenance['dangerous_membership']:raise ValueError('scoped_nonsuperuser_maintenance_role_required')
    return dsn,dict(maintenance,runtime_role=runtime['role'],qualification='ACTUAL_DISTINCT_MAINTENANCE_SAME_TARGET')

def grant_runtime_access(config,migration_dsn,runtime_role):
    """Private infrastructure grants; never ownership/CREATE or arbitrary namespaces."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    schemas={'application','catalog','pricing','inventory','cart','checkout','payments','orders','returns','shipping','tax','notifications','events','integrations','demo_identity','public'}
    changed=[]
    with psycopg.connect(migration_dsn,row_factory=dict_row,autocommit=False,connect_timeout=5) as connection:
        binding=verify_database_instance(inspect_database_connection(connection),config)
        for schema in sorted(schemas):
            rows=connection.execute('SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN (\'r\',\'p\',\'v\',\'m\',\'S\') ORDER BY c.relname',(schema,)).fetchall()
            if not rows:continue
            connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(runtime_role)))
            connection.execute(sql.SQL('REVOKE CREATE ON SCHEMA {} FROM {}').format(sql.Identifier(schema),sql.Identifier(runtime_role)))
            if schema=='public':connection.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            for row in rows:
                name=row['relname']
                if schema=='public' and not name.startswith('commerce_'):continue
                if row['relkind']=='S':privileges='USAGE,SELECT';kind='SEQUENCE'
                elif name in ('commerce_schema_registry','commerce_migration_history'):privileges='SELECT';kind='TABLE'
                else:privileges='SELECT,INSERT,UPDATE,DELETE';kind='TABLE'
                connection.execute(sql.SQL('GRANT '+privileges+' ON '+kind+' {}.{} TO {}').format(sql.Identifier(schema),sql.Identifier(name),sql.Identifier(runtime_role)));changed.append(schema+'.'+name)
        connection.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(binding['database']),sql.Identifier(runtime_role)))
    return {'objects_granted':len(changed),'schemas':sorted(schemas),'ownership_or_create_granted':False}
