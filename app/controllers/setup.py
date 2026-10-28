"""Private local setup through native owner APIs; never resets existing data."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import quote, urlsplit
import uuid

from app.config import ROOT, guarded_test_context, read_secret, strict_object


def private_directory(path):
    candidate=Path(path).expanduser().absolute()
    if candidate.is_symlink():raise ValueError('private_state_directory_required')
    candidate=candidate.resolve()
    if candidate==ROOT or ROOT in candidate.parents:raise ValueError('state_must_be_outside_project')
    candidate.mkdir(mode=0o700,parents=True,exist_ok=True)
    mode=candidate.stat()
    if not stat.S_ISDIR(mode.st_mode) or stat.S_IMODE(mode.st_mode)!=0o700 or mode.st_uid!=os.getuid():raise ValueError('private_state_directory_required')
    return candidate


def private_file(path,value):
    data=value if isinstance(value,str) else json.dumps(value,indent=2)+'\n'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    return 'file:'+str(path)


def state_directory(value=None):
    return private_directory(value or Path.home()/'.local/share/single-merchant-store')


def configure_database(*,state_dir=None):
    """Prepare private credentials for a new native local PostgreSQL cluster.

    Existing files are verified and reused so repeated configure never rotates
    an existing PostgreSQL cluster's bootstrap password. The operator starts
    PostgreSQL explicitly with initdb/pg_ctl; this function does not start it.
    """
    state=state_directory(state_dir)
    password_path=state/'postgres-password';admin_path=state/'admin-database'
    present=[p.exists() or p.is_symlink() for p in (password_path,admin_path)]
    if any(present):
        if not all(present):raise ValueError('partial_database_configuration_preserved')
        password=read_secret('file:'+str(password_path));admin=read_secret('file:'+str(admin_path))
        if admin!='postgresql://sme_fixture_admin:'+quote(password,safe='')+'@127.0.0.1:55439/postgres':raise ValueError('existing_database_configuration_mismatch')
    else:
        password=secrets.token_urlsafe(36)
        private_file(password_path,password)
        private_file(admin_path,'postgresql://sme_fixture_admin:'+quote(password,safe='')+'@127.0.0.1:55439/postgres')
    return {'state_directory':str(state),'admin_database_file':str(admin_path),'postgres_password_file':str(password_path),'postgres_data_directory':str(state/'postgres')}


def create_disposable_fixture(admin_dsn,fixture_root,run_id,environment_id,*,port=55439):
    """Birth a new UUID-scoped local database, role and independent control marker."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    run=str(uuid.UUID(run_id));environment=str(uuid.UUID(environment_id))
    if run!=run_id or environment!=environment_id:raise ValueError('canonical_fixture_uuid_required')
    declared=psycopg.conninfo.conninfo_to_dict(admin_dsn)
    if declared.get('host') not in ('127.0.0.1','::1') or int(declared.get('port','5432'))!=port:raise ValueError('explicit_loopback_admin_database_required')
    name='sme_test_'+uuid.UUID(run).hex;folder=Path(fixture_root)
    if folder.is_symlink() or folder.resolve()!=folder or folder.exists():raise ValueError('new_private_fixture_directory_required')
    password=secrets.token_urlsafe(40)
    with psycopg.connect(admin_dsn,autocommit=True,row_factory=dict_row,connect_timeout=5) as admin:
        identity=admin.execute("SELECT current_user AS role,host(inet_server_addr()) AS address,inet_server_port() AS port,current_setting('server_version_num')::int AS version,current_setting('listen_addresses') AS listen,system_identifier::text AS cluster_id FROM pg_control_system()").fetchone()
        if identity['role']!='sme_fixture_admin' or not ipaddress.ip_address(identity['address']).is_loopback or identity['port']!=port or identity['version']!=160015 or not set(identity['listen'].split(','))<={'127.0.0.1','localhost','::1'}:raise ValueError('dedicated_local_fixture_bootstrap_required')
        if admin.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone() or admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(name,)).fetchone():raise ValueError('fixture_identity_already_exists')
        folder.mkdir(mode=0o700)
        admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(name),sql.Identifier(name)))
    endpoint='['+declared['host']+']' if ':' in declared['host'] else declared['host']
    dsn='postgresql://'+quote(name,safe='')+':'+quote(password,safe='')+'@'+endpoint+':'+str(port)+'/'+quote(name,safe='')
    admin_target=psycopg.conninfo.make_conninfo(admin_dsn,dbname=name)
    with psycopg.connect(admin_target,autocommit=False,row_factory=dict_row,connect_timeout=5) as admin:
        admin.execute('CREATE SCHEMA sme_test_control AUTHORIZATION sme_fixture_admin')
        admin.execute('CREATE TABLE sme_test_control.disposable_fixture(singleton int PRIMARY KEY CHECK(singleton=1),profile text NOT NULL,run_id uuid NOT NULL,environment_id uuid NOT NULL,cluster_id text NOT NULL,database_name text NOT NULL,role_name text NOT NULL,database_oid bigint NOT NULL,role_oid bigint NOT NULL,marker_nonce text NOT NULL,provisioned_at timestamptz NOT NULL)')
        admin.execute(sql.SQL('GRANT USAGE ON SCHEMA sme_test_control TO {}').format(sql.Identifier(name)))
        admin.execute(sql.SQL('GRANT SELECT ON sme_test_control.disposable_fixture TO {}').format(sql.Identifier(name)))
        admin.execute(sql.SQL('GRANT EXECUTE ON FUNCTION pg_control_system() TO {}').format(sql.Identifier(name)))
        ids=admin.execute('SELECT (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) database_oid,(SELECT oid::bigint FROM pg_roles WHERE rolname=%s) role_oid,host(inet_server_addr()) server_address,inet_server_port() server_port,clock_timestamp() now',(name,)).fetchone()
        nonce=secrets.token_hex(32)
        admin.execute("INSERT INTO sme_test_control.disposable_fixture VALUES(1,'demo-test',%s,%s,%s,%s,%s,%s,%s,%s,%s)",(run,environment,identity['cluster_id'],name,name,ids['database_oid'],ids['role_oid'],nonce,ids['now']))
    proof={'format':'sme.disposable-fixture/1','profile':'demo-test','run_id':run,'environment_id':environment,'database':name,'role':name,'cluster_id':identity['cluster_id'],'database_oid':ids['database_oid'],'role_oid':ids['role_oid'],'marker_nonce':nonce,'endpoint':{'host':declared['host'],'connection_port':port,'server_address':ids['server_address'],'server_port':ids['server_port']},'provisioned_at':ids['now'].isoformat(),'provisioner':'operations.new-disposable-fixture/1'}
    proof_path=folder/'disposable-proof.json';private_file(proof_path,proof);private_file(folder/'runtime-dsn',dsn)
    return {'dsn':dsn,'proof_file':str(proof_path),'run_id':run,'environment_id':environment}


