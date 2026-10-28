"""Customer identity over guarded Pg, real native authority, encrypted mail intents.

The private fixture oracle decrypts its own synthetic outbox only to exercise
one-use links; no token, password, or credential is printed or exported.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import secrets
import uuid

from cryptography.fernet import Fernet
import pytest
from app.models.authorization import HostBoundPgAuthority,AuthorityDenied
from app.models.disposable import DisposableResourceGuard
from app.models.identity import PortfolioDemoIdentity,IdentityError
from app.models.notifications import NotificationService,IdentityMailService
from app.models.persistence import PostgresPersistence

pytestmark=pytest.mark.native
PASSWORD='Synthetic public password 2026!'


@pytest.fixture(scope='module')
def setup():
    dsn=os.environ.get('SME_TEST_DSN')
    assert dsn,'Explicit independently attested disposable target required'
    DisposableResourceGuard.from_environment().attest_dsn(dsn)
    db=PostgresPersistence(dsn);db.migrate_core()
    authority=HostBoundPgAuthority(db);authority.migrate()
    key=secrets.token_bytes(32);cipher=Fernet.generate_key()
    owner=PortfolioDemoIdentity(db,merchant_id=str(uuid.uuid4()),profile='demo-test',authority=authority,receipt_key_provider=lambda:key)
    owner.migrate();owner.initialize();authority.publication_verifier=owner.verify_publication_receipt
    notifications=NotificationService(db,profile='demo-test');notifications.migrate()
    mail=IdentityMailService(db,config={'mode':'local_capture'},key_provider=lambda:cipher,intent_guard=owner.verify_mail_intent)
    owner.mail_port=mail
    return db,owner,authority,cipher


def carrier(owner):
    pre=owner.bootstrap_preauth()
    return {'preauth_credential':pre.preauth_credential,'csrf':pre.public['csrf_token'],'source_key':'customer-test-'+uuid.uuid4().hex}


def action(kw,key=None):return dict(kw,operation_id=key or str(uuid.uuid4()))


def email():return 'customer+'+uuid.uuid4().hex+'@review.invalid'


def private_message(setup,address,purpose):
    db,owner,_,key=setup
    with db.transaction('notifications') as tx:rows=tx.fetchall('SELECT m.ciphertext FROM notifications.identity_mail m JOIN demo_identity.customer_tokens t ON t.message_id=m.id WHERE m.purpose=%s AND t.merchant_id=%s ORDER BY m.created_at DESC',(purpose,owner.merchant_id))
    for row in rows:
        payload=json.loads(Fernet(key).decrypt(bytes(row['ciphertext'])))
        if payload['recipient']==address:return payload
    raise AssertionError('Actual encrypted mail intent not found')


def verified(setup):
    _,owner,_,_=setup;kw=carrier(owner);address=email()
    owner.register_customer('Synthetic Customer',address,PASSWORD,PASSWORD,**action(kw))
    message=private_message(setup,address,'email_verification')
    assert owner.verify_email(message['token'],**action(kw))['state']=='email_verified'
    return address,kw


def login(owner,address,password=PASSWORD):
    kw=carrier(owner)
    return owner.authenticate(address,password,preauth_credential=kw['preauth_credential'],csrf=kw['csrf'],source_key=kw['source_key'])


def test_registration_validation_pending_idempotence_and_native_profile(setup):
    db,owner,_,_=setup;kw=carrier(owner);address=email();key=str(uuid.uuid4())
    for args in [('A',address,PASSWORD,PASSWORD),('Valid Name','bad-address',PASSWORD,PASSWORD),('Valid Name',address,'short','short'),('Valid Name',address,PASSWORD,PASSWORD+'different')]:
        with pytest.raises(IdentityError) as caught:owner.register_customer(*args,**action(kw))
        assert caught.value.status==422 and caught.value.field_errors
    result=owner.register_customer('  Marie Example  ',address.upper(),PASSWORD,PASSWORD,**action(kw,key))
    assert result['state']=='verification_pending' and result['delivery_status']=='REQUEST_ACCEPTED'
    assert set(result)=={'state','message','operation_id','delivery_status'}
    assert owner.register_customer('Marie Example',address,PASSWORD,PASSWORD,**action(kw,key))==result
    with pytest.raises(IdentityError,match='idempotency_conflict'):owner.register_customer('Other Name',address,PASSWORD,PASSWORD,**action(kw,key))
    with pytest.raises(IdentityError,match='email_verification_required'):login(owner,address)
    payload=private_message(setup,address,'email_verification')
    with db.transaction('demo_identity') as tx:
        account=tx.fetchone('SELECT * FROM demo_identity.accounts WHERE email_canonical=%s',(address,))
        assert account['roles']==['customer'] and account['kind']=='customer' and not account['active']
        assert tx.fetchone('SELECT count(*) n FROM demo_identity.sessions WHERE principal_id=%s',(account['id'],))['n']==0
        token=tx.fetchone('SELECT * FROM demo_identity.customer_tokens WHERE principal_id=%s',(account['id'],))
        assert token['token_digest']!=payload['token'] and payload['token'] not in json.dumps(result)
        assert tx.fetchone("SELECT count(*) n FROM commerce_audit_staging WHERE payload::text LIKE %s",('%'+payload['token']+'%',))['n']==0
    key=str(uuid.uuid4());assert owner.verify_email(payload['token'],**action(kw,key))['state']=='email_verified'
    assert owner.verify_email(payload['token'],**action(kw,key))['state']=='email_verified'
    with pytest.raises(IdentityError,match='verification_token_invalid'):owner.verify_email(payload['token'],**action(kw))
    logged=login(owner,address.upper());assert logged.public['actor']['full_name']=='Marie Example' and logged.public['actor']['email']==address
    # Only the current real carrier may read the action and native mail status.
    assert owner.get_account_action_status(result['operation_id'],**kw)['delivery_status']=='queued'
    with pytest.raises(IdentityError,match='not_found'):owner.get_account_action_status(result['operation_id'],**carrier(owner))


def test_email_full254_plus_case_and_concurrent_registration(setup):
    db,owner,_,_=setup
    local='a'*31+uuid.uuid4().hex+'b';domain='c'*63+'.'+'d'*63+'.'+'e'*61
    address=local+'@'+domain;assert len(address)==254
    owner.register_customer('Long Email Customer',address,PASSWORD,PASSWORD,**action(carrier(owner)))
    assert private_message(setup,address,'email_verification')['recipient']==address
    address=email();requests=[carrier(owner),carrier(owner)]
    def register(kw):return owner.register_customer('Concurrent Customer',address,PASSWORD,PASSWORD,**action(kw))
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(register,requests))
    assert all(r['state']=='verification_pending' for r in results)
    with db.transaction('demo_identity') as tx:
        assert tx.fetchone('SELECT count(*) n FROM demo_identity.accounts WHERE email_canonical=%s',(address,))['n']==1
        assert tx.fetchone('SELECT count(*) n FROM demo_identity.customer_tokens t JOIN demo_identity.accounts a ON a.id=t.principal_id WHERE a.email_canonical=%s',(address,))['n']==1


def test_expiry_resend_purpose_binding_and_durable_rate(setup):
    db,owner,_,_=setup;kw=carrier(owner);address=email()
    owner.register_customer('Token Customer',address,PASSWORD,PASSWORD,**action(kw));old=private_message(setup,address,'email_verification')
    with pytest.raises(IdentityError,match='password_reset_token_invalid'):owner.reset_password(old['token'],PASSWORD+'new',PASSWORD+'new',**action(kw))
    with db.transaction('demo_identity') as tx:tx.execute("UPDATE demo_identity.customer_tokens SET created_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 second' WHERE message_id=%s",(old['message_id'],))
    with pytest.raises(IdentityError,match='verification_token_invalid'):owner.verify_email(old['token'],**action(kw))
    owner.resend_verification(address,**action(kw));new=private_message(setup,address,'email_verification');assert new['token']!=old['token']
    with pytest.raises(IdentityError,match='verification_token_invalid'):owner.verify_email(old['token'],**action(kw))
    assert owner.verify_email(new['token'],**action(kw))['state']=='email_verified'
    source='durable-invalid-link-'+uuid.uuid4().hex;bad=carrier(owner);bad['source_key']=source
    for _ in range(20):
        with pytest.raises(IdentityError,match='verification_token_invalid'):owner.verify_email(secrets.token_urlsafe(32),**action(bad))
    with pytest.raises(IdentityError,match='authentication_rate_limited'):owner.verify_email(secrets.token_urlsafe(32),**action(bad))


def test_reset_revokes_native_sessions_once_and_preserves_customer(setup):
    db,owner,authority,_=setup;address,kw=verified(setup);logged=login(owner,address);old=owner.resolve_session(logged.credential)
    request=owner.request_password_reset(address,**action(kw));assert request['delivery_status']=='REQUEST_ACCEPTED'
    payload=private_message(setup,address,'password_reset');new=PASSWORD+' changed';key=str(uuid.uuid4())
    with pytest.raises(IdentityError,match='verification_token_invalid'):owner.verify_email(payload['token'],**action(kw))
    result=owner.reset_password(payload['token'],new,new,**action(kw,key));assert result['state']=='password_reset'
    assert owner.reset_password(payload['token'],new,new,**action(kw,key))==result
    with pytest.raises(IdentityError):owner.resolve_session(logged.credential)
    with pytest.raises(IdentityError):login(owner,address,PASSWORD)
    logged_new=login(owner,address,new);current=owner.resolve_session(logged_new.credential);assert current['id']==old['id'] and current['account_epoch']==old['account_epoch']+1
    with db.transaction('authority') as tx:
        gate=tx.fetchone("SELECT is_open FROM commerce_authority_gates WHERE merchant_id=%s AND kind='session' AND subject_id=%s",(owner.merchant_id,old['session_id']));assert gate['is_open'] is False
    with db.transaction('demo_identity') as tx:assert tx.fetchone('SELECT count(*) n FROM demo_identity.password_resets WHERE principal_id=%s',(old['id'],))['n']==1
    # Lost reply with an expired/revoked original browser can safely obtain a
    # fresh carrier and recover the exact result, without resetting a second time.
    retry=owner.reset_password(payload['token'],new,new,**action(carrier(owner),key));assert retry['state']=='password_reset'
    assert owner.resolve_session(logged_new.credential)['account_epoch']==current['account_epoch']


def test_reset_lost_publication_reply_resumes_original_native_epoch(setup):
    db,owner,authority,_=setup;address,kw=verified(setup);logged=login(owner,address);before=owner.resolve_session(logged.credential)
    owner.request_password_reset(address,**action(kw));payload=private_message(setup,address,'password_reset');new=PASSWORD+' recovered';key=str(uuid.uuid4());original=authority.publish_session_binding
    def unavailable(*args,**kwargs):raise TimeoutError('controlled_publication_interruption')
    authority.publish_session_binding=unavailable
    try:
        with pytest.raises(TimeoutError):owner.reset_password(payload['token'],new,new,**action(kw,key))
    finally:authority.publish_session_binding=original
    with pytest.raises(IdentityError):owner.resolve_session(logged.credential)
    with db.transaction('demo_identity') as tx:
        epoch=tx.fetchone('SELECT account_epoch FROM demo_identity.accounts WHERE id=%s',(before['id'],))['account_epoch'];assert epoch==before['account_epoch']+1
        assert tx.fetchone("SELECT state FROM demo_identity.password_resets WHERE principal_id=%s",(before['id'],))['state']=='UPDATED'
    assert owner.reset_password(payload['token'],new,new,**action(kw,key))['state']=='password_reset'
    with db.transaction('demo_identity') as tx:assert tx.fetchone('SELECT account_epoch FROM demo_identity.accounts WHERE id=%s',(before['id'],))['account_epoch']==epoch
    assert login(owner,address,new).public['actor']['kind']=='customer'


def test_legacy_namespace_and_private_provision_cannot_race_public_claim(setup):
    db,owner,_,_=setup;address='legacy'+uuid.uuid4().hex+'@review.invalid';pid=str(uuid.uuid4())
    kw=carrier(owner)
    def public():return owner.register_customer('Public Customer',address,PASSWORD,PASSWORD,**action(kw))
    def private():
        try:return owner.provision_account(principal_id=pid,username=address.upper(),roles=['customer'],password=PASSWORD)
        except IdentityError as error:
            assert error.code=='identity_version_conflict';return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(public);b=pool.submit(private);a.result();b.result()
    with db.transaction('demo_identity') as tx:assert tx.fetchone('SELECT count(*) n FROM demo_identity.accounts WHERE lower(username)=%s',(address,))['n']==1
    # No public operation can turn a privately provisioned identity into a new
    # customer or overwrite its credential merely by knowing its email address.
    before=owner.request_password_reset(address,**action(carrier(owner)))
    assert before['state']=='reset_requested'


def test_staff_email_cannot_enter_public_reset_or_customer_enrollment(setup):
    from types import SimpleNamespace
    db,owner,_,_=setup;address='staff'+uuid.uuid4().hex+'@review.invalid';pid=str(uuid.uuid4());previous=owner.secret_provider
    owner.secret_provider=SimpleNamespace(resolve=lambda reference:'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')
    try:owner.provision_account(principal_id=pid,username=address,roles=['finance'],password=PASSWORD,factor_ref='private-factor',factor_version=1)
    finally:owner.secret_provider=previous
    kw=carrier(owner)
    assert owner.request_password_reset(address,**action(kw))['state']=='reset_requested'
    assert owner.register_customer('Would Be Customer',address.upper(),PASSWORD,PASSWORD,**action(kw))['state']=='verification_pending'
    with db.transaction('demo_identity') as tx:
        row=tx.fetchone('SELECT kind,roles FROM demo_identity.accounts WHERE id=%s',(pid,));assert row=={'kind':'staff','roles':['finance']}
        assert tx.fetchone('SELECT count(*) n FROM demo_identity.customer_tokens WHERE principal_id=%s',(pid,))['n']==0
        assert tx.fetchone('SELECT count(*) n FROM demo_identity.accounts WHERE lower(username)=%s',(address,))['n']==1


def test_authenticated_customer_carrier_reset_and_fresh_carrier_reply_recovery(setup):
    db,owner,_,_=setup;address,_=verified(setup);logged=login(owner,address)
    context=owner.resolve_session(logged.credential)
    kw={'preauth_credential':logged.credential,'csrf':logged.public['csrf_token'],'source_key':'signed-in-reset-'+uuid.uuid4().hex}
    owner.request_password_reset(address,**action(kw));payload=private_message(setup,address,'password_reset');new=PASSWORD+' signed-in';key=str(uuid.uuid4())
    assert owner.reset_password(payload['token'],new,new,**action(kw,key))['state']=='password_reset'
    with pytest.raises(IdentityError):owner.reset_password(payload['token'],new,new,**action(kw,key))
    assert owner.reset_password(payload['token'],new,new,**action(carrier(owner),key))['state']=='password_reset'
    current=owner.resolve_session(login(owner,address,new).credential);assert current['account_epoch']==context['account_epoch']+1


def test_current_native_mfa_staff_carrier_does_not_make_staff_email_resettable(setup):
    from types import SimpleNamespace
    import pyotp
    db,owner,_,_=setup;address='native'+uuid.uuid4().hex+'@review.invalid';pid=str(uuid.uuid4());secret=pyotp.random_base32();previous=owner.secret_provider
    owner.secret_provider=SimpleNamespace(resolve=lambda reference:secret)
    try:
        owner.provision_account(principal_id=pid,username=address,roles=['finance'],password=PASSWORD,factor_ref='private-factor-'+pid,factor_version=1)
        challenge=login(owner,address)
        staff=owner.verify_staff_totp(challenge.public['challenge']['id'],pyotp.TOTP(secret).now(),challenge_credential=challenge.challenge_credential,preauth_credential=challenge.preauth_credential,csrf=challenge.public['csrf_token'])
        kw={'preauth_credential':staff.credential,'csrf':staff.public['csrf_token'],'source_key':'staff-carrier-'+uuid.uuid4().hex}
        response=owner.request_password_reset(address,**action(kw));assert response['state']=='reset_requested' and response['delivery_status']=='REQUEST_ACCEPTED'
        assert owner.get_account_action_status(response['operation_id'],**kw)['delivery_status']=='REQUEST_ACCEPTED'
        with db.transaction('demo_identity') as tx:assert tx.fetchone('SELECT count(*) n FROM demo_identity.customer_tokens WHERE principal_id=%s',(pid,))['n']==0
    finally:owner.secret_provider=previous


def test_resend_cooldown_and_forgot_status_use_actual_encrypted_mail_receipt(setup):
    db,owner,_,_=setup;kw=carrier(owner);address=email()
    signup=owner.register_customer('Delivery Status Customer',address,PASSWORD,PASSWORD,**action(kw));payload=private_message(setup,address,'email_verification')
    resend=owner.resend_verification(address,**action(kw));assert 1<=resend['retry_after']<=60
    with db.transaction('demo_identity') as tx:
        a=tx.fetchone('SELECT message_id FROM demo_identity.public_actions WHERE id=%s',(signup['operation_id'],))
        b=tx.fetchone('SELECT message_id FROM demo_identity.public_actions WHERE id=%s',(resend['operation_id'],))
        assert a['message_id']==b['message_id']
        tx.execute("UPDATE notifications.identity_mail SET state='DELIVERY_FAILED',retryable=false WHERE id=%s",(a['message_id'],))
    assert owner.get_account_action_status(resend['operation_id'],**kw)['delivery_status']=='DELIVERY_FAILED'
    owner.verify_email(payload['token'],**action(kw))
    forgot=owner.request_password_reset(address,**action(kw));assert owner.get_account_action_status(forgot['operation_id'],**kw)['delivery_status']=='queued'
    with db.transaction('demo_identity') as tx:
        mid=tx.fetchone('SELECT message_id FROM demo_identity.public_actions WHERE id=%s',(forgot['operation_id'],))['message_id'];tx.execute("UPDATE notifications.identity_mail SET state='SMTP_ACCEPTED' WHERE id=%s",(mid,))
    assert owner.get_account_action_status(forgot['operation_id'],**kw)['delivery_status']=='SMTP_ACCEPTED'
    assert owner.get_account_action_status(owner.request_password_reset(email(),**action(kw))['operation_id'],**kw)['delivery_status']=='REQUEST_ACCEPTED'


def test_private_rotation_winning_before_reset_fence_cannot_hold_login_or_block_fresh_reset(setup):
    db,owner,authority,_=setup;address='rotating'+uuid.uuid4().hex+'@review.invalid';kw=carrier(owner)
    owner.register_customer('Private Rotation Customer',address,PASSWORD,PASSWORD,**action(kw));verify=private_message(setup,address,'email_verification');owner.verify_email(verify['token'],**action(kw));logged=login(owner,address)
    context=owner.resolve_session(logged.credential)
    owner.request_password_reset(address,**action(kw));payload=private_message(setup,address,'password_reset');original=authority.prepare_principal_change;rotated=PASSWORD+' privately rotated';once=[False]
    with db.transaction('demo_identity') as tx:revision=tx.fetchone('SELECT revision FROM demo_identity.accounts WHERE id=%s',(context['id'],))['revision']
    def race(merchant,principal,sessions,operation,**options):
        if options.get('admission_guard') and not once[0]:
            once[0]=True;authority.prepare_principal_change=original
            try:owner.provision_account(principal_id=context['id'],username=address,roles=['customer'],password=rotated,expected_identity_revision=revision)
            finally:authority.prepare_principal_change=race
        return original(merchant,principal,sessions,operation,**options)
    authority.prepare_principal_change=race
    try:
        with pytest.raises(IdentityError,match='password_reset_token_invalid'):owner.reset_password(payload['token'],PASSWORD+' superseded',PASSWORD+' superseded',**action(kw))
    finally:authority.prepare_principal_change=original
    # The stale reset never closed the now-current private principal/session.
    assert login(owner,address,rotated).public['actor']['kind']=='customer'
    owner.request_password_reset(address,**action(kw));fresh=private_message(setup,address,'password_reset');assert fresh['token']!=payload['token']
    with pytest.raises(IdentityError,match='password_reset_token_invalid'):owner.reset_password(payload['token'],PASSWORD+' superseded',PASSWORD+' superseded',**action(kw))
    assert owner.reset_password(fresh['token'],PASSWORD+' final',PASSWORD+' final',**action(kw))['state']=='password_reset'
    assert login(owner,address,PASSWORD+' final').public['actor']['kind']=='customer'


def test_concurrent_same_key_reset_applies_one_hash_and_one_epoch(setup):
    db,owner,_,_=setup;address,kw=verified(setup);logged=login(owner,address);before=owner.resolve_session(logged.credential)
    owner.request_password_reset(address,**action(kw));payload=private_message(setup,address,'password_reset');key=str(uuid.uuid4());new=PASSWORD+' concurrent'
    def reset(_):return owner.reset_password(payload['token'],new,new,**action(kw,key))
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reset,range(2)))
    assert all(result['state']=='password_reset' for result in results) and results[0]==results[1]
    current=owner.resolve_session(login(owner,address,new).credential);assert current['account_epoch']==before['account_epoch']+1
    with db.transaction('demo_identity') as tx:
        assert tx.fetchone('SELECT password_version FROM demo_identity.accounts WHERE id=%s',(before['id'],))['password_version']==2
        assert tx.fetchone('SELECT count(*) n FROM demo_identity.password_resets WHERE principal_id=%s',(before['id'],))['n']==1


def test_consumed_reset_token_cannot_be_reused_under_new_client_key(setup):
    db,owner,_,_=setup;address,kw=verified(setup);owner.request_password_reset(address,**action(kw));payload=private_message(setup,address,'password_reset');key=str(uuid.uuid4());new=PASSWORD+' one-use'
    assert owner.reset_password(payload['token'],new,new,**action(kw,key))['state']=='password_reset'
    with pytest.raises(IdentityError,match='password_reset_token_invalid'):owner.reset_password(payload['token'],new,new,**action(carrier(owner)))
    assert owner.reset_password(payload['token'],new,new,**action(carrier(owner),key))['state']=='password_reset'
    with db.transaction('demo_identity') as tx:assert tx.fetchone('SELECT password_version FROM demo_identity.accounts WHERE email_canonical=%s',(address,))['password_version']==2


def test_completed_reset_reply_is_not_current_after_private_credential_rotation(setup):
    db,owner,_,_=setup;address='after'+uuid.uuid4().hex+'@review.invalid';kw=carrier(owner)
    owner.register_customer('Later Rotation Customer',address,PASSWORD,PASSWORD,**action(kw));verification=private_message(setup,address,'email_verification');owner.verify_email(verification['token'],**action(kw));pid=owner.resolve_session(login(owner,address).credential)['id']
    owner.request_password_reset(address,**action(kw));payload=private_message(setup,address,'password_reset');key=str(uuid.uuid4());new=PASSWORD+' old-reset'
    owner.reset_password(payload['token'],new,new,**action(kw,key))
    with db.transaction('demo_identity') as tx:revision=tx.fetchone('SELECT revision FROM demo_identity.accounts WHERE id=%s',(pid,))['revision']
    rotated=PASSWORD+' current-private';owner.provision_account(principal_id=pid,username=address,roles=['customer'],password=rotated,expected_identity_revision=revision)
    with pytest.raises(IdentityError,match='password_reset_token_invalid'):owner.reset_password(payload['token'],new,new,**action(kw,key))
    assert login(owner,address,rotated).public['actor']['id']==pid


@pytest.mark.parametrize('interruption',['PREPARED','UPDATED'])
def test_fresh_forgot_recovers_admitted_reset_without_old_browser_token_or_password(setup,interruption):
    db,owner,authority,_=setup;address,old_carrier=verified(setup);logged=login(owner,address);before=owner.resolve_session(logged.credential)
    owner.request_password_reset(address,**action(old_carrier));old_message=private_message(setup,address,'password_reset');chosen=PASSWORD+' lost-browser';old_key=str(uuid.uuid4())
    if interruption=='PREPARED':
        original=authority.prepare_principal_change
        def interrupted(*args,**kwargs):
            if kwargs.get('admission_guard'):raise TimeoutError('controlled_before_reset_fence')
            return original(*args,**kwargs)
        authority.prepare_principal_change=interrupted
    else:
        original=authority.publish_session_binding
        def interrupted(*args,**kwargs):raise TimeoutError('controlled_after_native_reset')
        authority.publish_session_binding=interrupted
    try:
        with pytest.raises(TimeoutError):owner.reset_password(old_message['token'],chosen,chosen,**action(old_carrier,old_key))
    finally:
        if interruption=='PREPARED':authority.prepare_principal_change=original
        else:authority.publish_session_binding=original
    # This new request supplies EMAIL ONLY under a fresh real carrier. The
    # original raw token, password and browser operation key are not arguments.
    fresh_carrier=carrier(owner);result=owner.request_password_reset(address,**action(fresh_carrier));assert result['state']=='reset_requested'
    fresh_message=private_message(setup,address,'password_reset');assert fresh_message['token']!=old_message['token']
    with pytest.raises(IdentityError):owner.resolve_session(logged.credential)
    with db.transaction('demo_identity') as tx:
        current=tx.fetchone('SELECT password_version,account_epoch FROM demo_identity.accounts WHERE id=%s',(before['id'],));assert current=={'password_version':2,'account_epoch':before['account_epoch']+1}
        assert tx.fetchone('SELECT state FROM demo_identity.password_resets WHERE principal_id=%s',(before['id'],))['state']=='COMPLETED'
    assert login(owner,address,chosen).public['actor']['kind']=='customer'
    newest=PASSWORD+' fresh-link'
    assert owner.reset_password(fresh_message['token'],newest,newest,**action(fresh_carrier))['state']=='password_reset'
    assert login(owner,address,newest).public['actor']['kind']=='customer'
