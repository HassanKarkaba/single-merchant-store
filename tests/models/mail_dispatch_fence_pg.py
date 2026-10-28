"""Explicit native revocation/hold races across bounded real SMTP dispatch."""
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid
import pytest
from app.config import guarded_test_context,load_config,resolve_database
from app.controllers.mail_capture import start_capture
from app.controllers.setup import initialize
from app.models.notifications import account_mail
from app.models.store import Store

pytestmark=pytest.mark.native


def available_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


@pytest.mark.parametrize('change',['revoke','hold'])
def test_native_authority_change_waits_for_smtp_and_its_receipt(change,monkeypatch):
    admin=os.environ.get('SME_MAIL_TEST_ADMIN_FILE');base=os.environ.get('SME_MAIL_TEST_STATE_DIR')
    if not admin or not base:pytest.fail('explicit private new-instance SMTP fence inputs required')
    result=initialize(admin_database_file=admin,state_dir=Path(base)/change,origin='https://localhost:12443')
    path=Path(result['profile_file']);config=json.loads(path.read_text());config['mail']['port']=config['mail']['capture']['smtp_port']=available_port();config['mail']['capture']['http_port']=available_port();path.write_text(json.dumps(config));path.chmod(0o600)
    config=load_config(path,'demo-test');guard=guarded_test_context(config,result['run_id']);app=Store(resolve_database(config),profile='demo-test',config=config,maintenance=True);app.initialize()
    smtp,inbox=start_capture(config['mail']);token=None;thread=None;complete=threading.Event();backend=threading.Event();errors=[];pids=[];blocked=[]
    connect=app.db._connect
    def observed_connect():
        connection=connect()
        if threading.current_thread().name=='mail-fence-change':
            pids.append(connection.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']);backend.set()
        return connection
    monkeypatch.setattr(app.db,'_connect',observed_connect)
    def alter_authority():
        try:
            if change=='revoke':app.authority.revoke_workload(config['merchant_id'],config['machine_principal_id'],'sme-worker',config['workload_credential_ref'],expected_revision=1)
            else:app.recovery.start_restore_reconciliation(guard=guard)
        except BaseException as error:errors.append(type(error).__name__)
        finally:complete.set()
    send=account_mail.send_account_mail
    def checkpoint(message,mail_config,*,deadline):
        nonlocal thread
        thread=threading.Thread(target=alter_authority,name='mail-fence-change');thread.start()
        if not backend.wait(.5):raise AssertionError('Native authority transaction did not start')
        until=min(time.monotonic()+1,deadline-.2)
        while time.monotonic()<until:
            if complete.is_set():raise AssertionError('Native authority change committed before SMTP fencing')
            with app.db.transaction('notifications') as tx:waiting=tx.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(pids[-1],)).fetchone()
            if waiting and waiting['wait_event_type']=='Lock':blocked.append(True);break
            time.sleep(.01)
        if not blocked:raise AssertionError('Native authority change was not blocked by actual PostgreSQL locks')
        outcome=send(message,mail_config,deadline=deadline)
        if complete.is_set():raise AssertionError('Native authority change crossed bounded SMTP dispatch')
        return outcome
    monkeypatch.setattr(account_mail,'send_account_mail',checkpoint)
    try:
        pre=app.identity.bootstrap_preauth();app.identity.register_customer('Example Customer','fence-'+uuid.uuid4().hex+'@review.invalid','Private-example-password-123','Private-example-password-123',preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key='mail-fence-race',operation_id=str(uuid.uuid4()))
        app.recovery.run_once(limit=1)
        with app.db.transaction('application') as tx:row=tx.execute('SELECT token,process_id,expires_at>clock_timestamp() AS active FROM application.worker_lease WHERE singleton=1').fetchone()
        assert row['active'] and row['process_id']==str(os.getpid());token=row['token']
        delivery=app.account_mail.run_once(worker_token=token)
        assert delivery['state']=='LOCAL_CAPTURED' and blocked and len(inbox.store.messages())==1
        thread.join(3)
        assert complete.is_set() and not errors
        with app.db.transaction('notifications') as tx:state=tx.execute('SELECT state,attempts FROM notifications.identity_mail').fetchone()
        assert state['state']=='LOCAL_CAPTURED' and state['attempts']==1
        with pytest.raises(PermissionError):app.account_mail.run_once(worker_token=token)
    finally:
        if thread:thread.join(3)
        if token:
            with app.db.transaction('application') as tx:tx.execute('UPDATE application.worker_lease SET expires_at=clock_timestamp() WHERE singleton=1 AND token=%s AND process_id=%s',(token,str(os.getpid())))
        app.close()
        for server in (smtp,inbox):server.shutdown();server.server_close()