def authenticate_seed(app,account):
    import pyotp
    pre=app.identity.bootstrap_preauth()
    login=app.identity.authenticate(account['username'],account['password'],preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key='private-local-setup')
    if not login.public.get('challenge'):raise ValueError('native_staff_mfa_challenge_required')
    login=app.identity.verify_staff_totp(login.public['challenge']['id'],pyotp.TOTP(account['totp_secret']).now(),challenge_credential=login.challenge_credential,preauth_credential=login.preauth_credential,csrf=login.public['csrf_token'])
    if not login.credential:raise ValueError('native_staff_mfa_required')
    app.complete_authentication(login.credential)
    return app.actor(login.credential)


def initialize(*,admin_database_file,state_dir=None,origin='https://localhost:8443',database_port=55439):
    """Create and seed one new private local instance; existing state is refused."""
    from app.models.store import Store
    state=state_directory(state_dir)
    if (state/'profile.json').exists() or (state/'profile.json').is_symlink():raise ValueError('state_already_initialized')
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or parsed.username or parsed.password or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or origin.endswith('/'):raise ValueError('exact_https_origin_required')
    if database_port!=55439:raise ValueError('isolated_database_port_55439_required')
    admin=read_secret('file:'+str(Path(admin_database_file).expanduser().absolute())).strip()
    run=str(uuid.uuid4());environment=str(uuid.uuid4());merchant=str(uuid.uuid4());machine=str(uuid.uuid4())
    fixture=create_disposable_fixture(admin,state/'database',run,environment,port=database_port)
    config={'format':'sme.portfolio-profile/1','profile':'demo-test','providers':{'mode':'deterministic','outbound_network':False},'environment_id':environment,'merchant_id':merchant,'machine_principal_id':machine,'release_id':'local-mvc-1','database_ref':'file:'+str(state/'database/runtime-dsn'),'disposable_proof_file':fixture['proof_file'],'origin':origin,'proxy_peer':'127.0.0.1','assets':json.loads((ROOT/'config/assets.json').read_text(),object_pairs_hook=strict_object)}
    for key,name in [('identity_receipt_key_ref','identity-key'),('simulator_callback_key_ref','callback-key'),('workload_credential_ref','workload-key')]:config[key]=private_file(state/name,secrets.token_hex(32))
    factors=state/'totp';factors.mkdir(mode=0o700);sockets=state/'renderer';sockets.mkdir(mode=0o700)
    config.update(totp_root=str(factors),renderer_socket=str(sockets/'renderer.sock'))
    accounts={'profile':'demo-test','run_id':run};inputs=[];instructions={}
    for label in ('customerA','customerB','merchant','mfaReviewer','seedMaintainer'):
        staff=label not in ('customerA','customerB');principal=str(uuid.uuid4());username=label+'_'+uuid.UUID(run).hex[:12];password=secrets.token_urlsafe(36);password_ref=private_file(state/(label+'-password'),password)
        secret=base64.b32encode(secrets.token_bytes(20)).decode() if staff else None;factor_ref=private_file(factors/(label+'-totp'),secret) if staff else None
        record={'environment':'demo-test','principal_id':principal,'username':username,'roles':['merchant_admin'] if staff else ['customer'],'password_ref':password_ref,'totp_secret_ref':factor_ref,'factor_version':1 if staff else 0,'expected_identity_revision':0};private_file(state/(label+'-provision.json'),record);inputs.append(record)
        accounts[label]={'username':username,'password':password,**({'totp_secret':secret} if staff else {})}
        instructions[label]={'username':username,'password_file':password_ref[5:],**({'totp_secret_file':factor_ref[5:]} if staff else {})}
        if label=='seedMaintainer':config['seed_credentials_ref']=private_file(state/'seed-credentials.json',{'username':username,'password_ref':password_ref,'totp_secret_ref':factor_ref})
    config['test_accounts_ref']=private_file(state/'accounts.json',accounts)
    config['mail']=local_mail_defaults(state,origin)
    private_file(state/'profile.json',config);private_file(state/'reviewer-setup.json',instructions)
    guarded_test_context(config,run)
    app=Store(fixture['dsn'],profile='demo-test',config=config,maintenance=True)
    try:
        app.migrate()
        for record in inputs:app.identity.provision_account(principal_id=record['principal_id'],username=record['username'],roles=record['roles'],password=read_secret(record['password_ref']),factor_ref=record['totp_secret_ref'],factor_version=record['factor_version'],expected_identity_revision=0)
        now=datetime.now(timezone.utc)
        app.authority.provision_workload(merchant_id=merchant,machine_principal_id=machine,service_id='sme-worker',credential_ref=config['workload_credential_ref'],issued_at=now-timedelta(seconds=1),expires_at=now+timedelta(days=30),expected_revision=0)
        app.recovery.provision_control()
        result=app.seed_demo(actor=authenticate_seed(app,accounts['seedMaintainer']))
        if result.get('state')!='COMPLETE':raise ValueError('native_seed_did_not_complete')
    finally:app.close()
    return {'state_directory':str(state),'profile_file':str(state/'profile.json'),'run_id':run,'environment_id':environment,'reviewer_setup_file':str(state/'reviewer-setup.json'),'accounts_file':str(state/'accounts.json'),'renderer_socket':config['renderer_socket'],'origin':origin}


