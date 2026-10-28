"""Explicit actual SMTP lost-acknowledgment retry on a newly born guarded instance."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import pytest
from app.config import ROOT,guarded_test_context,load_config,resolve_database
from app.controllers.mail_capture import SMTPHandler,start_capture
from app.controllers.setup import initialize
from app.models.store import Store

pytestmark=pytest.mark.native


def available_port():
    with socket.socket() as connection:connection.bind(('127.0.0.1',0));return connection.getsockname()[1]


def test_lost_smtp_ack_retries_same_message_without_duplicate_capture():
    admin=os.environ.get('SME_MAIL_TEST_ADMIN_FILE');state=os.environ.get('SME_MAIL_TEST_STATE_DIR')
    if not admin or not state:pytest.fail('explicit private new-instance mail retry inputs required')
    result=initialize(admin_database_file=admin,state_dir=state,origin='https://localhost:11443')
    path=Path(result['profile_file']);config=json.loads(path.read_text());config['mail']['port']=config['mail']['capture']['smtp_port']=available_port();config['mail']['capture']['http_port']=available_port();path.write_text(json.dumps(config));path.chmod(0o600)
    config=load_config(path,'demo-test');guarded_test_context(config,result['run_id']);dsn=resolve_database(config)
    app=Store(dsn,profile='demo-test',config=config)
    try:
        pre=app.identity.bootstrap_preauth();action=app.identity.register_customer('Example Customer','retry-'+uuid.uuid4().hex+'@review.invalid','Private-example-password-123','Private-example-password-123',preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key='native-mail-retry',operation_id=str(uuid.uuid4()))
        with app.db.transaction('demo_identity') as tx:message_id=tx.execute('SELECT message_id FROM demo_identity.public_actions WHERE id=%s',(action['operation_id'],)).fetchone()['message_id']
    finally:app.close()
    smtp,inbox=start_capture(config['mail'])
    class LostAckHandler(SMTPHandler):
        def reply(self,code,text):
            if code==250 and text.startswith('LOCAL_CAPTURED '):
                self.request.shutdown(socket.SHUT_RDWR);self.request.close();raise ConnectionResetError('private_test_ack_loss')
            return super().reply(code,text)
    smtp.RequestHandlerClass=LostAckHandler
    def turn():
        process=subprocess.run([sys.executable,str(ROOT/'manage.py'),'worker-demo','--once','--config',str(path)],cwd=ROOT,capture_output=True,text=True,timeout=30)
        if process.returncode:pytest.fail('Native mail worker did not complete')
        return json.loads(process.stdout.strip().splitlines()[-1])['account_mail']
    def inspect():
        store=Store(dsn,profile='demo-test',config=config)
        try:
            with store.db.transaction('notifications') as tx:return dict(tx.execute('SELECT state,attempts,retryable,ciphertext,GREATEST(0,extract(epoch FROM(next_at-clock_timestamp()))) AS wait FROM notifications.identity_mail WHERE id=%s',(message_id,)).fetchone())
        finally:store.close()
    try:
        first=turn();assert first['state']=='DELIVERY_FAILED' and first['retryable'] is True
        initial=inspect();assert initial['attempts']==1 and len(inbox.store.messages())==1
        smtp.RequestHandlerClass=SMTPHandler
        time.sleep(min(3,float(initial['wait'])+.05))
        second=turn();assert second['state']=='LOCAL_CAPTURED'
        completed=inspect();assert completed['attempts']==2 and completed['retryable'] is False and bytes(completed['ciphertext'])==bytes(initial['ciphertext'])
        assert len(inbox.store.messages())==1
        assert turn()['processed']==0 and inspect()['attempts']==2
    finally:
        for server in (smtp,inbox):server.shutdown();server.server_close()
