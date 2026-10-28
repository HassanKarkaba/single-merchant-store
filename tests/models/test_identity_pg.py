"""Actual PF Identity/AAC tests on an independently preexisting disposable target.

No credential, OTP, CSRF or factor secret is printed or included in evidence.
"""
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import secrets
import sys
import uuid
import pytest

pytestmark = pytest.mark.native


from app.models import identity
from app.models import authorization as aac
from app.models import persistence
from app.models import disposable as guards

@pytest.fixture(scope='module')
def setup():
 dsn=os.environ.get('SME_TEST_DSN')
 if not dsn:pytest.fail('explicit guarded disposable fixture required')
 guards.DisposableResourceGuard.from_environment().attest_dsn(dsn)
 db=persistence.PostgresPersistence(dsn);db.migrate_core();db.initialize()
 authority=aac.HostBoundPgAuthority(db);authority.migrate();authority.initialize()
 import pyotp
 class SecretProvider:
  def __init__(self):self.values={}
  def resolve(self,reference):return self.values[reference]
 provider=SecretProvider();merchant=str(uuid.uuid4())
 receipt_key=secrets.token_bytes(32)
 owner=identity.PortfolioDemoIdentity(db,merchant_id=merchant,profile='demo-test',authority=authority,secret_provider=provider,receipt_key_provider=lambda:receipt_key)
 owner.migrate();owner.initialize();authority.publication_verifier=owner.verify_publication_receipt
 db.migrate('pf_identity_test',1,'CREATE TABLE pf_identity_probe(id text PRIMARY KEY)')
 return db,authority,owner,provider

def provision(setup,kind='customer'):
 db,authority,owner,provider=setup;pid=str(uuid.uuid4());password=secrets.token_urlsafe(32);username='u'+uuid.uuid4().hex
 factor_ref=None;factor_version=0
 if kind=='staff':
  import pyotp
  factor_ref='factor-'+pid;provider.values[factor_ref]=pyotp.random_base32();factor_version=1
 owner.provision_account(principal_id=pid,username=username,roles=['merchant_admin'] if kind=='staff' else ['customer'],password=password,factor_ref=factor_ref,factor_version=factor_version)
 return pid,username,password,factor_ref

def login(setup,username,password):
 owner=setup[2];pre=owner.bootstrap_preauth()
 return owner.authenticate(username,password,preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key=str(uuid.uuid4()))

def guard_for(setup,actor):
 db,authority,owner,provider=setup
 with db.transaction('authority') as tx:
  epochs={kind:tx.execute('SELECT epoch FROM commerce_authority_gates WHERE merchant_id=%s AND kind=%s AND subject_id=%s',(owner.merchant_id,kind,subject)).fetchone()['epoch'] for kind,subject in [('policy',owner.merchant_id),('principal',actor['id']),('session',actor['session_id']),('service','sme-web')]}
 native=authority.guard(merchant_id=owner.merchant_id,principal_id=actor['id'],session_id=actor['session_id'],service_id='sme-web',policy_evaluator=lambda snapshots,owner_name,action:True,expected_epochs=epochs,guest=actor['kind']=='guest')
 def admit(tx):
  native.admit(tx);owner.admit_session(tx,actor);return True
 return admit

def test_customer_password_publication_native_admission_and_logout(setup):
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup)
 with pytest.raises(identity.IdentityError):login(setup,user,password+'wrong')
 result=login(setup,user,password);assert result.public['actor']=={'id':pid,'kind':'customer','roles':['customer']}
 actor=owner.resolve_session(result.credential)
 with db.transaction('pf_identity_test',authority_guard=guard_for(setup,actor)) as tx:tx.execute('INSERT INTO pf_identity_probe VALUES(%s)',(str(uuid.uuid4()),))
 owner.logout(result.credential)
 with pytest.raises(identity.IdentityError):owner.resolve_session(result.credential)
 with pytest.raises((identity.IdentityError,aac.AuthorityDenied)):
  with db.transaction('pf_identity_test',authority_guard=guard_for(setup,actor)):pass