def local_mail_defaults(state,origin):
    from cryptography.fernet import Fernet
    from app.models.notifications.mail_config import validate_mail_config
    state=private_directory(state)
    directory=state/'mail-capture';directory.mkdir(mode=0o700,exist_ok=False)
    queue_ref=private_file(state/'mail-queue-key',Fernet.generate_key().decode('ascii'))
    password_ref=private_file(state/'mail-capture-password',secrets.token_urlsafe(32))
    return validate_mail_config({'mode':'local_capture','public_origin':origin,'from_address':'accounts@store.invalid','from_name':'Single Merchant Store','host':'127.0.0.1','port':1025,'security':'none','username':'local-capture','password_ref':password_ref,'timeout_seconds':3,'max_attempts':8,'queue_key_ref':queue_ref,'capture':{'directory':str(directory),'smtp_port':1025,'http_port':8025,'password_ref':password_ref}})


def configure_mail(profile_file):
    """Add private local mail bindings without changing an existing binding."""
    path=Path(profile_file).expanduser().absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_mode&0o777!=0o600 or path.stat().st_uid!=os.getuid():raise ValueError('protected_profile_file_required')
    before=path.read_bytes()
    if len(before)>65536:raise ValueError('bounded_profile_file_required')
    config=json.loads(before,object_pairs_hook=strict_object)
    if config.get('mail') is not None:
        from app.models.notifications.mail_config import validate_mail_config
        validate_mail_config(config['mail'])
    else:
        config['mail']=local_mail_defaults(path.parent,config['origin'])
        temporary=path.with_name(path.name+'.mail-'+secrets.token_hex(8))
        private_file(temporary,config)
        if path.read_bytes()!=before:temporary.unlink();raise ValueError('profile_changed_during_mail_configuration')
        os.replace(temporary,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return {'profile_file':str(path),'mail_mode':config['mail']['mode'],'mail_viewer':'http://127.0.0.1:'+str(config['mail']['capture']['http_port']) if config['mail']['mode']=='local_capture' else None,'mail_viewer_username':'operator' if config['mail']['mode']=='local_capture' else None,'mail_password_file':config['mail']['password_ref'][5:] if config['mail']['mode']=='local_capture' else None}
