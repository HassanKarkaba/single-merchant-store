from __future__ import annotations
import re
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class ProviderBindings:

    def bind_dispatch(self, guard):
        if not callable(guard):
            raise CommerceError('invalid_input', 'Trusted owner dispatch guard required')
        self.dispatch_guard = guard

    def bind_inspection(self, guard):
        if not callable(guard):
            raise CommerceError('invalid_input', 'Trusted owner inspection guard required')
        self.inspection_guard = guard


    def bind_test_checkpoint(self, callback, *, run_id, case_id, actor=None):
        if self.profile != 'demo-test' or self.test_guard is None or self.test_guard(actor, run_id, case_id) is not True or (not callable(callback)):
            raise CommerceError('not_found', 'Private checkpoint control unavailable')
        _client_command(run_id)
        if not isinstance(case_id, str) or not re.fullmatch('(?:PAC|BM)[0-9]{2}(?:-[A-Za-z0-9_-]+)?', case_id):
            raise CommerceError('invalid_input', 'Exact controlled case required')
        self.test_checkpoint = callback



    def complete_action(self, *args, **kwargs):
        raise CommerceError('not_supported', 'Selected instruments resolve through durable simulator time; no completion shortcut exists')


    def get_operation_readiness(self, provider, operation):
        allowed = {'stripe': {'create_authorization', 'capture', 'cancel', 'refund'}, 'easypost': {'create_shipment', 'buy_label', 'cancel_label'}, 'ses': {'send'}}
        return {'profile': self.profile, 'ready': operation in allowed.get(provider, set()) and self.dispatch_guard is not None and (self.inspection_guard is not None), 'live_ready': False, 'simulated': True}


    def inject_fault(self, provider, operation, fault, count=1, *, run_id=None, case_id=None, effect_key=None, actor=None):
        if self.profile != 'demo-test' or self.test_guard is None or self.test_guard(actor, run_id, case_id) is not True:
            raise CommerceError('not_found', 'Test control unavailable')
        _client_command(run_id)
        _client_command(effect_key)
        _integer(count, 1, 100)
        if not isinstance(case_id, str) or not re.fullmatch('(?:PAC|BM)[0-9]{2}(?:-[A-Za-z0-9_-]+)?', case_id) or fault not in ('lost_reply', 'decline', 'unavailable_before_send', 'pending'):
            raise CommerceError('invalid_input', 'Run-scoped finite fault plan required')
        ident = str(uuid.uuid4())
        with self.db.transaction(self.owner) as tx:
            _provider_snapshot_shared(tx)
            tx.execute('INSERT INTO integrations.fault_plans VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', (ident, run_id, case_id, provider, operation, effect_key, fault, count))
        return {'id': ident, 'state': 'configured', 'remaining': count, 'run_id': run_id, 'case_id': case_id}

def _client_command(command_id):
    if command_id is None:
        return str(uuid.uuid4())
    try:
        value = uuid.UUID(command_id)
    except (ValueError, TypeError, AttributeError):
        raise CommerceError('invalid_input', 'command_id must be canonical UUID text')
    if str(value) != command_id:
        raise CommerceError('invalid_input', 'command_id must be canonical UUID text')
    return command_id

def _integer(value, minimum=0, maximum=99999999):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CommerceError('invalid_input', 'Expected a bounded integer')
    return value

def _provider_snapshot_shared(tx):
    tx.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', ('sme.integrations.survivor.snapshot.v1',))
