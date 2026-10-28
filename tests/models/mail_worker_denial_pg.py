"""Explicit mail authority negatives on a newly born private instance only."""
import json
import os
from pathlib import Path
import uuid
import pytest
from app.config import guarded_test_context,load_config,resolve_database
from app.controllers.setup import initialize
from app.models.store import Store

pytestmark=pytest.mark.native


def test_foreign_worker_and_revoked_workload_cannot_claim_account_mail():
    admin=os.environ.get('SME_MAIL_TEST_ADMIN_FILE');state=os.environ.get('SME_MAIL_TEST_STATE_DIR')
    if not admin or not state:pytest.fail('explicit private new-instance mail authority inputs required')
    result=initialize(admin_database_file=admin,state_dir=state,origin='https://localhost:10443')
    path=Path(result['profile_file']);config=json.loads(path.read_text());config['mail']['port']=config['mail']['capture']['smtp_port']=11125;config['mail']['capture']['http_port']=18035
    path.write_text(json.dumps(config));path.chmod(0o600)
    config=load_config(path,'demo-test');guarded_test_context(config,result['run_id'])
    app=Store(resolve_database(config),profile='demo-test',config=config)
    token=None
    try:
        pre=app.identity.bootstrap_preauth()
        action=app.identity.register_customer('Example Customer','worker-'+uuid.uuid4().hex+'@review.invalid','Private-example-password-123','Private-example-password-123',preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key='worker-mail-denial',operation_id=str(uuid.uuid4()))
        app.recovery.run_once(limit=1)
        with app.db.transaction('application') as tx:
            row=tx.execute('SELECT token,process_id,expires_at>clock_timestamp() AS active FROM application.worker_lease WHERE singleton=1').fetchone()
            assert row['active'] and row['process_id']==str(os.getpid());token=row['token']
        with pytest.raises(PermissionError):app.account_mail.run_once(worker_token=str(uuid.uuid4()))
        app.authority.revoke_workload(config['merchant_id'],config['machine_principal_id'],'sme-worker',config['workload_credential_ref'],expected_revision=1)
        with pytest.raises(PermissionError):app.account_mail.run_once(worker_token=token)
        with app.db.transaction('demo_identity') as tx:
            row=tx.execute('SELECT m.state,m.attempts FROM notifications.identity_mail m JOIN demo_identity.public_actions a ON a.message_id=m.id WHERE a.id=%s',(action['operation_id'],)).fetchone()
        assert row['state']=='QUEUED' and row['attempts']==0
    finally:
        if token:
            with app.db.transaction('application') as tx:tx.execute('UPDATE application.worker_lease SET expires_at=clock_timestamp() WHERE singleton=1 AND token=%s AND process_id=%s',(token,str(os.getpid())))
        app.close()
