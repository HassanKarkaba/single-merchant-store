"""Explicit terminal-only access to generated local-demo bootstrap accounts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import uuid

from app.config import (ROOT, guarded_test_context, read_secret, resolve_database,
                        strict_object, validate_runtime_binding)
from app.models.identity import PortfolioDemoIdentity
from app.models.identity.local_access import inspect_bootstrap_credentials
from app.models.persistence import PostgresPersistence


LABELS = ('customerA', 'customerB', 'merchant')


def _private_path(path, root=None):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('local_demo_private_path_required')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('local_demo_symlink_denied')
    if root is not None and root not in path.parents:
        raise ValueError('local_demo_foreign_setup_denied')
    return path


def _private_json(path, *, root=None, limit=65536):
    path = _private_path(path, root)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid() or info.st_size > limit):
                raise ValueError('local_demo_protected_setup_required')
            raw = os.read(fd, limit + 1)
        finally:
            os.close(fd)
        if len(raw) > limit:
            raise ValueError('local_demo_protected_setup_required')
        return json.loads(raw, object_pairs_hook=strict_object)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('local_demo_protected_setup_required') from None


def _setup_records(profile_path, config):
    """Admit the matching generated setup, never an arbitrary secret path."""
    profile = _private_path(Path(profile_path).expanduser().absolute())
    state = profile.parent
    mode = state.stat()
    if (profile.name != 'profile.json' or state == ROOT or ROOT in state.parents
            or not stat.S_ISDIR(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o700
            or mode.st_uid != os.getuid()):
        raise ValueError('local_demo_private_state_required')
    stored = _private_json(profile)
    if stored != config or config.get('profile') != 'demo-test':
        raise ValueError('local_demo_profile_required')
    if (config.get('database_ref') != 'file:' + str(state / 'database/runtime-dsn')
            or config.get('disposable_proof_file') != str(state / 'database/disposable-proof.json')
            or config.get('totp_root') != str(state / 'totp')):
        raise ValueError('local_demo_generated_instance_required')
    proof = _private_json(config['disposable_proof_file'], root=state, limit=16384)
    run = str(uuid.UUID(proof['run_id']))
    if (run != proof['run_id'] or proof.get('environment_id') != config.get('environment_id')
            or proof.get('profile') != 'demo-test'
            or proof.get('provisioner') != 'operations.new-disposable-fixture/1'):
        raise ValueError('local_demo_setup_identity_mismatch')
    setup = _private_json(state / 'reviewer-setup.json', root=state)
    if type(setup) is not dict or not set(LABELS) <= set(setup) or set(setup) - set(LABELS) - {'mfaReviewer', 'seedMaintainer'}:
        raise ValueError('local_demo_setup_schema_required')
    records = []
    fields = {'environment', 'principal_id', 'username', 'roles', 'password_ref',
              'totp_secret_ref', 'factor_version', 'expected_identity_revision'}
    for label in LABELS:
        staff = label == 'merchant'
        entry = setup[label]
        expected_entry = {'username', 'password_file', 'totp_secret_file'} if staff else {'username', 'password_file'}
        if type(entry) is not dict or set(entry) != expected_entry:
            raise ValueError('local_demo_setup_schema_required')
        record = _private_json(state / (label + '-provision.json'), root=state)
        password_path = state / (label + '-password')
        factor_path = state / 'totp' / (label + '-totp') if staff else None
        roles = ['merchant_admin'] if staff else ['customer']
        if (type(record) is not dict or set(record) != fields
                or record['environment'] != 'demo-test'
                or record['username'] != label + '_' + uuid.UUID(run).hex[:12]
                or entry['username'] != record['username'] or record['roles'] != roles
                or record['password_ref'] != 'file:' + str(password_path)
                or entry['password_file'] != str(password_path)
                or record['totp_secret_ref'] != ('file:' + str(factor_path) if staff else None)
                or (staff and entry['totp_secret_file'] != str(factor_path))
                or record['factor_version'] != (1 if staff else 0)
                or record['expected_identity_revision'] != 0):
            raise ValueError('local_demo_setup_identity_mismatch')
        principal = str(uuid.UUID(record['principal_id']))
        if principal != record['principal_id']:
            raise ValueError('local_demo_setup_identity_mismatch')
        _private_path(password_path, state)
        if staff:
            _private_path(factor_path, state)
        records.append({'label': label, 'principal_id': principal, 'username': record['username'],
                        'roles': roles, 'password': read_secret(record['password_ref']),
                        'factor_ref': record['totp_secret_ref'], 'factor_version': record['factor_version'],
                        'factor_secret': read_secret(record['totp_secret_ref']).strip() if staff else None})
    return run, records


def _local_capture_access(profile_path, config):
    """Read only this generated demo's optional local mailbox credential."""
    mail = config.get('mail')
    if type(mail) is not dict or mail.get('mode') != 'local_capture':
        return None
    from app.models.notifications.mail_config import validate_mail_config
    validate_mail_config(mail)
    state = Path(profile_path).expanduser().absolute().parent
    password_path = state / 'mail-capture-password'
    capture_path = _private_path(state / 'mail-capture', state)
    if (mail['password_ref'] != 'file:' + str(password_path)
            or mail['capture']['password_ref'] != mail['password_ref']
            or mail['capture']['directory'] != str(capture_path)
            or mail['public_origin'] != config.get('origin')):
        raise ValueError('local_demo_mail_binding_required')
    mode = capture_path.stat()
    if (not stat.S_ISDIR(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o700
            or mode.st_uid != os.getuid()):
        raise ValueError('local_demo_private_mail_required')
    _private_path(password_path, state)
    password = read_secret(mail['password_ref']).strip()
    if not 1 <= len(password) <= 256 or any(ord(char) < 33 or ord(char) == 127 for char in password):
        raise ValueError('local_demo_mail_credential_required')
    url = (config['origin'] + '/local-mail/' if os.environ.get('SME_CONTAINER_RUNTIME') == '1'
           else 'http://127.0.0.1:' + str(mail['capture']['http_port']) + '/')
    return {'url': url, 'username': 'operator', 'password': password}


def _validated_records(profile_path, config):
    run, records = _setup_records(profile_path, config)
    guarded_test_context(config, run)
    validate_runtime_binding(config)
    db = PostgresPersistence(resolve_database(config))
    identity = PortfolioDemoIdentity(db, merchant_id=config['merchant_id'], profile='demo-test')
    try:
        identity.initialize()
        statuses = inspect_bootstrap_credentials(identity, records)
    finally:
        identity.close()
    return records, statuses


def inspect_local_access(profile_path, config):
    """Safe startup check: return validation metadata only, with no secret fields."""
    _, statuses = _validated_records(profile_path, config)
    return {'state': 'CURRENT' if all(item['usable'] for item in statuses) else 'STALE',
            'accounts': statuses}


def show_local_access(profile_path, config, *, stream=None):
    """Reveal current credentials on deliberate TTY invocation, never redirect.

    Native checks describe a database snapshot. A later concurrent rotation may
    make any printed credential stale; no bootstrap material is rewritten.
    """
    stream = sys.stdout if stream is None else stream
    if not stream.isatty():
        raise ValueError('local_access_interactive_terminal_required')
    records, statuses = _validated_records(profile_path, config)
    mailbox = _local_capture_access(profile_path, config)
    by_label = {record['label']: record for record in records}
    lines = ['LOCAL DEMO CREDENTIALS — this command intentionally reveals secrets to this terminal.',
             'Do not paste this output into logs, issues, source control, or public messages.',
             'Validation is a database snapshot; this command changes no account or factor.', '']
    import pyotp
    for status in sorted(statuses, key=lambda item: LABELS.index(item['label'])):
        record = by_label[status['label']]
        lines.extend([status['label'] + ': ' + record['username'],
                      '  Checked at: ' + status['checked_at'],
                      '  Bootstrap access: ' + ('CURRENT' if status['usable'] else 'STALE OR UNAVAILABLE')])
        if status['revision'] is not None:
            lines.append('  Private principal: ' + status['principal_id'] + '; current identity revision: ' + str(status['revision']))
        if status['password_current']:
            lines.append('  Password: ' + record['password'])
        else:
            lines.append('  Password: STALE — changed passwords cannot be recovered from their hashes.')
        if status['factor_current'] is True:
            uri = pyotp.TOTP(record['factor_secret']).provisioning_uri(name=record['username'], issuer_name='Single Merchant Local Demo')
            lines.extend(['  Staff sign-in requires password plus a current authenticator code.',
                          '  Authenticator enrollment URI: ' + uri])
        elif status['factor_current'] is False:
            lines.append('  Authenticator enrollment: STALE — the saved factor is no longer current.')
        if not status['usable']:
            lines.extend(['  Use the supported private provision-reviewer/reset-password procedure with a NEW protected',
                          '  input file and the CURRENT identity revision; review factor_ref/version before rotation.',
                          '  This helper never resets, rotates, or republishes credentials.'])
        lines.append('')
    if mailbox is not None:
        lines.extend(['Local verification/reset mailbox (generated local capture only):',
                      '  URL: ' + mailbox['url'], '  Username: ' + mailbox['username'],
                      '  Password: ' + mailbox['password'],
                      '  These are local demo messages; external SMTP credentials are never displayed.', ''])
    stream.write('\n'.join(lines) + '\n')
    stream.flush()
