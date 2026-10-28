"""Encrypted backups and private replacement staging; public admission stays held."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from app.config import (guarded_test_context, inspect_database_connection, load_config,
                        observe_database, read_secret, resolve_database, strict_object,
                        verify_database_instance, verify_same_database)
from app.controllers.http import wire_value
from app.controllers.setup import authenticate_seed, private_file
from app.models.store import Store


def encode_manifest(value):
    return json.dumps(wire_value(value),ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')


def stage_receipt(args,result):
    path=Path(args.replacement_config).absolute().parent
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode&0o077:raise ValueError('private_replacement_configuration_directory_required')
    name='restore-stage.json' if args.command=='restore-demo' else 'reset-stage.json'
    private_file(path/name,wire_value(result))
    return result


def private_seed_actor(config,store):
    record=json.loads(read_secret(config['seed_credentials_ref']),object_pairs_hook=strict_object)
    return authenticate_seed(store,{'username':record['username'],'password':read_secret(record['password_ref']),'totp_secret':read_secret(record['totp_secret_ref'])})


def provision_private(path,config,store):
    value=json.loads(read_secret('file:'+str(Path(path).absolute())),object_pairs_hook=strict_object)
    fields={'environment','principal_id','username','roles','password_ref','totp_secret_ref','factor_version','expected_identity_revision'}
    if type(value) is not dict or set(value)!=fields or value['environment']!=config['profile']:raise ValueError('closed_provision_schema_required')
    return store.identity.provision_account(principal_id=value['principal_id'],username=value['username'],roles=value['roles'],password=read_secret(value['password_ref']),factor_ref=value['totp_secret_ref'],factor_version=value['factor_version'],expected_identity_revision=value['expected_identity_revision'])

def backup_database(args,config,store=None):
    # Protected custom-format dump; secrets occur only in child process environment.
    import time
    started=time.monotonic();path=Path(args.backup_file).absolute()
    if path.exists() or path.is_symlink() or not path.parent.is_dir():raise ValueError('new_private_backup_destination_required')
    if path.parent.stat().st_mode&0o077:raise ValueError('private_backup_directory_required')
    if not config.get('backup_age_recipient') or not config.get('age_binary') or not config.get('pg_dump_binary'):raise ValueError('qualified_encrypted_backup_binding_required')
    raw=path.with_suffix(path.suffix+'.restricted-plaintext');fd=os.open(raw,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
    dsn=read_secret(config.get('backup_database_ref',config['database_ref'])).strip()
    try:
        import psycopg
        from psycopg.rows import dict_row
        runtime_binding=verify_database_instance(observe_database(resolve_database(config)),config)
        with psycopg.connect(dsn,row_factory=dict_row,autocommit=False,connect_timeout=5) as connection:
            connection.isolation_level=psycopg.IsolationLevel.REPEATABLE_READ;connection.read_only=True
            actual_binding=verify_database_instance(verify_same_database(runtime_binding,inspect_database_connection(connection)),config)
            snapshot=connection.execute('SELECT pg_export_snapshot() AS snapshot').fetchone()['snapshot']
            subprocess.run([config['pg_dump_binary'],'--format=custom','--exclude-schema=sme_test_control','--snapshot',snapshot,'--file',str(raw)],check=True,env=private_pg_environment(dsn),capture_output=True)
        subprocess.run([config['age_binary'],'-r',config['backup_age_recipient'],'-o',str(path),str(raw)],check=True,capture_output=True)
        os.chmod(path,0o600)
        if path.stat().st_size==0:raise ValueError('empty_backup')
        manifest={'format':'sme.encrypted-backup/1','environment_id':config['environment_id'],'profile':config['profile'],'release_id':config['release_id'],'timestamp':datetime.now(timezone.utc),'ciphertext_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'plaintext_custom_archive_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'bytes':path.stat().st_size,'elapsed_ms':round((time.monotonic()-started)*1000),'merchant_id':config['merchant_id'],'state':'ENCRYPTED_DUMP_CREATED_NOT_RESTORE_QUALIFIED','actual_database_binding':actual_binding,'retention':'at least seven daily plus one pre-release; no automatic deletion of unqualified/unknown copies'}
        receipt_file=path.with_suffix(path.suffix+'.receipt.json');fd=os.open(receipt_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:os.write(fd,encode_manifest(manifest))
        finally:os.close(fd)
        return manifest
    finally:raw.unlink(missing_ok=True)

def private_pg_environment(dsn):
    import psycopg
    values=psycopg.conninfo.conninfo_to_dict(dsn);environment={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    for key,target in [('host','PGHOST'),('port','PGPORT'),('dbname','PGDATABASE'),('user','PGUSER'),('password','PGPASSWORD'),('sslmode','PGSSLMODE'),('sslrootcert','PGSSLROOTCERT')]:
        if key in values:environment[target]=values[key]
    return environment

def replacement_target(args,config):
    replacement=load_config(args.replacement_config,'demo-test',unit='restore-demo')
    if replacement['environment_id']==config['environment_id'] or resolve_database(replacement)==resolve_database(config):raise ValueError('fresh_replacement_identity_required')
    guard=guarded_test_context(replacement,args.run_id)
    # Empty target, independently attested before the first DDL. Never drop a preexisting schema.
    with guard.connect_verified(resolve_database(replacement)) as connection:
        rows=connection.execute("SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('public','information_schema','sme_test_control') AND nspname NOT LIKE 'pg_%'").fetchall()
        if rows:raise ValueError('replacement_target_not_empty')
    return replacement,guard

def reset_database(args,config,app):
    check=getattr(app.recovery,'unresolved_snapshot',None)
    if check is None:raise ValueError('complete_unresolved_family_inspection_required')
    snapshot=check();required={'application','purchases','payments','refunds','cancellations','fulfillment','returns','tax','notifications','events','audit'}
    if type(snapshot) is not dict or snapshot.get('complete') is not True or set(snapshot.get('coverage',[]))!=required or snapshot.get('failures')!=[] or type(snapshot.get('unresolved')) is not int:raise ValueError('complete_unresolved_family_inspection_required')
    if snapshot['unresolved']!=0:raise ValueError('reset_refused_unresolved_obligations')
    replacement,guard=replacement_target(args,config)
    if not config.get('replacement_identity_inputs'):raise ValueError('private_replacement_identity_manifest_required')
    # A new target is constructed; the old target/evidence/simulator identities are never deleted.
    fresh=Store(resolve_database(replacement),profile=replacement['profile'],config=replacement,maintenance=True)
    try:
        fresh.migrate()
        for path in config['replacement_identity_inputs']:
            provision_private(path,replacement,fresh)
        result=fresh.seed_demo(actor=private_seed_actor(replacement,fresh))
        hold=fresh.recovery.start_restore_reconciliation(guard=guard)
        if hold.get('state')!='RECOVERY_HOLD':raise ValueError('native_replacement_hold_not_established')
        stage_receipt(args,{'state':'REPLACEMENT_SEEDED_RECOVERY_HOLD','hold':hold,'replacement_environment_id':replacement['environment_id'],'seed_result':result,'swap':'NOT_PERFORMED','old_environment':'PRESERVED','requires':'native all-family reconciliation, process drain/death and explicit operator switch'})
    finally:fresh.close()
    raise ValueError('new_target_operator_switch_required')

def restore_database(args,config,source_app):
    import time
    started=time.monotonic();backup=Path(args.backup_file)
    manifest_file=backup.with_suffix(backup.suffix+'.receipt.json')
    if backup.is_symlink() or not backup.is_file() or not manifest_file.is_file():raise ValueError('encrypted_backup_receipt_required')
    manifest=json.loads(manifest_file.read_text(),object_pairs_hook=strict_object)
    if manifest.get('environment_id')!=config['environment_id'] or manifest.get('ciphertext_sha256')!=hashlib.sha256(backup.read_bytes()).hexdigest():raise ValueError('backup_identity_or_digest_mismatch')
    # An operator supplies an independently surviving ledger observation, never a public HTTP fault hook.
    independent=Path(args.independent_simulator_evidence)
    if independent.is_symlink() or not independent.is_file():raise ValueError('independent_simulator_evidence_required')
    independent_sha=hashlib.sha256(independent.read_bytes()).hexdigest()
    read_secret('file:'+str(Path(args.restore_key_file).absolute()))
    replacement,guard=replacement_target(args,config)
    if not hasattr(source_app,'admit_restored_instance'):raise ValueError('native_restore_hold_and_generation_rebind_port_required')
    if not config.get('age_binary') or not config.get('pg_restore_binary'):raise ValueError('qualified_restore_tools_required')
    raw=backup.parent/('restore-'+args.run_id+'.restricted-plaintext')
    fd=os.open(raw,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
    try:
        subprocess.run([config['age_binary'],'--decrypt','--identity',args.restore_key_file,'--output',str(raw),str(backup)],check=True,capture_output=True)
        if hashlib.sha256(raw.read_bytes()).hexdigest()!=manifest.get('plaintext_custom_archive_sha256'):raise ValueError('restored_plaintext_digest_mismatch')
        with guard.connect_verified(resolve_database(replacement)):pass
        with raw.open('rb') as stream:
            subprocess.run([config['pg_restore_binary'],'--exit-on-error','--no-owner','--no-privileges','--dbname',private_pg_environment(resolve_database(replacement))['PGDATABASE']],env=private_pg_environment(resolve_database(replacement)),stdin=stream,check=True,capture_output=True)
        restored=Store(resolve_database(replacement),profile=replacement['profile'],config=replacement,maintenance=True)
        try:
            hold=restored.admit_restored_instance(source_environment_id=config['environment_id'],backup_sha256=manifest['ciphertext_sha256'],independent_evidence_sha256=independent_sha,run_id=args.run_id)
            if hold.get('state')!='RECOVERY_HOLD':raise ValueError('native_restore_hold_not_established')
        finally:restored.close()
        stage_receipt(args,{'state':'RESTORED_RECOVERY_HOLD','replacement_environment_id':replacement['environment_id'],'ciphertext_sha256':manifest['ciphertext_sha256'],'independent_simulator_evidence_sha256':independent_sha,'elapsed_ms':round((time.monotonic()-started)*1000),'hold':hold,'swap':'NOT_PERFORMED','required_next':'native independent-simulator/owner reconciliation and explicit operator acceptance'})
    finally:raw.unlink(missing_ok=True)
    raise ValueError('independent_owner_reconciliation_required_before_swap')