def test_staff_password_is_unprivileged_then_exact_mfa_counter_is_one_use(setup):
 import pyotp
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup,'staff')
 challenge=login(setup,user,password)
 assert challenge.public['actor'] is None and challenge.credential is None
 assert challenge.public['challenge']['state']=='mfa_required'
 challenge_id=challenge.public['challenge']['id'];csrf=challenge.public['csrf_token']
 with pytest.raises(identity.IdentityError):owner.verify_staff_totp(challenge_id,'000000',challenge_credential=challenge.challenge_credential,preauth_credential=challenge.preauth_credential,csrf=csrf)
 with db.transaction('demo_identity') as tx:now=tx.fetchone('SELECT clock_timestamp() now')['now']
 code=pyotp.TOTP(provider.values[ref]).at(now)
 session=owner.verify_staff_totp(challenge_id,code,challenge_credential=challenge.challenge_credential,preauth_credential=challenge.preauth_credential,csrf=csrf)
 assert session.public['actor']['kind']=='staff'
 with pytest.raises(identity.IdentityError):owner.verify_staff_totp(challenge_id,code,challenge_credential=challenge.challenge_credential,preauth_credential=challenge.preauth_credential,csrf=csrf)
 actor=owner.resolve_session(session.credential)
 with db.transaction('pf_identity_test',authority_guard=guard_for(setup,actor)):pass

def test_reset_closes_old_authority_and_new_login_reopens_only_new_session(setup):
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup)
 old=login(setup,user,password);actor=owner.resolve_session(old.credential)
 replacement=secrets.token_urlsafe(32)
 current=owner.provision_account(principal_id=pid,username=user,roles=['customer'],password=replacement,expected_identity_revision=1)
 assert current['revision']>1
 with pytest.raises(identity.IdentityError):owner.resolve_session(old.credential)
 with pytest.raises(identity.IdentityError):login(setup,user,password)
 new=login(setup,user,replacement);assert new.public['actor']['id']==pid
 with db.transaction('authority') as tx:
  assert tx.fetchone("SELECT count(*) n FROM commerce_authority_operations WHERE merchant_id=%s AND state<>'PUBLISHED'",(owner.merchant_id,))['n']==0

def test_expiry_native_role_tampering_and_preauth_logout_deny(setup):
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup)
 session=login(setup,user,password);actor=owner.resolve_session(session.credential)
 forged=dict(actor,roles=['merchant_admin'])
 with pytest.raises(identity.IdentityError):
  with db.transaction('pf_identity_test') as tx:owner.admit_session(tx,forged)
 with db.transaction('demo_identity') as tx:tx.execute("UPDATE demo_identity.sessions SET idle_expires_at=clock_timestamp() WHERE id=%s",(actor['session_id'],))
 with pytest.raises(identity.IdentityError):owner.resolve_session(session.credential)
 pre=owner.bootstrap_preauth();owner.logout(preauth_credential=pre.preauth_credential)
 with pytest.raises(identity.IdentityError):owner.resolve_session(pre.preauth_credential)

def test_five_password_failures_are_persistent_and_sixth_is_rate_denied(setup):
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup)
 pre=owner.bootstrap_preauth();source=str(uuid.uuid4())
 for _ in range(5):
  with pytest.raises(identity.IdentityError) as error:owner.authenticate(user,password+'wrong',preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key=source)
  assert error.value.status==401
 with pytest.raises(identity.IdentityError) as error:owner.authenticate(user,password,preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key=source)
 assert error.value.status==429

