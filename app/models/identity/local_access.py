"""Read-only validation of generated local-demo bootstrap material.

This private model port returns flags, never passwords or factor secrets. It
neither authenticates a browser session nor changes native credentials.
"""
from __future__ import annotations

import hmac

from .errors import IdentityError
from .service import canonical_uuid, hash_password


def inspect_bootstrap_credentials(identity, records):
    """Compare setup records with one locked snapshot of native Identity rows."""
    if identity.profile != 'demo-test' or identity._closed:
        raise IdentityError('local_demo_profile_required', 403)
    if not isinstance(records, list) or not 1 <= len(records) <= 3:
        raise IdentityError('local_demo_records_required', 422)
    fields = {'label', 'principal_id', 'username', 'roles', 'password',
              'factor_ref', 'factor_version', 'factor_secret'}
    for record in records:
        if type(record) is not dict or set(record) != fields:
            raise IdentityError('local_demo_records_required', 422)
        canonical_uuid(record['principal_id'])
        if record['label'] not in ('customerA', 'customerB', 'merchant'):
            raise IdentityError('local_demo_records_required', 422)
        if not isinstance(record['password'], str) or not 24 <= len(record['password']) <= 1024:
            raise IdentityError('local_demo_records_required', 422)
    if len({record['principal_id'] for record in records}) != len(records):
        raise IdentityError('local_demo_records_required', 422)
    result = []
    with identity.db.transaction(identity.owner) as tx:
        identity._generation_lock(tx)
        checked_at = tx.execute('SELECT clock_timestamp() now').fetchone()['now']
        for record in sorted(records, key=lambda value: value['principal_id']):
            account = tx.execute('SELECT * FROM demo_identity.accounts WHERE id=%s FOR SHARE',
                                 (record['principal_id'],)).fetchone()
            kind = 'staff' if record['label'] == 'merchant' else 'customer'
            roles = ['merchant_admin'] if kind == 'staff' else ['customer']
            bound = bool(account and account['merchant_id'] == identity.merchant_id
                         and account['username'] == record['username']
                         and account['kind'] == kind and account['roles'] == roles
                         and record['roles'] == roles
                         and account.get('customer_registration_state') == 'private')
            active = bool(bound and account['active'])
            password_current = bool(active and account['password_salt'] is not None
                                    and account['password_hash'] is not None
                                    and hmac.compare_digest(
                                        hash_password(record['password'], bytes(account['password_salt'])),
                                        bytes(account['password_hash'])))
            factor_current = None
            if kind == 'staff':
                counter = tx.execute('SELECT factor_version FROM demo_identity.factor_counters WHERE principal_id=%s FOR SHARE',
                                     (record['principal_id'],)).fetchone() if bound else None
                try:
                    fingerprint = identity._generation_fingerprint(record['factor_secret'])
                except IdentityError:
                    fingerprint = None
                factor_current = bool(active and counter
                                      and account['factor_ref'] == record['factor_ref']
                                      and account['factor_version'] == record['factor_version']
                                      and counter['factor_version'] == account['factor_version']
                                      and fingerprint is not None
                                      and isinstance(account.get('factor_fingerprint'), str)
                                      and hmac.compare_digest(account['factor_fingerprint'], fingerprint))
            elif bound and (account['factor_ref'] is not None or account['factor_version'] != 0):
                bound = active = password_current = False
            result.append({'label': record['label'], 'principal_id': record['principal_id'],
                           'username': record['username'], 'account_current': active,
                           'password_current': password_current, 'factor_current': factor_current,
                           'usable': password_current and factor_current is not False,
                           'revision': account['revision'] if bound else None,
                           'checked_at': checked_at.isoformat()})
    return result
