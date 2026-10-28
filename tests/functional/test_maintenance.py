"""Maintenance staging controls preserve source data and held replacement order."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.controllers import maintenance


def test_incomplete_owner_inspection_stops_reset_before_new_target(monkeypatch):
    source=SimpleNamespace(recovery=SimpleNamespace(unresolved_snapshot=lambda:{'complete':False,'unresolved':0}))
    monkeypatch.setattr(maintenance,'replacement_target',lambda *_:pytest.fail('incomplete scan admitted replacement'))
    with pytest.raises(ValueError,match='complete_unresolved_family_inspection_required'):maintenance.reset_database(SimpleNamespace(),{},source)


def test_reset_native_seed_precedes_guarded_hold(monkeypatch):
    calls=[];guard=object();cfg={'profile':'demo-test','environment_id':'replacement'}
    families={'application','purchases','payments','refunds','cancellations','fulfillment','returns','tax','notifications','events','audit'}
    source=SimpleNamespace(recovery=SimpleNamespace(unresolved_snapshot=lambda:{'complete':True,'coverage':list(families),'failures':[],'unresolved':0}))
    def hold(**kwargs):assert kwargs=={'guard':guard};calls.append('hold');return {'state':'RECOVERY_HOLD'}
    target=SimpleNamespace(migrate=lambda:calls.append('migrate'),seed_demo=lambda **_:calls.append('seed') or {'state':'COMPLETE'},recovery=SimpleNamespace(start_restore_reconciliation=hold),close=lambda:calls.append('close'))
    monkeypatch.setattr(maintenance,'replacement_target',lambda *_:(cfg,guard));monkeypatch.setattr(maintenance,'resolve_database',lambda _:'opaque');monkeypatch.setattr(maintenance,'Store',lambda *_,**__:target);monkeypatch.setattr(maintenance,'provision_private',lambda *_:calls.append('provision'));monkeypatch.setattr(maintenance,'private_seed_actor',lambda *_:calls.append('mfa') or {});monkeypatch.setattr(maintenance,'stage_receipt',lambda *_:calls.append('receipt'))
    with pytest.raises(ValueError,match='new_target_operator_switch_required'):maintenance.reset_database(SimpleNamespace(),{'replacement_identity_inputs':['/private/input']},source)
    assert calls==['migrate','provision','mfa','seed','hold','receipt','close']


def test_backup_refuses_existing_ciphertext_without_opening_database(tmp_path,monkeypatch):
    path=tmp_path/'backup.age';path.write_bytes(b'preserved')
    monkeypatch.setattr(maintenance,'read_secret',lambda _:pytest.fail('existing output opened database'))
    with pytest.raises(ValueError,match='new_private_backup_destination_required'):maintenance.backup_database(SimpleNamespace(backup_file=str(path)),{})
    assert path.read_bytes()==b'preserved'


def test_backup_child_environment_keeps_password_out_of_command_arguments(monkeypatch):
    monkeypatch.setenv('PGSERVICE','must-not-inherit');monkeypatch.setenv('PGPASSWORD','must-not-inherit')
    environment=maintenance.private_pg_environment('postgresql://scoped:private-value@127.0.0.1:55439/scoped')
    assert environment['PGPASSWORD']=='private-value' and environment['PGDATABASE']=='scoped' and 'PGSERVICE' not in environment


def test_held_stage_receipt_is_private_and_exclusive(tmp_path):
    tmp_path.chmod(0o700);args=SimpleNamespace(command='restore-demo',replacement_config=str(tmp_path/'profile.json'))
    result={'state':'RESTORED_RECOVERY_HOLD','swap':'NOT_PERFORMED'}
    assert maintenance.stage_receipt(args,result)==result
    path=tmp_path/'restore-stage.json';assert path.stat().st_mode&0o777==0o600 and json.loads(path.read_text())==result
    with pytest.raises(FileExistsError):maintenance.stage_receipt(args,result)