def test_five_bad_otp_attempts_revoke_challenge_and_never_advance_factor(setup):
 import pyotp
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup,'staff');challenge=login(setup,user,password)
 with db.transaction('demo_identity') as tx:now=tx.fetchone('SELECT clock_timestamp() now')['now']
 counter=int(now.timestamp())//30;codes={pyotp.TOTP(provider.values[ref]).at(n*30) for n in (counter-1,counter,counter+1)}
 invalid=next(str(n).zfill(6) for n in range(10) if str(n).zfill(6) not in codes)
 for _ in range(5):
  with pytest.raises(identity.IdentityError):owner.verify_staff_totp(challenge.public['challenge']['id'],invalid,challenge_credential=challenge.challenge_credential,preauth_credential=challenge.preauth_credential,csrf=challenge.public['csrf_token'])
 with db.transaction('demo_identity') as tx:
  assert tx.fetchone('SELECT last_accepted_counter FROM demo_identity.factor_counters WHERE principal_id=%s',(pid,))['last_accepted_counter']==-1
  assert tx.fetchone('SELECT state FROM demo_identity.preauth_challenges WHERE id=%s',(challenge.public['challenge']['id'],))['state']=='REVOKED'

def test_touch_closes_and_republishes_expiry_without_extending_absolute_limit(setup):
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup);result=login(setup,user,password);actor=owner.resolve_session(result.credential)
 with db.transaction('demo_identity') as tx:tx.execute("UPDATE demo_identity.sessions SET last_activity=clock_timestamp()-interval '2 minutes' WHERE id=%s",(actor['session_id'],))
 touched=owner.touch(result.credential,str(uuid.uuid4()));current=owner.resolve_session(result.credential)
 assert current['epoch']==actor['epoch']+1 and current['absolute_expires_at']==actor['absolute_expires_at']
 with db.transaction('pf_identity_test',authority_guard=guard_for(setup,current)):pass

def test_publication_mac_tampering_and_missing_key_fail_closed(setup):
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup);result=login(setup,user,password);actor=owner.resolve_session(result.credential)
 with db.transaction(owner.owner) as tx:
  op=tx.fetchone('SELECT publication_id FROM demo_identity.sessions WHERE id=%s',(actor['session_id'],))['publication_id']
  tx.execute("UPDATE demo_identity.operation_receipts SET receipt_mac=%s WHERE operation_id=%s",('0'*64,op))
 with pytest.raises(identity.IdentityError,match='invalid_identity_receipt_signature'):
  with db.transaction('authority') as tx:owner.verify_publication_receipt(tx,{'receipt_id':op,'operation_id':op})
 previous=owner.receipt_key_provider;owner.receipt_key_provider=None
 try:
  with pytest.raises(identity.IdentityError,match='identity_receipt_key_unavailable'):login(setup,user,password)
 finally:owner.receipt_key_provider=previous
 # A failed new mint cannot turn its partial native row into ACTIVE authority.
 with db.transaction(owner.owner) as tx:assert tx.fetchone("SELECT count(*) n FROM demo_identity.sessions WHERE principal_id=%s AND state='ACTIVE'",(pid,))['n']==1

def test_private_reset_checks_native_revision_again_before_final_write(setup):
 db,authority,owner,provider=setup;pid,user,password,ref=provision(setup)
 with db.transaction(owner.owner) as tx:before=tx.fetchone('SELECT * FROM demo_identity.accounts WHERE id=%s',(pid,))
 original=owner.invalidate_account
 def racing_invalidation(principal_id,**kwargs):
  result=original(principal_id,**kwargs)
  with db.transaction(owner.owner) as tx:tx.execute('UPDATE demo_identity.accounts SET revision=revision+1 WHERE id=%s',(principal_id,))
  return result
 owner.invalidate_account=racing_invalidation
 try:
  with pytest.raises(identity.IdentityError,match='identity_version_conflict'):owner.provision_account(principal_id=pid,username=user,roles=['customer'],password=__import__('secrets').token_urlsafe(32),expected_identity_revision=before['revision'])
 finally:owner.invalidate_account=original
 with db.transaction(owner.owner) as tx:
  after=tx.fetchone('SELECT * FROM demo_identity.accounts WHERE id=%s',(pid,));assert after['password_hash']==before['password_hash'] and after['active'] is False
