"""Actual native Identity/notification transaction atomicity on an attested test DB."""
import json
import os
from pathlib import Path
import secrets
import uuid
from cryptography.fernet import Fernet
import pytest
from app.config import guarded_test_context,load_config,read_secret,resolve_database
from app.models.notifications import IdentityMailService
from app.models.store import Store

pytestmark=pytest.mark.native


@pytest.fixture
def native_mail():
    path=os.environ.get('SME_TEST_CONFIG');run=os.environ.get('SME_TEST_RUN_ID')
    if not path or not run:pytest.fail('explicit private account-mail fixture required')
    config=load_config(path,'demo-test');guarded_test_context(config,run)
    store=Store(resolve_database(config),profile='demo-test',config=config)
    mail=IdentityMailService(store.db,config['mail'],key_provider=lambda:read_secret(config['mail']['queue_key_ref']),intent_guard=store.identity.verify_mail_intent)
    store.identity.mail_port=mail
    try:yield store,mail,config
    finally:store.close()


def register(identity,email,operation=None):
    pre=identity.bootstrap_preauth();kwargs={'preauth_credential':pre.preauth_credential,'csrf':pre.public['csrf_token'],'source_key':'account-mail-native-'+str(uuid.uuid4()),'operation_id':operation or str(uuid.uuid4())}
    result=identity.register_customer('Example Customer',email,'Private-example-password-123','Private-example-password-123',**kwargs)
    return result,kwargs


def test_native_registration_and_encrypted_mail_commit_once_without_public_token(native_mail):
    store,mail,config=native_mail;email='mail-'+uuid.uuid4().hex+'@review.invalid'
    result,kwargs=register(store.identity,email)
    assert result['state']=='verification_pending' and 'token' not in result and 'email' not in result
    with store.db.transaction('demo_identity') as tx:
        row=tx.execute('SELECT m.* FROM notifications.identity_mail m JOIN demo_identity.public_actions a ON a.message_id=m.id WHERE a.id=%s',(result['operation_id'],)).fetchone()
        account=tx.execute('SELECT active,customer_registration_state FROM demo_identity.accounts WHERE email_canonical=%s',(email,)).fetchone()
    assert account['active'] is False and account['customer_registration_state']=='pending' and row['state']=='QUEUED'
    encrypted=bytes(row['ciphertext']);payload=json.loads(Fernet(read_secret(config['mail']['queue_key_ref']).encode()).decrypt(encrypted))
    if payload['token'].encode() in encrypted or email.encode() in encrypted:pytest.fail('Private mail encryption boundary failed')
    if payload['token'] in json.dumps(result):pytest.fail('Private token escaped public result')
    replay=store.identity.register_customer('Example Customer',email,'Private-example-password-123','Private-example-password-123',**kwargs)
    assert replay==result
    with store.db.transaction('demo_identity') as tx:
        count=tx.execute('SELECT count(*) n FROM notifications.identity_mail WHERE id=%s',(row['id'],)).fetchone()['n'];unchanged=tx.execute('SELECT ciphertext FROM notifications.identity_mail WHERE id=%s',(row['id'],)).fetchone()['ciphertext']
        status=mail.status(tx,message_id=row['id'])
    assert count==1 and bytes(unchanged)==encrypted and set(status)=={'message_id','state','attempts','retryable'}


def test_native_mail_encryption_failure_rolls_back_pending_account_and_token(native_mail):
    store,mail,_=native_mail;email='rollback-'+uuid.uuid4().hex+'@review.invalid'
    mail.key_provider=lambda:'invalid-key'
    with pytest.raises(ValueError,match='protected_mail_queue_key_required'):register(store.identity,email)
    with store.db.transaction('demo_identity') as tx:
        account=tx.execute('SELECT id FROM demo_identity.accounts WHERE email_canonical=%s',(email,)).fetchone()
    assert account is None


def test_mail_stage_refuses_caller_fabricated_identity_intent(native_mail):
    from datetime import datetime,timedelta,timezone
    store,mail,_=native_mail
    with pytest.raises(PermissionError):
        with store.db.transaction('demo_identity') as tx:mail.stage_identity_mail(tx,message_id=str(uuid.uuid4()),purpose='email_verification',recipient='forged@review.invalid',token=secrets.token_urlsafe(32),expires_at=datetime.now(timezone.utc)+timedelta(minutes=30),full_name='Example Customer')
