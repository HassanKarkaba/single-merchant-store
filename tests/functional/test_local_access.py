"""Private local account inspection never reveals secrets through ordinary output."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import uuid

import pytest

from app.controllers import local_access as access
from app.controllers.setup import private_file
import manage


class Terminal(io.StringIO):
    def isatty(self):
        return True


def generated(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / 'database').mkdir(mode=0o700)
    (tmp_path / 'totp').mkdir(mode=0o700)
    run = str(uuid.uuid4())
    config = {'profile': 'demo-test', 'environment_id': str(uuid.uuid4()),
              'merchant_id': str(uuid.uuid4()), 'database_ref': 'file:' + str(tmp_path / 'database/runtime-dsn'),
              'disposable_proof_file': str(tmp_path / 'database/disposable-proof.json'),
              'totp_root': str(tmp_path / 'totp')}
    private_file(tmp_path / 'profile.json', config)
    private_file(tmp_path / 'database/disposable-proof.json',
                 {'run_id': run, 'environment_id': config['environment_id'], 'profile': 'demo-test',
                  'provisioner': 'operations.new-disposable-fixture/1'})
    setup = {}
    for label in access.LABELS:
        staff = label == 'merchant'
        password_ref = private_file(tmp_path / (label + '-password'), 'example-only-' + label + '-password-not-a-real-credential')
        factor_ref = private_file(tmp_path / 'totp' / (label + '-totp'), 'A' * 32) if staff else None
        username = label + '_' + uuid.UUID(run).hex[:12]
        private_file(tmp_path / (label + '-provision.json'),
                     {'environment': 'demo-test', 'principal_id': str(uuid.uuid4()), 'username': username,
                      'roles': ['merchant_admin'] if staff else ['customer'], 'password_ref': password_ref,
                      'totp_secret_ref': factor_ref, 'factor_version': 1 if staff else 0,
                      'expected_identity_revision': 0})
        setup[label] = {'username': username, 'password_file': password_ref[5:],
                        **({'totp_secret_file': factor_ref[5:]} if staff else {})}
    private_file(tmp_path / 'reviewer-setup.json', setup)
    return tmp_path / 'profile.json', config


def test_cli_registers_deliberate_local_access():
    args = manage.build_parser().parse_args(['local-access', '--config', '/state/profile.json'])
    assert args.command == 'local-access' and args.config == '/state/profile.json'


def test_redirected_output_denied_before_reading_configuration(monkeypatch):
    monkeypatch.setattr(access, '_validated_records', lambda *_: pytest.fail('redirect read credentials'))
    with pytest.raises(ValueError, match='interactive_terminal_required'):
        access.show_local_access('/not-read/profile.json', {}, stream=io.StringIO())


def test_generated_setup_is_exact_and_protected(tmp_path):
    path, config = generated(tmp_path)
    before = {item: item.read_bytes() for item in tmp_path.rglob('*') if item.is_file()}
    run, records = access._setup_records(path, config)
    assert str(uuid.UUID(run)) == run
    assert [record['label'] for record in records] == list(access.LABELS)
    assert records[-1]['roles'] == ['merchant_admin']
    assert before == {item: item.read_bytes() for item in before}
    (tmp_path / 'merchant-provision.json').chmod(0o644)
    with pytest.raises(ValueError, match='protected_setup_required'):
        access._setup_records(path, config)


@pytest.mark.parametrize('change', ['profile', 'foreign_file', 'identity', 'symlink', 'state_mode'])
def test_unrelated_or_modified_setup_never_admitted(tmp_path, change):
    path, config = generated(tmp_path)
    if change == 'profile':
        config['profile'] = 'portfolio-demo'
        path.write_text(json.dumps(config))
    elif change == 'foreign_file':
        record_path = tmp_path / 'merchant-provision.json'
        record = json.loads(record_path.read_text())
        record['password_ref'] = 'file:/unrelated/password'
        record_path.write_text(json.dumps(record))
    elif change == 'identity':
        record_path = tmp_path / 'merchant-provision.json'
        record = json.loads(record_path.read_text())
        record['username'] = 'unrelated-operator'
        record_path.write_text(json.dumps(record))
    elif change == 'symlink':
        item = tmp_path / 'merchant-password'
        replacement = tmp_path / 'other-password'
        item.rename(replacement)
        item.symlink_to(replacement)
    else:
        tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        access._setup_records(path, config)


def validation_mock(monkeypatch, records, *, stale=False):
    statuses = [{'label': item['label'], 'username': item['username'], 'principal_id': item['principal_id'],
                 'account_current': True, 'password_current': not stale, 'factor_current': False if stale and item['label'] == 'merchant' else True if item['label'] == 'merchant' else None,
                 'usable': not stale, 'revision': 1, 'checked_at': datetime.now(timezone.utc).isoformat()} for item in records]
    monkeypatch.setattr(access, '_validated_records', lambda *_: (records, statuses))
    return statuses


def test_safe_startup_inspection_has_no_secret_fields(tmp_path, monkeypatch):
    path, config = generated(tmp_path)
    _, records = access._setup_records(path, config)
    validation_mock(monkeypatch, records)
    result = access.inspect_local_access(path, config)
    serialized = json.dumps(result)
    assert result['state'] == 'CURRENT'
    for item in records:
        assert item['password'] not in serialized
        if item['factor_secret']:
            assert item['factor_secret'] not in serialized
    assert 'otpauth://' not in serialized


def test_only_explicit_terminal_displays_current_credentials(tmp_path, monkeypatch):
    path, config = generated(tmp_path)
    _, records = access._setup_records(path, config)
    validation_mock(monkeypatch, records)
    terminal = Terminal()
    access.show_local_access(path, config, stream=terminal)
    rendered = terminal.getvalue()
    assert 'LOCAL DEMO CREDENTIALS' in rendered and 'otpauth://totp/' in rendered
    for record in records:
        assert record['password'] in rendered


def test_stale_bootstrap_is_withheld_and_never_rotated(tmp_path, monkeypatch):
    path, config = generated(tmp_path)
    _, records = access._setup_records(path, config)
    validation_mock(monkeypatch, records, stale=True)
    terminal = Terminal()
    access.show_local_access(path, config, stream=terminal)
    rendered = terminal.getvalue()
    assert 'STALE OR UNAVAILABLE' in rendered and 'CURRENT identity revision' in rendered
    assert 'otpauth://' not in rendered
    for record in records:
        assert record['password'] not in rendered


def add_local_mail(path, config):
    from app.controllers.setup import local_mail_defaults
    config['origin'] = 'https://localhost:8443'
    config['mail'] = local_mail_defaults(path.parent, config['origin'])
    path.write_text(json.dumps(config))
    return access.read_secret(config['mail']['password_ref'])


@pytest.mark.parametrize('container', [False, True])
def test_local_mailbox_revealed_only_on_explicit_terminal(tmp_path, monkeypatch, container):
    path, config = generated(tmp_path)
    password = add_local_mail(path, config)
    _, records = access._setup_records(path, config)
    validation_mock(monkeypatch, records)
    monkeypatch.setenv('SME_CONTAINER_RUNTIME', '1' if container else '0')
    terminal = Terminal()
    access.show_local_access(path, config, stream=terminal)
    output = terminal.getvalue()
    expected = config['origin']+'/local-mail/' if container else 'http://127.0.0.1:8025/'
    if password not in output or expected not in output or 'Username: operator' not in output:
        pytest.fail('explicit terminal omitted the generated local mailbox access')
    if password in json.dumps(access.inspect_local_access(path, config)):
        pytest.fail('safe startup inspector exposed a mailbox credential')


def test_external_smtp_credentials_never_read_or_displayed(tmp_path, monkeypatch):
    path, config = generated(tmp_path)
    _, records = access._setup_records(path, config)
    validation_mock(monkeypatch, records)
    config['mail'] = {'mode': 'smtp', 'password_ref': 'file:/unrelated/smtp-password'}
    monkeypatch.setattr(access, 'read_secret', lambda *_: pytest.fail('external SMTP secret was read'))
    terminal = Terminal()
    access.show_local_access(path, config, stream=terminal)
    assert 'Local verification/reset mailbox' not in terminal.getvalue()


def test_local_mail_secret_must_belong_to_generated_instance(tmp_path, monkeypatch):
    path, config = generated(tmp_path)
    add_local_mail(path, config)
    config['mail']['password_ref'] = config['mail']['capture']['password_ref'] = 'file:/unrelated/mail-password'
    monkeypatch.setattr(access, 'read_secret', lambda *_: pytest.fail('foreign mailbox secret was read'))
    with pytest.raises(ValueError, match='local_demo_mail_binding_required'):
        access._local_capture_access(path, config)
