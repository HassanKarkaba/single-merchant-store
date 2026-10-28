"""Real PostgreSQL checks for private local access and expiry-only renewal."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import secrets
import threading
import time
import uuid

import pytest

from app.models.authorization import AuthorityDenied, HostBoundPgAuthority
from app.models.disposable import DisposableResourceGuard
from app.models.identity import PortfolioDemoIdentity
from app.models.identity.local_access import inspect_bootstrap_credentials
from app.models.persistence import PostgresPersistence

pytestmark = pytest.mark.native


@pytest.fixture(scope='module')
def db():
    dsn = os.environ.get('SME_TEST_DSN')
    if not dsn:
        pytest.fail('explicit guarded disposable fixture required')
    DisposableResourceGuard.from_environment().attest_dsn(dsn)
    database = PostgresPersistence(dsn)
    database.migrate_core()
    database.initialize()
    return database


@pytest.fixture
def workload(db):
    merchant, machine = str(uuid.uuid4()), str(uuid.uuid4())
    reference = 'private-local-test-' + uuid.uuid4().hex
    secret = secrets.token_bytes(32)
    authority = HostBoundPgAuthority(db, workload_secret_provider=lambda _: secret)
    authority.migrate()
    authority.initialize()
    now = datetime.now(timezone.utc)
    authority.provision_workload(merchant, machine, 'sme-worker', reference,
                                issued_at=now-timedelta(seconds=1), expires_at=now+timedelta(days=30))
    return authority, merchant, machine, reference, secret


def snapshot(workload):
    authority, merchant, machine, reference, _ = workload
    with authority.persistence.transaction('authority') as tx:
        row = tx.fetchone('SELECT * FROM commerce_workload_credentials WHERE merchant_id=%s AND credential_ref=%s', (merchant, reference))
        gates = tx.fetchall('SELECT * FROM commerce_authority_gates WHERE merchant_id=%s ORDER BY kind,subject_id', (merchant,))
    return row, gates


def expire(workload):
    authority, merchant, _, reference, _ = workload
    with authority.persistence.transaction('authority') as tx:
        tx.execute("UPDATE commerce_workload_credentials SET issued_at=clock_timestamp()-interval '32 days',expires_at=clock_timestamp()-interval '2 days' WHERE merchant_id=%s AND credential_ref=%s", (merchant, reference))


def renew(workload):
    authority, merchant, machine, reference, _ = workload
    return authority.renew_expired_workload(merchant, machine, 'sme-worker', reference)


def test_current_lifetime_not_extended_expired_secret_renewed_once(workload):
    before, gates = snapshot(workload)
    assert renew(workload)['state'] == 'CURRENT'
    assert snapshot(workload) == (before, gates)
    expire(workload)
    expired, _ = snapshot(workload)
    receipt = renew(workload)
    after, after_gates = snapshot(workload)
    assert receipt['state'] == 'RENEWED' and receipt['version'] == expired['version']+1
    assert after_gates == gates
    assert after['credential_hash'] == expired['credential_hash']
    assert after['credential_epoch'] == expired['credential_epoch']+1
    assert after['issued_at'] > expired['expires_at']
    assert after['expires_at']-after['issued_at'] == timedelta(days=30)
    assert renew(workload)['state'] == 'CURRENT'
    assert snapshot(workload) == (after, after_gates)


@pytest.mark.parametrize('kind', ['policy', 'principal', 'service'])
def test_closed_gates_never_reopened_by_expiry_renewal(workload, kind):
    authority, merchant, _, _, _ = workload
    expire(workload)
    with authority.persistence.transaction('authority') as tx:
        tx.execute('UPDATE commerce_authority_gates SET is_open=false,epoch=epoch+1 WHERE merchant_id=%s AND kind=%s', (merchant, kind))
    before = snapshot(workload)
    with pytest.raises(AuthorityDenied, match='WORKLOAD_GATE_CLOSED_OR_EXPIRED'):
        renew(workload)
    assert snapshot(workload) == before


def test_revocation_and_changed_secret_never_renewed(workload):
    authority, merchant, machine, reference, _ = workload
    expire(workload)
    before = snapshot(workload)
    authority.workload_secret_provider = lambda _: secrets.token_bytes(32)
    with pytest.raises(AuthorityDenied, match='WORKLOAD_RENEWAL_REJECTED'):
        renew(workload)
    assert snapshot(workload) == before
    authority.workload_secret_provider = lambda _: workload[-1]
    authority.revoke_workload(merchant, machine, 'sme-worker', reference, expected_revision=1)
    revoked = snapshot(workload)
    with pytest.raises(AuthorityDenied):
        renew(workload)
    assert snapshot(workload) == revoked


def test_concurrent_startups_renew_exactly_once(workload):
    expire(workload)
    before, gates = snapshot(workload)
    barrier = threading.Barrier(2)
    def call():
        barrier.wait(timeout=10)
        return renew(workload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: call(), range(2)))
    after, after_gates = snapshot(workload)
    assert sorted(item['state'] for item in receipts) == ['CURRENT', 'RENEWED']
    assert after['version'] == before['version']+1 and after_gates == gates
    assert receipts[0]['expected_epochs'] == receipts[1]['expected_epochs']


def test_distinct_existing_credentials_have_distinct_renewal_audit_receipts(workload):
    authority, merchant, machine, reference, _ = workload
    second = reference+'-second'
    now = datetime.now(timezone.utc)
    authority.provision_workload(merchant, machine, 'sme-worker', second,
                                issued_at=now-timedelta(seconds=1), expires_at=now+timedelta(days=30))
    expire(workload)
    with authority.persistence.transaction('authority') as tx:
        tx.execute("UPDATE commerce_workload_credentials SET issued_at=clock_timestamp()-interval '32 days',expires_at=clock_timestamp()-interval '2 days' WHERE merchant_id=%s AND credential_ref=%s", (merchant, second))
    assert renew(workload)['state'] == 'RENEWED'
    assert authority.renew_expired_workload(merchant, machine, 'sme-worker', second)['state'] == 'RENEWED'
    with authority.persistence.transaction('authority') as tx:
        rows = tx.fetchall("SELECT event_id FROM commerce_audit_staging WHERE owner='authority' AND payload->>'operation'='workload_expiry_renewal' AND payload->>'machine_principal_id'=%s", (machine,))
    assert len(rows) == len({row['event_id'] for row in rows}) == 2


def test_completed_native_gate_close_cannot_be_undone_by_renewal(workload):
    authority, merchant, machine, _, _ = workload
    expire(workload)
    authority.prepare_change(merchant, [('principal', machine)], str(uuid.uuid4()))
    before = snapshot(workload)
    with pytest.raises(AuthorityDenied, match='WORKLOAD_GATE_CLOSED_OR_EXPIRED'):
        renew(workload)
    assert snapshot(workload) == before


def test_concurrent_gate_close_commits_before_waiting_renewal_denies(workload):
    authority, merchant, machine, _, _ = workload
    expire(workload)
    started = threading.Event()
    def call():
        started.set()
        return renew(workload)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with authority.persistence.transaction('authority') as closing:
            closing.execute("UPDATE commerce_authority_gates SET is_open=false,epoch=epoch+1 WHERE merchant_id=%s AND kind='principal' AND subject_id=%s", (merchant, machine))
            future = pool.submit(call)
            assert started.wait(timeout=5)
            deadline = time.monotonic()+5
            waiting = False
            while time.monotonic()<deadline:
                with authority.persistence.transaction('authority') as probe:
                    row = probe.fetchone("SELECT count(*) n FROM pg_stat_activity WHERE usename=current_user AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE 'SELECT *, (expires_at IS NULL OR expires_at>clock_timestamp()) AS current_time_valid FROM commerce_authority_gates%%'")
                if row['n']:
                    waiting = True
                    break
                time.sleep(0.01)
            assert waiting and not future.done()
        with pytest.raises(AuthorityDenied, match='WORKLOAD_GATE_CLOSED_OR_EXPIRED'):
            future.result(timeout=10)
    row, gates = snapshot(workload)
    assert row['version'] == 1 and row['credential_epoch'] == 1
    assert next(gate for gate in gates if gate['kind']=='principal')['is_open'] is False


def test_native_bootstrap_snapshot_detects_password_factor_and_identity_changes(db):
    class Factors:
        values = {}
        def resolve(self, reference):
            return self.values[reference]
    factors = Factors()
    receipt_key = secrets.token_bytes(32)
    owner = PortfolioDemoIdentity(db, merchant_id=str(uuid.uuid4()), profile='demo-test',
                                 secret_provider=factors, receipt_key_provider=lambda: receipt_key)
    owner.migrate()
    owner.initialize()
    records = []
    for label in ('customerA', 'customerB', 'merchant'):
        staff = label == 'merchant'
        principal = str(uuid.uuid4())
        factor_ref = 'operator-factor-' + principal if staff else None
        factor = __import__('pyotp').random_base32() if staff else None
        if staff:
            factors.values[factor_ref] = factor
        record = {'label': label, 'principal_id': principal, 'username': 'local_' + uuid.uuid4().hex,
                  'roles': ['merchant_admin'] if staff else ['customer'], 'password': secrets.token_urlsafe(36),
                  'factor_ref': factor_ref, 'factor_version': 1 if staff else 0, 'factor_secret': factor}
        owner.provision_account(**{key: record[key] for key in ('principal_id', 'username', 'roles', 'password', 'factor_ref', 'factor_version')})
        records.append(record)
    with db.transaction('demo_identity') as tx:
        before_accounts = tx.fetchall('SELECT * FROM demo_identity.accounts WHERE merchant_id=%s ORDER BY id', (owner.merchant_id,))
        before_sessions = tx.fetchone('SELECT count(*) n FROM demo_identity.sessions')['n']
    statuses = inspect_bootstrap_credentials(owner, records)
    assert all(item['usable'] for item in statuses)
    encoded = json.dumps(statuses)
    assert all(item['password'] not in encoded for item in records)
    assert records[-1]['factor_secret'] not in encoded
    with db.transaction('demo_identity') as tx:
        assert before_accounts == tx.fetchall('SELECT * FROM demo_identity.accounts WHERE merchant_id=%s ORDER BY id', (owner.merchant_id,))
        assert before_sessions == tx.fetchone('SELECT count(*) n FROM demo_identity.sessions')['n']
    changed_password = [dict(item) for item in records]
    changed_password[0]['password'] = secrets.token_urlsafe(36)
    flags = {item['label']: item for item in inspect_bootstrap_credentials(owner, changed_password)}
    assert not flags['customerA']['password_current'] and flags['merchant']['usable']
    changed_factor = [dict(item) for item in records]
    changed_factor[-1]['factor_secret'] = __import__('pyotp').random_base32()
    flags = {item['label']: item for item in inspect_bootstrap_credentials(owner, changed_factor)}
    assert flags['merchant']['password_current'] and not flags['merchant']['factor_current']
    foreign = PortfolioDemoIdentity(db, merchant_id=str(uuid.uuid4()), profile='demo-test')
    assert not any(item['usable'] for item in inspect_bootstrap_credentials(foreign, records))
    authority = HostBoundPgAuthority(db)
    authority.migrate()
    owner.authority = authority
    authority.publication_verifier = owner.verify_publication_receipt
    customer = records[0]
    owner.provision_account(principal_id=customer['principal_id'], username=customer['username'],
                            roles=customer['roles'], password=secrets.token_urlsafe(36), expected_identity_revision=1)
    merchant = records[-1]
    replacement_ref = 'replacement-factor-' + merchant['principal_id']
    factors.values[replacement_ref] = __import__('pyotp').random_base32()
    owner.provision_account(principal_id=merchant['principal_id'], username=merchant['username'],
                            roles=merchant['roles'], password=merchant['password'], factor_ref=replacement_ref,
                            factor_version=2, expected_identity_revision=1)
    flags = {item['label']: item for item in inspect_bootstrap_credentials(owner, records)}
    assert not flags['customerA']['password_current'] and flags['customerA']['revision']>1
    assert flags['merchant']['password_current'] and not flags['merchant']['factor_current']
    assert flags['customerB']['usable']
