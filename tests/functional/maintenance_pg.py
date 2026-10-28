"""Explicit encrypted backup and fresh held restore against a guarded local fixture.

Requires private SME_MAINTENANCE_* inputs; creates only a new UUID target.
No existing database is reset, swapped, or reopened.
"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import uuid
import pytest
from app.config import guarded_test_context,load_config,read_secret,resolve_database
from app.controllers import maintenance
from app.controllers.setup import create_disposable_fixture,private_file
from app.models.store import Store

pytestmark=pytest.mark.native


def test_actual_encrypted_backup_restores_only_into_fresh_held_target():
    required=('SME_MAINTENANCE_CONFIG','SME_MAINTENANCE_ADMIN_FILE','SME_MAINTENANCE_STATE','SME_MAINTENANCE_AGE','SME_MAINTENANCE_AGE_KEYGEN','SME_MAINTENANCE_PG_DUMP','SME_MAINTENANCE_PG_RESTORE')
    if any(not os.environ.get(key) for key in required):pytest.fail('explicit private maintenance fixture inputs required')
    state=Path(os.environ['SME_MAINTENANCE_STATE']);assert state.resolve()==state and state.is_dir() and state.stat().st_mode&0o777==0o700
    config=load_config(os.environ['SME_MAINTENANCE_CONFIG'],'demo-test');proof=json.loads(Path(config['disposable_proof_file']).read_text());source_guard=guarded_test_context(config,proof['run_id']);source_dsn=resolve_database(config)
    key=state/'backup-key';subprocess.run([os.environ['SME_MAINTENANCE_AGE_KEYGEN'],'-o',str(key)],check=True,capture_output=True);key.chmod(0o600)
    recipient=subprocess.run([os.environ['SME_MAINTENANCE_AGE_KEYGEN'],'-y',str(key)],check=True,capture_output=True,text=True).stdout.strip()
    config.update(age_binary=os.environ['SME_MAINTENANCE_AGE'],pg_dump_binary=os.environ['SME_MAINTENANCE_PG_DUMP'],pg_restore_binary=os.environ['SME_MAINTENANCE_PG_RESTORE'],backup_age_recipient=recipient)
    with source_guard.connect_verified(source_dsn) as connection:before=connection.execute("SELECT count(*) n FROM integrations.money_ledger WHERE kind IN ('capture','refund')").fetchone()['n']
    backup=state/'backup.age';manifest=maintenance.backup_database(SimpleNamespace(backup_file=str(backup)),config)
    assert backup.stat().st_mode&0o777==0o600 and backup.stat().st_size>0
    assert manifest['ciphertext_sha256']==hashlib.sha256(backup.read_bytes()).hexdigest()
    assert manifest['actual_database_binding']['database_oid']==source_guard.proof.database_oid
    raw=state/'inspection.dump'
    try:
        subprocess.run([config['age_binary'],'--decrypt','--identity',str(key),'--output',str(raw),str(backup)],check=True,capture_output=True)
        assert hashlib.sha256(raw.read_bytes()).hexdigest()==manifest['plaintext_custom_archive_sha256']
        listing=subprocess.run([config['pg_restore_binary'],'--list',str(raw)],check=True,capture_output=True,text=True).stdout
        assert 'sme_test_control' not in listing and 'application instance' in listing
    finally:raw.unlink(missing_ok=True)
    run=str(uuid.uuid4());environment=str(uuid.uuid4());replacement_dir=state/'replacement';replacement_dir.mkdir(mode=0o700)
    fixture=create_disposable_fixture(read_secret('file:'+os.environ['SME_MAINTENANCE_ADMIN_FILE']).strip(),replacement_dir/'database',run,environment)
    replacement=dict(config,environment_id=environment,machine_principal_id=str(uuid.uuid4()),database_ref='file:'+str(replacement_dir/'database/runtime-dsn'),disposable_proof_file=fixture['proof_file'])
    replacement_file=replacement_dir/'profile.json';private_file(replacement_file,replacement)
    independent=state/'independent-observation.json';private_file(independent,{'environment_id':config['environment_id'],'observed_simulator_money_rows':before,'scope':'OBSERVATION_ONLY_NOT_RECONCILIATION'})
    args=SimpleNamespace(command='restore-demo',backup_file=str(backup),replacement_config=str(replacement_file),independent_simulator_evidence=str(independent),restore_key_file=str(key),run_id=run)
    source=Store(source_dsn,profile='demo-test',config=config,maintenance=True)
    try:
        with pytest.raises(ValueError,match='independent_owner_reconciliation_required_before_swap'):maintenance.restore_database(args,config,source)
    finally:source.close()
    receipt=json.loads((replacement_dir/'restore-stage.json').read_text());assert receipt['state']=='RESTORED_RECOVERY_HOLD' and receipt['swap']=='NOT_PERFORMED'
    guard=guarded_test_context(replacement,run)
    with guard.connect_verified(fixture['dsn']) as connection:
        assert connection.execute('SELECT state FROM application.release_state WHERE id=1').fetchone()['state']=='RECOVERY_HOLD'
        assert connection.execute('SELECT environment_id FROM application.instance WHERE singleton=1').fetchone()['environment_id']==environment
        assert connection.execute("SELECT count(*) n FROM integrations.money_ledger WHERE kind IN ('capture','refund')").fetchone()['n']==before
    with pytest.raises((PermissionError,ValueError)):Store(fixture['dsn'],profile='demo-test',config=replacement)
    with source_guard.connect_verified(source_dsn) as connection:
        assert connection.execute("SELECT count(*) n FROM integrations.money_ledger WHERE kind IN ('capture','refund')").fetchone()['n']==before
        assert connection.execute('SELECT environment_id FROM application.instance WHERE singleton=1').fetchone()['environment_id']==config['environment_id']
