"""One-container local-demo lifecycle with persistent, independently guarded state."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import ssl
import stat
import subprocess
import sys
import time
import traceback
import urllib.request
from app.config import ROOT,guarded_test_context,load_config,read_secret,resolve_database,strict_object,validate_runtime_binding
from app.controllers.http import RendererClient
from app.controllers.local_access import inspect_local_access
from app.controllers.setup import configure_database,initialize,private_file
from app.models.store import Store

STATE=Path('/state')
ORIGIN='https://localhost:8443'


class StartupFailure(ValueError):
    pass


class StopRequested(Exception):
    pass


def atomic_json(path,value):
    temporary=path.with_name(path.name+'.write-'+str(os.getpid()))
    private_file(temporary,value)
    os.replace(temporary,path)
    directory=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)


def private_json(path):
    if path.is_symlink() or not path.is_file():raise StartupFailure('private_state_metadata_required')
    info=path.stat()
    if stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.getuid() or info.st_size>65536:raise StartupFailure('private_state_metadata_required')
    return json.loads(path.read_text(),object_pairs_hook=strict_object)


def assert_state_directory(state):
    if not state.is_absolute() or state.is_symlink() or state.resolve()!=state or not state.is_dir():raise StartupFailure('state_volume_directory_required')
    info=state.stat()
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:raise StartupFailure('state_volume_owner_or_mode_mismatch')


def inside_state(path,state):
    value=Path(path)
    if not value.is_absolute() or '..' in value.parts or state not in value.parents:raise StartupFailure('state_binding_outside_named_volume')
    current=state
    for part in value.relative_to(state).parts:
        current/=part
        if current.is_symlink():raise StartupFailure('state_symlink_denied')
    return value


def validate_bindings(config,state):
    if config['profile']!='demo-test' or config.get('origin')!=ORIGIN or config.get('proxy_peer')!='127.0.0.1':raise StartupFailure('container_profile_identity_mismatch')
    for name in ('database_ref','identity_receipt_key_ref','simulator_callback_key_ref','workload_credential_ref','seed_credentials_ref','test_accounts_ref'):
        value=config[name]
        if not value.startswith('file:'):raise StartupFailure('protected_state_reference_required')
        inside_state(value[5:],state)
    for name in ('disposable_proof_file','totp_root','renderer_socket'):inside_state(config[name],state)
    mail=config.get('mail')
    if mail:
        if mail['public_origin']!=ORIGIN:raise StartupFailure('container_mail_origin_mismatch')
        for key in ('queue_key_ref','password_ref'):
            if mail.get(key):inside_state(mail[key][5:],state)
        if mail.get('mode')=='local_capture':inside_state(mail['capture']['directory'],state)
    return config


def https_probe(state,*,storefront=True):
    certificate=state/'caddy-data/caddy/pki/authorities/local/root.crt'
    if not certificate.is_file() or certificate.is_symlink():return False
    try:
        context=ssl.create_default_context(cafile=str(certificate))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
        with opener.open(ORIGIN+'/health/ready',timeout=2) as response:
            if response.status!=200 or json.loads(response.read(65537))['data']['ready'] is not True:return False
        if storefront:
            with opener.open(ORIGIN+'/',timeout=2) as response:
                body=response.read(2097153)
                if response.status!=200 or len(body)>2097152 or b'<html' not in body.lower() or b'/assets/browser.js' not in body:return False
        return True
    except (OSError,ValueError,KeyError):return False


def healthcheck(state=STATE):
    try:
        assert_state_directory(state)
        marker=private_json(state/'container-ready.json')
        if marker.get('state')!='READY' or type(marker.get('pid')) is not int or marker['pid']<=0 or not isinstance(marker.get('boot_nonce'),str) or re.fullmatch('[0-9a-f]{32}',marker['boot_nonce']) is None:return False
        os.kill(marker['pid'],0)
        return https_probe(state)
    except (OSError,ValueError,KeyError):return False


class ContainerRuntime:
    def __init__(self,state=STATE):
        self.state=state;self.profile=state/'profile.json';self.children=[];self.lock=None;self.lock_acquired=False;self.boot_nonce=secrets.token_hex(16);self.renderer_socket=None;self.closing=False;self.phase='starting'
        self.environment=dict(os.environ,HOME=str(state),PYTHONDONTWRITEBYTECODE='1',XDG_DATA_HOME=str(state/'caddy-data'),XDG_CONFIG_HOME=str(state/'caddy-config'))

    def log(self,state,**values):
        print(json.dumps(dict(state=state,**values),ensure_ascii=False),flush=True)

    def progress(self,phase):
        self.phase=phase;atomic_json(self.state/'container-progress.json',{'phase':phase,'pid':os.getpid(),'boot_nonce':self.boot_nonce,'updated_at':int(time.time())})
        self.log('STARTING',phase=phase)

    def acquire(self):
        assert_state_directory(self.state)
        self.lock=os.open(self.state/'container-startup.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        info=os.fstat(self.lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:raise StartupFailure('private_startup_lock_required')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise StartupFailure('another_instance_owns_state_volume') from None
        self.lock_acquired=True
        ready=self.state/'container-ready.json'
        if ready.exists():
            previous=private_json(ready)
            if previous.get('state')=='READY':ready.unlink()

    def healthy_children(self):
        for name,process in self.children:
            if process.poll() is not None:raise StartupFailure('child_exited_'+name)

    def start(self,name,command,*,quiet=False):
        process=subprocess.Popen(command,cwd=ROOT,env=self.environment,stdout=subprocess.DEVNULL if quiet else None,stderr=subprocess.DEVNULL if quiet else None)
        self.children.append((name,process));return process

    def command(self,name,command,timeout=60):
        process=self.start(name,command,quiet=True);deadline=time.monotonic()+timeout
        while process.poll() is None:
            if time.monotonic()>deadline:raise StartupFailure(name+'_timed_out')
            time.sleep(.05)
        self.children.remove((name,process))
        if process.returncode:raise StartupFailure(name+'_failed_state_preserved')

    def wait(self,check,timeout,label):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.healthy_children()
            if check():return
            time.sleep(.1)
        raise StartupFailure(label+'_readiness_timeout')

    def database_ready(self):
        import psycopg
        try:
            with psycopg.connect(read_secret('file:'+str(self.state/'admin-database')).strip(),connect_timeout=1) as connection:
                row=connection.execute("SELECT current_user,current_setting('server_version_num')::int,host(inet_server_addr()),inet_server_port()").fetchone()
                return row==('sme_fixture_admin',160015,'127.0.0.1',55439)
        except psycopg.Error:return False

    def database(self):
        self.progress('database')
        data=self.state/'postgres'
        if not data.exists():
            unexpected=[p.name for p in self.state.iterdir() if p.name not in {'container-startup.lock','container-progress.json'}]
            if unexpected:raise StartupFailure('partial_initialization_without_cluster_preserved')
            configure_database(state_dir=self.state)
            self.command('initdb',['initdb','-D',str(data),'-U','sme_fixture_admin','--pwfile='+str(self.state/'postgres-password'),'--auth-local=peer','--auth-host=scram-sha-256','--encoding=UTF8','--locale=C.UTF-8'])
        elif not (data/'PG_VERSION').is_file() or (data/'PG_VERSION').read_text().strip()!='16':raise StartupFailure('partial_or_unsupported_cluster_preserved')
        if data.is_symlink() or data.stat().st_uid!=os.getuid() or stat.S_IMODE(data.stat().st_mode)!=0o700:raise StartupFailure('private_postgres_directory_required')
        if not (self.state/'admin-database').is_file() or not (self.state/'postgres-password').is_file():raise StartupFailure('cluster_bootstrap_bindings_missing_state_preserved')
        sockets=self.state/'postgres-socket';sockets.mkdir(mode=0o700,exist_ok=True)
        if sockets.is_symlink() or stat.S_IMODE(sockets.stat().st_mode)!=0o700:raise StartupFailure('private_postgres_socket_directory_required')
        self.start('postgres',['postgres','-D',str(data),'-h','127.0.0.1','-p','55439','-k',str(sockets),'-c','log_statement=none','-c','log_min_error_statement=panic','-c','log_parameter_max_length=0','-c','log_parameter_max_length_on_error=0'])
        self.wait(self.database_ready,45,'database')

    def application(self):
        self.progress('application')
        marker=self.state/'container-initialized.json'
        if not self.profile.exists():
            if (self.state/'database').exists():raise StartupFailure('partial_application_initialization_preserved')
            initialize(admin_database_file=str(self.state/'admin-database'),state_dir=self.state,origin=ORIGIN)
        config=validate_bindings(load_config(self.profile,'demo-test'),self.state)
        proof=private_json(Path(config['disposable_proof_file']));guarded_test_context(config,proof['run_id'])
        if marker.exists():
            completed=private_json(marker)
            expected={key:config[key] for key in ('environment_id','merchant_id','machine_principal_id')}
            if completed.get('format')!='store.container-state/1' or completed.get('identities')!=expected:raise StartupFailure('initialization_marker_identity_mismatch')
        validate_runtime_binding(config)
        store=Store(resolve_database(config),profile='demo-test',config=config,maintenance=True)
        try:
            store.migrate();store.initialize()
            with store.db.transaction('application') as tx:
                seed=tx.execute("SELECT state,steps FROM application.seed_receipts WHERE revision='pf-demo-six-20261028'").fetchone()
                if not seed or seed['state']!='COMPLETE' or len(seed['steps'])!=36 or any(entry.get('state')!='DONE' for entry in seed['steps'].values()):raise StartupFailure('partial_seed_initialization_preserved')
            bootstrap=inspect_local_access(self.profile,config)
            if bootstrap['state']=='STALE':self.log('WARNING',code='bootstrap_credentials_changed_use_local_access')
            renewal=store.authority.renew_expired_workload(config['merchant_id'],config['machine_principal_id'],'sme-worker',config['workload_credential_ref'],lifetime_days=30)
            store.recovery.provision_control()
            if renewal['state']=='RENEWED':self.log('WORKER_RENEWED',version=renewal['version'])
        finally:store.close()
        if not marker.exists():atomic_json(marker,{'format':'store.container-state/1','identities':{key:config[key] for key in ('environment_id','merchant_id','machine_principal_id')},'completed_at':int(time.time())})
        return config

    def services(self,config):
        self.progress('services')
        socket=Path(config['renderer_socket'])
        if socket.exists():
            info=socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():raise StartupFailure('unexpected_renderer_socket_preserved')
            # State lock is exclusive and PostgreSQL was started by this process.
            socket.unlink()
        self.start('renderer',['node',str(ROOT/'app/controllers/renderer.mjs'),'--listen',str(socket),str(ROOT/'dist/server/browser.mjs')])
        self.wait(lambda:RendererClient(str(socket)).ready(),15,'renderer')
        info=socket.lstat();self.renderer_socket=(socket,info.st_dev,info.st_ino)
        mail=config.get('mail',{})
        if mail.get('mode')=='local_capture':
            self.start('mail_capture',[sys.executable,'-m','app.controllers.mail_capture','--config',str(self.profile)])
            import socket as network
            def capture_ready():
                try:
                    for port in (mail['capture']['smtp_port'],mail['capture']['http_port']):
                        with network.create_connection(('127.0.0.1',port),timeout=.2):pass
                    return True
                except OSError:return False
            self.wait(capture_ready,10,'mail_capture')
        common=[sys.executable,str(ROOT/'manage.py')]
        self.start('worker',[*common,'worker-demo','--config',str(self.profile)])
        self.start('http',[*common,'serve-demo','--config',str(self.profile),'--port','8080'])
        self.start('caddy',['caddy','run','--config',str(ROOT/'config/Caddyfile.container'),'--adapter','caddyfile'])
        self.wait(lambda:https_probe(self.state),90,'application')
        self.healthy_children()
        atomic_json(self.state/'container-ready.json',{'state':'READY','pid':os.getpid(),'boot_nonce':self.boot_nonce,'ready_at':int(time.time())})
        self.phase='ready'
        self.log('READY',url=ORIGIN,certificate='/state/caddy-data/caddy/pki/authorities/local/root.crt',local_access='docker compose exec store python manage.py local-access --config /state/profile.json',certificate_trust='Import only the public root.crt into your browser trust store.')

    def shutdown(self):
        self.closing=True
        ready=self.state/'container-ready.json'
        if self.lock_acquired and ready.exists():
            try:
                marker=private_json(ready)
                if marker.get('pid')==os.getpid() and marker.get('boot_nonce')==self.boot_nonce:ready.unlink()
            except (OSError,ValueError):pass
        applications=[(name,process) for name,process in self.children if name!='postgres']
        databases=[process for name,process in self.children if name=='postgres']
        # All application children receive termination together. PostgreSQL has
        # a separate reserved fast-shutdown budget after clients have stopped.
        for _,process in applications:
            if process.poll() is None:process.send_signal(signal.SIGTERM)
        deadline=time.monotonic()+30
        for _,process in reversed(applications):
            try:process.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                try:process.wait(timeout=.2)
                except subprocess.TimeoutExpired:pass
        for process in databases:
            if process.poll() is None:process.send_signal(signal.SIGINT)
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        renderer_dead=all(process.poll() is not None for name,process in self.children if name=='renderer')
        if self.lock_acquired and renderer_dead and self.renderer_socket:
            path,device,inode=self.renderer_socket
            try:
                info=path.lstat()
                if stat.S_ISSOCK(info.st_mode) and info.st_uid==os.getuid() and (info.st_dev,info.st_ino)==(device,inode):path.unlink()
            except FileNotFoundError:pass
        if self.lock is not None:os.close(self.lock);self.lock=None
        self.lock_acquired=False

    def run(self):
        if platform.system()!='Linux' or ROOT!=Path('/opt/store') or os.getuid()==0:raise StartupFailure('nonroot_packaged_container_required')
        original={}
        def stop(*_):
            if not self.closing:raise StopRequested()
        for sig in (signal.SIGINT,signal.SIGTERM):original[sig]=signal.signal(sig,stop)
        try:
            self.acquire();self.database();config=self.application();self.services(config)
            while True:self.healthy_children();time.sleep(.25)
        except StopRequested:self.log('STOPPING')
        finally:
            self.shutdown()
            for sig,handler in original.items():signal.signal(sig,handler)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Self-contained local store container lifecycle.')
    parser.add_argument('--healthcheck',action='store_true');args=parser.parse_args(argv)
    if args.healthcheck:return 0 if healthcheck() else 1
    runtime=ContainerRuntime()
    try:runtime.run();return 0
    except Exception as error:
        code=str(error) if isinstance(error,StartupFailure) else getattr(error,'code',None)
        if not isinstance(code,str) or not re.fullmatch('[A-Za-z0-9_:-]{1,160}',code):code='startup_failed'
        frames=traceback.extract_tb(error.__traceback__)
        point={'file':Path(frames[-1].filename).name,'function':frames[-1].name,'line':frames[-1].lineno} if frames else None
        runtime.log('FAILED',phase=runtime.phase,code=code,error_type=type(error).__name__,tracepoint=point,state_preserved=True,guidance='Inspect docker compose logs. Repair the preserved private state; ordinary startup never resets it.')
        return 1


if __name__=='__main__':raise SystemExit(main())
