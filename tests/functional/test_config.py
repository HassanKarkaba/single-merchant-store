"""Configuration admission and protected-reference regressions."""
import json
from pathlib import Path
import uuid
import pytest
from app import config


def profile():
    return {'format':'sme.portfolio-profile/1','profile':'demo-test','providers':{'mode':'deterministic','outbound_network':False},'environment_id':str(uuid.uuid4()),'merchant_id':str(uuid.uuid4()),'machine_principal_id':str(uuid.uuid4()),'release_id':'test','database_ref':'file:/not-mounted/database','identity_receipt_key_ref':'file:/not-mounted/identity','simulator_callback_key_ref':'file:/not-mounted/callback','workload_credential_ref':'file:/not-mounted/workload','origin':'https://store.test:8443','proxy_peer':'127.0.0.1','totp_root':'/not-mounted/totp','renderer_socket':'/not-mounted/renderer.sock'}


def save(tmp_path,value):
    path=tmp_path/'profile.json';path.write_text(json.dumps(value));return path


def test_declaration_check_does_not_resolve_secrets_or_environment(tmp_path,monkeypatch):
    value=profile();value['environment_id']='${SME_ENVIRONMENT_ID}'
    monkeypatch.setattr(config,'read_secret',lambda _:pytest.fail('declaration check resolved secret'))
    assert config.load_config(save(tmp_path,value),'demo-test',bootstrap=True)==value


def test_duplicate_and_unknown_configuration_fields_rejected(tmp_path):
    path=save(tmp_path,profile());path.write_text('{"profile":"demo-test","profile":"portfolio-demo"}')
    with pytest.raises(ValueError,match='duplicate_json_key'):config.load_config(path,'demo-test',bootstrap=True)
    value=profile();value['unexpected']=True
    with pytest.raises(ValueError,match='closed_profile_schema_required'):config.load_config(save(tmp_path,value),'demo-test',bootstrap=True)


def test_worker_does_not_resolve_web_identity_or_totp(tmp_path,monkeypatch):
    value=profile();opened=[]
    monkeypatch.setattr(config,'read_secret',lambda ref:opened.append(ref) or 'opaque')
    loaded=config.load_config(save(tmp_path,value),'demo-test',unit='worker-demo')
    assert opened==[value['simulator_callback_key_ref'],value['workload_credential_ref']]
    assert not {'origin','identity_receipt_key_ref','totp_root','renderer_socket'}&loaded.keys()


def test_secret_reference_requires_owner_only_regular_file(tmp_path):
    secret=tmp_path/'secret';secret.write_text(' value preserved\n');secret.chmod(0o600)
    assert config.read_secret('file:'+str(secret))==' value preserved\n'
    secret.chmod(0o644)
    with pytest.raises(Exception,match='protected_secret_file_required'):config.read_secret('file:'+str(secret))
    secret.chmod(0o600);link=tmp_path/'link';link.symlink_to(secret)
    with pytest.raises(Exception):config.read_secret('file:'+str(link))


@pytest.mark.parametrize('unsafe',['dangerous_role','dangerous_membership','database_create','schema_ddl','object_owner'])
def test_public_runtime_refuses_ddl_or_privileged_role(tmp_path,monkeypatch,unsafe):
    value=profile();value['profile']='portfolio-demo'
    binding={'instance':{key:value[key] for key in ('environment_id','merchant_id','profile')},'database_oid':5,'cluster_id':'cluster','server_address':'127.0.0.1','server_port':55439,'database_owner_oid':7,'role_oid':8,'dangerous_role':False,'dangerous_membership':False,'database_create':False,'schema_ddl':False,'object_owner':False};binding[unsafe]=True
    monkeypatch.setattr(config,'resolve_database',lambda _:'opaque');monkeypatch.setattr(config,'observe_database',lambda _:binding)
    with pytest.raises(ValueError,match='runtime_role_has_ddl_authority'):config.validate_runtime_binding(value)


def test_database_configuration_reuses_private_credentials_without_rotation(tmp_path):
    from app.controllers.setup import configure_database
    state=tmp_path/'state'
    first=configure_database(state_dir=state)
    before={key:Path(first[key]).read_bytes() for key in ('admin_database_file','postgres_password_file')}
    assert configure_database(state_dir=state)==first
    assert all(Path(first[key]).read_bytes()==value for key,value in before.items())
    assert all(Path(first[key]).stat().st_mode&0o777==0o600 for key in before)
    assert state.stat().st_mode&0o777==0o700


def test_setup_refuses_state_inside_project_and_partial_database_configuration(tmp_path):
    from app.controllers.setup import configure_database,private_directory
    with pytest.raises(ValueError,match='state_must_be_outside_project'):private_directory(config.ROOT)
    state=tmp_path/'state';state.mkdir(mode=0o700);password=state/'postgres-password';password.write_text('retained');password.chmod(0o600)
    with pytest.raises(ValueError,match='partial_database_configuration_preserved'):configure_database(state_dir=state)
    assert password.read_text()=='retained' and not (state/'admin-database').exists()


def test_init_does_not_follow_admin_secret_symlink_before_database_birth(tmp_path):
    from app.controllers.setup import initialize
    admin=tmp_path/'admin';admin.write_text('opaque');admin.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(admin)
    state=tmp_path/'state'
    with pytest.raises(Exception,match='secret_symlink_denied'):initialize(admin_database_file=link,state_dir=state)
    assert not (state/'database').exists() and not (state/'profile.json').exists()
