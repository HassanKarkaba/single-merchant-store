#!/usr/bin/env python3
"""Install-independent entry point for private initialization and application processes."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import signal
import sys
from app.config import (load_config, read_secret, resolve_database, guarded_test_context, resolve_migration_binding,
                        grant_runtime_access, validate_runtime_binding, strict_object)


def build_parser():
    parser = argparse.ArgumentParser(description='Single merchant store: private setup, database maintenance and MVC runtime.')
    sub = parser.add_subparsers(dest='command', required=True)
    configure = sub.add_parser('configure', help='Create protected local PostgreSQL bootstrap files outside this project.')
    configure.add_argument('--state-dir')
    mail=sub.add_parser('configure-mail',help='Configure private local account mail capture for an existing instance.')
    mail.add_argument('--config',required=True)
    initialize = sub.add_parser('init', help='Create a new isolated synthetic instance; never reset an existing database.')
    initialize.add_argument('--state-dir'); initialize.add_argument('--admin-database-file', required=True)
    initialize.add_argument('--origin', default='https://localhost:8443'); initialize.add_argument('--database-port',type=int,default=55439)
    commands = ('check','local-access','migrate','seed-demo','provision-reviewer','reset-password','provision-workload','rotate-workload','revoke-workload','serve-demo','worker-demo','run','backup-demo','restore-demo','reset-demo')
    for name in commands:
        command = sub.add_parser(name)
        command.add_argument('--config', required=True)
        command.add_argument('--profile', choices=('local-sandbox','demo-test','portfolio-demo'))
        if name=='check': command.add_argument('--declarations', action='store_true')
        if name in ('provision-reviewer','reset-password'):
            command.add_argument('--principal-id',required=True); command.add_argument('--secret-input-file',required=True)
        if name in ('provision-workload','rotate-workload','revoke-workload'): command.add_argument('--secret-input-file',required=True)
        if name=='serve-demo': command.add_argument('--port',type=int,default=8080)
        if name=='worker-demo': command.add_argument('--once',action='store_true')
        if name=='run': command.add_argument('--node',default='node'); command.add_argument('--caddy',default='caddy'); command.add_argument('--port',type=int,default=8080)
        if name in ('backup-demo','restore-demo'):command.add_argument('--backup-file',required=True)
        if name in ('restore-demo','reset-demo'):
            command.add_argument('--replacement-config',required=True);command.add_argument('--run-id',required=True)
        if name=='restore-demo':
            command.add_argument('--restore-key-file',required=True);command.add_argument('--independent-simulator-evidence',required=True)
    return parser


def provision_reviewer(args, config, store):
    value = json.loads(read_secret('file:'+str(Path(args.secret_input_file).absolute())), object_pairs_hook=strict_object)
    fields = {'environment','principal_id','username','roles','password_ref','totp_secret_ref','factor_version','expected_identity_revision'}
    if type(value) is not dict or set(value)!=fields or value['environment']!=config['profile'] or value['principal_id']!=args.principal_id:
        raise ValueError('closed_provision_schema_required')
    return store.identity.provision_account(principal_id=value['principal_id'],username=value['username'],roles=value['roles'],
        password=read_secret(value['password_ref']),factor_ref=value['totp_secret_ref'],factor_version=value['factor_version'],expected_identity_revision=value['expected_identity_revision'])


def provision_workload(args, config, store):
    value = json.loads(read_secret('file:'+str(Path(args.secret_input_file).absolute())), object_pairs_hook=strict_object)
    base = {'environment','merchant_id','machine_principal_id','service_id','credential_ref','expected_revision'}
    expected = base if args.command=='revoke-workload' else base|{'issued_at','expires_at'}
    if type(value) is not dict or set(value)!=expected or value['environment']!=config['profile'] or value['merchant_id']!=config['merchant_id'] or value['machine_principal_id']!=config['machine_principal_id'] or value['service_id']!='sme-worker' or value['credential_ref']!=config['workload_credential_ref']:
        raise ValueError('closed_workload_provision_schema_required')
    fields = {key:value[key] for key in ('merchant_id','machine_principal_id','service_id','credential_ref','expected_revision')}
    if args.command=='revoke-workload': return store.authority.revoke_workload(**fields)
    for key in ('issued_at','expires_at'):
        if not isinstance(value[key],str) or not value[key].endswith('Z'): raise ValueError('workload_utc_timestamp_required')
        fields[key] = datetime.fromisoformat(value[key].replace('Z','+00:00'))
    result = store.authority.provision_workload(**fields)
    store.recovery.provision_control()
    return result


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = None
    try:
        if args.command=='configure':
            from app.controllers.setup import configure_database
            result = configure_database(state_dir=args.state_dir)
        elif args.command=='configure-mail':
            from app.controllers.setup import configure_mail
            result=configure_mail(args.config)
        elif args.command=='init':
            from app.controllers.setup import initialize
            result = initialize(admin_database_file=args.admin_database_file,state_dir=args.state_dir,origin=args.origin,database_port=args.database_port)
        else:
            profile = args.profile or json.loads(Path(args.config).read_text(), object_pairs_hook=strict_object).get('profile')
            config = load_config(args.config,profile,bootstrap=getattr(args,'declarations',False),unit=args.command)
            if args.command=='check' and args.declarations:
                print(json.dumps({'state':'CONFIGURATION_VALID','database_access':False}));return 0
            if config['profile']=='demo-test':
                proof=json.loads(Path(config['disposable_proof_file']).read_text());guarded_test_context(config,proof['run_id'])
            if args.command=='local-access':
                from app.controllers.local_access import show_local_access
                show_local_access(args.config,config);return 0
            if args.command=='run':
                from app.controllers.runtime import supervise
                supervise(args.config,config,node=args.node,caddy=args.caddy,port=args.port);return 0
            from app.models.store import Store
            dsn=resolve_database(config)
            if args.command=='migrate':
                dsn,binding=resolve_migration_binding(config)
                store=Store(dsn,profile=config['profile'],config=config,maintenance=True)
                store.migrate()
                if config['profile']=='portfolio-demo':grant_runtime_access(config,dsn,binding['runtime_role']);validate_runtime_binding(config)
                result={'state':'MIGRATED'}
            else:
                if args.command in ('serve-demo','worker-demo') and config['profile']=='portfolio-demo':validate_runtime_binding(config)
                private_workload=args.command in ('provision-workload','rotate-workload','revoke-workload')
                store=Store(dsn,profile=config['profile'],config=config,maintenance=private_workload)
                if private_workload:store.initialize()
                if args.command in ('backup-demo','restore-demo','reset-demo'):
                    from app.controllers import maintenance
                    handler={'backup-demo':maintenance.backup_database,'restore-demo':maintenance.restore_database,'reset-demo':maintenance.reset_database}[args.command]
                    result=handler(args,config,store)
                elif args.command in ('provision-reviewer','reset-password'):result=provision_reviewer(args,config,store)
                elif args.command in ('provision-workload','rotate-workload','revoke-workload'):result=provision_workload(args,config,store)
                elif args.command=='seed-demo':
                    from app.controllers.setup import authenticate_seed
                    record=json.loads(read_secret(config['seed_credentials_ref']),object_pairs_hook=strict_object)
                    result=store.seed_demo(actor=authenticate_seed(store,{'username':record['username'],'password':read_secret(record['password_ref']),'totp_secret':read_secret(record['totp_secret_ref'])}))
                elif args.command=='check':
                    result=store.readiness()
                    if not result['ready']:raise ValueError('runtime_not_ready_start_worker')
                elif args.command=='worker-demo':
                    from app.controllers.worker import worker_loop
                    worker_loop(args,config,store);return 0
                elif args.command=='serve-demo':
                    from waitress import create_server
                    from app.controllers.http import PortfolioDemoWSGI,RendererClient
                    server=create_server(PortfolioDemoWSGI(store,config,RendererClient(config['renderer_socket'])),host='127.0.0.1',port=args.port,threads=4,connection_limit=32,channel_timeout=30,max_request_header_size=16384,max_request_body_size=1048576,clear_untrusted_proxy_headers=False)
                    stopping=False
                    def stop(*_):
                        nonlocal stopping
                        stopping=True
                        store.close();server.close()
                    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
                    try:server.run()
                    except (OSError,ValueError):
                        if not stopping:raise
                    return 0
                else:raise ValueError('unsupported_command')
        from app.controllers.http import wire_value
        print(json.dumps(wire_value(result),ensure_ascii=False,allow_nan=False));return 0
    except Exception as error:
        code=getattr(error,'code',str(error))
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,160}',code):code='command_failed'
        print(json.dumps({'command':args.command,'state':'FAILED','code':code}),file=sys.stderr);return 1
    finally:
        if store is not None:store.close()


if __name__=='__main__':raise SystemExit(main())
