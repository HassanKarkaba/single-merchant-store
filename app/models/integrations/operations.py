from __future__ import annotations
import hmac
import hashlib
import json
import re
import secrets
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class ProviderOperations:

    def bind_dispatch(self, guard):
        if not callable(guard):
            raise CommerceError('invalid_input', 'Trusted dispatch guard required')
        self.dispatch_guard = guard

    def complete_action(self, object_id, success=True, *, actor=None):
        if self.profile in ('demo-test', 'portfolio-demo'):
            raise CommerceError('not_supported', 'Selected instruments resolve by server-owned simulation; completion shortcuts are unavailable')
        with self.db.transaction(self.owner) as tx:
            if self.authorize:
                self.authorize(tx, actor, 'integrations.complete_action', object_id)
            row = _one(tx, "SELECT body FROM integrations.objects WHERE provider='stripe' AND id=%s FOR UPDATE", (object_id,))
            body = _json(row['body'])
            if body['status'] == 'requires_action':
                body['status'] = 'requires_capture' if success else 'declined'
            tx.execute("UPDATE integrations.objects SET body=%s::jsonb,version=version+1 WHERE provider='stripe' AND id=%s", (_canonical(body), object_id))
            return body

    def inspect_effect(self, provider, stable_key, *, actor=None):
        _client_command(stable_key)
        with self.db.transaction(self.owner) as tx:
            row = tx.execute('SELECT body FROM integrations.effects WHERE provider=%s AND effect_key=%s', (provider, stable_key)).fetchone()
            body = _json(row['body']) if row else None
            _provider_inspection(self, tx, actor, provider, 'effect', stable_key, body)
            return _provider_progress(self, tx, provider, body['id']) if body else None

    def inspect_provider_object(self, provider, object_id, *, actor=None):
        with self.db.transaction(self.owner) as tx:
            row = _one(tx, 'SELECT body FROM integrations.objects WHERE provider=%s AND id=%s', (provider, object_id))
            body = _json(row['body'])
            _provider_inspection(self, tx, actor, provider, 'object', object_id, body)
            return _provider_progress(self, tx, provider, object_id)

    def invoke(self, provider, operation, validated_request, domain_effect_key, deadline=None, *, actor=None):
        allowed = {'stripe': {'create_authorization', 'capture', 'cancel', 'refund'}, 'easypost': {'create_shipment', 'buy_label', 'cancel_label'}, 'ses': {'send'}}
        if operation not in allowed.get(provider, set()) or not isinstance(validated_request, dict):
            raise CommerceError('invalid_input', 'Provider operation is not allowlisted')
        _client_command(domain_effect_key)
        if set(validated_request) & {'url', 'endpoint', 'headers', 'pan', 'cvc', 'card_number', 'secret', 'client_secret', 'credentials'}:
            raise CommerceError('invalid_input', 'Forbidden simulator request')
        request = validated_request
        digest = hashlib.sha256(_canonical([operation, request]).encode()).hexdigest()
        with self.db.transaction(self.owner) as tx:
            _provider_authorize(self, tx, actor, provider, operation, domain_effect_key, request)
            prior = tx.execute('SELECT * FROM integrations.effects WHERE provider=%s AND effect_key=%s', (provider, domain_effect_key)).fetchone()
            if prior:
                if prior['digest'] != digest:
                    raise CommerceError('idempotency_conflict', 'Effect identity has different input')
                body = _json(prior['body'])
                return _provider_progress(self, tx, provider, body['id'])
        fault = _consume_fault(self, provider, operation, domain_effect_key)
        if fault == 'unavailable_before_send':
            raise CommerceError('not_applied', 'Simulated boundary rejected before effect admission')
        with self.db.transaction(self.owner) as tx:
            _provider_authorize(self, tx, actor, provider, operation, domain_effect_key, request)
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (provider + ':' + domain_effect_key,))
            prior = tx.execute('SELECT * FROM integrations.effects WHERE provider=%s AND effect_key=%s', (provider, domain_effect_key)).fetchone()
            if prior:
                if prior['digest'] != digest:
                    raise CommerceError('idempotency_conflict', 'Effect identity changed')
                return _provider_progress(self, tx, provider, _json(prior['body'])['id'])
            now = _now(tx)
            if deadline is not None and deadline <= now:
                raise CommerceError('not_applied', 'Original provider deadline expired before send')
            if operation == 'create_authorization':
                if set(request) - {'attempt_id', 'amount_minor', 'currency', 'capture_method', 'demo_instrument', 'order_id', 'merchant_id', 'obligation_id'}:
                    raise CommerceError('invalid_input', 'Unexpected authorization fields')
                if request.get('merchant_id', actor['merchant_id']) != actor['merchant_id'] or request.get('obligation_id', actor['obligation_id']) != actor['obligation_id']:
                    raise CommerceError('forbidden', 'Owner request binding differs from current obligation')
                amount = _integer(request.get('amount_minor'), 50)
                instrument = request.get('demo_instrument')
                if request.get('currency') != 'USD' or instrument not in ('demo_success', 'demo_decline', 'demo_pending'):
                    raise CommerceError('invalid_input', 'Selected USD demo instrument required')
                ident = 'pi_' + uuid.uuid4().hex
                pending = instrument == 'demo_pending' or fault == 'pending'
                declined = instrument == 'demo_decline' or fault == 'decline'
                body = {'id': ident, 'status': 'declined' if declined else 'processing' if pending else 'requires_capture', 'amount_minor': amount, 'currency': 'USD', 'attempt_id': request['attempt_id'], 'captured_minor': 0, 'refunded_minor': 0, 'pending_refund_minor': 0, 'demo_instrument': instrument, 'ready_at': now + 5000 if pending and (not declined) else None, 'desired_status': 'requires_capture' if pending else None, 'merchant_id': actor['merchant_id'], 'obligation_id': actor['obligation_id'], 'simulated': True}
            elif provider == 'stripe':
                ident = request.get('object_id')
                if not isinstance(ident, str):
                    raise CommerceError('invalid_input', 'Original simulator object identity required')
                body = _provider_progress(self, tx, provider, ident)
                if request.get('attempt_id') != body.get('attempt_id') or request.get('currency') != 'USD':
                    raise CommerceError('conflict', 'Simulator object binding mismatch')
                amount = _integer(request.get('amount_minor'), 1)
                pending = body.get('demo_instrument') == 'demo_pending' or fault == 'pending'
                if operation == 'capture':
                    deadline_value = request.get('capture_deadline')
                    if type(deadline_value) is not int or now >= deadline_value:
                        raise CommerceError('expired', 'Original capture hold expired before new effect')
                    if body['status'] != 'requires_capture' or amount != body['amount_minor']:
                        raise CommerceError('not_applied', 'Payment is not currently authorized for this exact amount')
                    body.update(status='processing' if pending else 'succeeded', captured_minor=0 if pending else amount, ready_at=now + 5000 if pending else None, desired_status='succeeded' if pending else None, pending_effect_key=domain_effect_key)
                    if not pending:
                        tx.execute("INSERT INTO integrations.money_ledger VALUES(%s,%s,'capture',%s,%s)", (domain_effect_key, ident, amount, now))
                elif operation == 'cancel':
                    if body['captured_minor'] or body.get('desired_status') == 'succeeded':
                        raise CommerceError('unknown_outcome', 'Capture may have been admitted and must be reconciled')
                    body.update(status='canceled', ready_at=None, desired_status=None)
                else:
                    if amount > body['captured_minor'] - body['refunded_minor'] - body.get('pending_refund_minor', 0):
                        raise CommerceError('not_applied', 'Refund exceeds successful plus pending remaining balance')
                    original = ident
                    if pending:
                        body['pending_refund_minor'] = body.get('pending_refund_minor', 0) + amount
                    else:
                        body['refunded_minor'] += amount
                    tx.execute('UPDATE integrations.objects SET body=%s::jsonb,version=version+1 WHERE provider=%s AND id=%s', (_canonical(body), provider, original))
                    ident = 're_' + uuid.uuid4().hex
                    body = {'id': ident, 'object_id': original, 'status': 'processing' if pending else 'succeeded', 'amount_minor': amount, 'currency': 'USD', 'attempt_id': request['attempt_id'], 'ready_at': now + 5000 if pending else None, 'desired_status': 'succeeded' if pending else None, 'pending_effect_key': domain_effect_key, 'merchant_id': actor['merchant_id'], 'obligation_id': actor['obligation_id'], 'simulated': True}
                    if not pending:
                        tx.execute("INSERT INTO integrations.money_ledger VALUES(%s,%s,'refund',%s,%s)", (domain_effect_key, original, amount, now))
            elif operation == 'create_shipment':
                ident = 'shp_' + uuid.uuid4().hex
                body = {'id': ident, 'status': 'created', 'shipment_id': request['shipment_id'], 'order_id': request['order_id'], 'postage': None, 'merchant_id': actor['merchant_id'], 'obligation_id': actor['obligation_id'], 'simulated': True}
            elif provider == 'easypost':
                ident = request.get('object_id')
                body = _provider_progress(self, tx, provider, ident)
                if request.get('shipment_id') != body.get('shipment_id') or request.get('order_id') != body.get('order_id'):
                    raise CommerceError('conflict', 'Parcel binding mismatch')
                if operation == 'buy_label':
                    if body.get('postage'):
                        raise CommerceError('conflict', 'This original parcel already has a simulated label')
                    body.update(status='purchased', postage={'id': 'postage_' + ident, 'tracking_code': 'SIM' + ident[-12:], 'private_artifact_ref': body['shipment_id']})
                else:
                    body['label_refund_state'] = 'succeeded'
            else:
                if not re.fullmatch('[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\\.invalid', request.get('recipient', '')):
                    raise CommerceError('invalid_input', 'Synthetic inbox recipient required')
                ident = 'message_' + uuid.uuid4().hex
                body = {'id': ident, 'status': 'accepted', 'aggregate_id': request.get('aggregate_id'), 'merchant_id': actor['merchant_id'], 'obligation_id': actor['obligation_id'], 'simulated': True}
            tx.execute('INSERT INTO integrations.objects VALUES(%s,%s,%s::jsonb,1) ON CONFLICT(provider,id) DO UPDATE SET body=excluded.body,version=integrations.objects.version+1', (provider, ident, _canonical(body)))
            tx.execute('INSERT INTO integrations.effects(provider,effect_key,digest,body,operation,request_binding) VALUES(%s,%s,%s,%s::jsonb,%s,%s::jsonb)', (provider, domain_effect_key, digest, _canonical(body), operation, _canonical(request)))
        if self.profile == 'demo-test' and self.test_checkpoint is not None:
            self.test_checkpoint(provider, operation, domain_effect_key, 'after_provider_commit')
        if fault == 'lost_reply':
            raise ProviderUnknown('Simulated response lost after independent effect commitment')
        return body

    def receive_callback(self, provider, raw_bytes, headers, *, actor=None):
        verified = self.verify_callback(provider, raw_bytes, headers)
        event_id = verified['event_id']
        body = verified['body']
        with self.db.transaction(self.owner) as tx:
            actual = _provider_progress(self, tx, provider, body['object_id'])
            _provider_inspection(self, tx, actor, provider, 'callback', event_id, actual)
            if any((body[key] != actual.get(key) for key in ('attempt_id', 'amount_minor', 'currency'))):
                raise CommerceError('conflict', 'Callback object binding mismatch')
            prior = tx.execute('SELECT * FROM integrations.callback_inbox WHERE provider=%s AND event_id=%s FOR UPDATE', (provider, event_id)).fetchone()
            if prior:
                if prior['digest'] != verified['digest']:
                    raise CommerceError('conflict', 'Callback identity changed')
                return _json(prior['result'])
            result = {'id': event_id, 'state': 'recorded', 'version': 1, 'object': actual}
            tx.execute('INSERT INTO integrations.callback_inbox VALUES(%s,%s,%s,%s,%s,%s::jsonb)', (provider, event_id, actual['id'], verified['digest'], _now(tx), _canonical(result)))
            return result

    def verify_callback(self, provider, raw_bytes, headers):
        if self.callback_secret is None or not isinstance(raw_bytes, bytes) or len(raw_bytes) > 262144:
            raise CommerceError('forbidden', 'Callback authentication unavailable')
        timestamp = headers.get('X-Simulator-Timestamp')
        event_id = headers.get('X-Simulator-Event-Id')
        signature = headers.get('X-Simulator-Signature')
        _client_command(event_id)
        try:
            stamp = int(timestamp)
        except (ValueError, TypeError):
            raise CommerceError('forbidden', 'Invalid callback authentication')
        with self.db.transaction(self.owner) as tx:
            now = _now(tx)
        if abs(now - stamp) > 300000:
            raise CommerceError('forbidden', 'Callback expired')
        message = str(stamp).encode() + b'.' + event_id.encode() + b'.' + raw_bytes
        expected = hmac.new(self.callback_secret(), message, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not secrets.compare_digest(expected, signature):
            raise CommerceError('forbidden', 'Invalid callback authentication')
        try:
            body = json.loads(raw_bytes.decode('utf-8'), object_pairs_hook=_unique_pairs)
        except Exception:
            raise CommerceError('invalid_input', 'Malformed callback')
        if not isinstance(body, dict) or set(body) != {'object_id', 'attempt_id', 'amount_minor', 'currency', 'status'}:
            raise CommerceError('invalid_input', 'Closed simulator callback required')
        _integer(body.get('amount_minor'))
        if body.get('currency') != 'USD' or not isinstance(body.get('object_id'), str) or (not isinstance(body.get('attempt_id'), str)):
            raise CommerceError('invalid_input', 'Typed simulator callback binding required')
        return {'provider': provider, 'event_id': event_id, 'digest': hashlib.sha256(raw_bytes).hexdigest(), 'body': body}

class ProviderUnknown(TimeoutError):
    effect_certainty = 'unknown'

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

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

def _consume_fault(service, provider, operation, key):
    if service.profile != 'demo-test':
        return None
    with service.db.transaction(service.owner) as tx:
        _provider_snapshot_shared(tx)
        tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('fault:' + provider + ':' + key,))
        if tx.execute('SELECT 1 FROM integrations.fault_consumptions WHERE provider=%s AND effect_key=%s', (provider, key)).fetchone():
            return None
        plan = tx.execute('SELECT * FROM integrations.fault_plans WHERE provider=%s AND operation=%s AND effect_key=%s AND remaining>0 ORDER BY id LIMIT 1 FOR UPDATE', (provider, operation, key)).fetchone()
        if plan:
            tx.execute('UPDATE integrations.fault_plans SET remaining=remaining-1 WHERE id=%s', (plan['id'],))
        tx.execute('INSERT INTO integrations.fault_consumptions VALUES(%s,%s,%s,%s)', (provider, key, plan['id'] if plan else None, plan['fault'] if plan else None))
        return plan['fault'] if plan else None

def _integer(value, minimum=0, maximum=99999999):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CommerceError('invalid_input', 'Expected a bounded integer')
    return value

def _json(value):
    """psycopg already decodes jsonb, including JSON string scalars."""
    return value

def _now(tx):
    return tx.execute('SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS value').fetchone()['value']

def _one(tx, query, params=()):
    row = tx.execute(query, params).fetchone()
    if row is None:
        raise CommerceError('not_found', 'Resource unavailable')
    return dict(row)

def _provider_authorize(service, tx, actor, provider, operation, key, request):
    _provider_snapshot_shared(tx)

    def admit(current):
        if not isinstance(actor, dict) or actor.get('kind') != 'recovery' or (not actor.get('obligation_id')) or (not actor.get('merchant_id')) or (service.dispatch_guard is None) or (service.dispatch_guard(current, actor, provider, operation, key, request) is not True):
            raise CommerceError('forbidden', 'Current scoped owner dispatch permit required')
        return True
    admit(tx)
    tx.add_precommit(admit)

def _provider_inspection(service, tx, actor, provider, kind, identity, body):
    _provider_snapshot_shared(tx)

    def admit(current):
        if not isinstance(actor, dict) or service.inspection_guard is None or service.inspection_guard(current, actor, provider, kind, identity, body) is not True:
            raise CommerceError('not_found', 'Simulator resource unavailable')
        return True
    admit(tx)
    tx.add_precommit(admit)

def _provider_progress(service, tx, provider, object_id):
    _provider_snapshot_shared(tx)
    row = _one(tx, 'SELECT * FROM integrations.objects WHERE provider=%s AND id=%s FOR UPDATE', (provider, object_id))
    body = _json(row['body'])
    if body.get('ready_at') is not None and _now(tx) >= body['ready_at']:
        target = body.get('desired_status')
        body.update(status=target, ready_at=None, desired_status=None)
        if target == 'succeeded' and body.get('object_id'):
            original = _one(tx, 'SELECT body FROM integrations.objects WHERE provider=%s AND id=%s FOR UPDATE', (provider, body['object_id']))
            payment = _json(original['body'])
            payment['pending_refund_minor'] -= body['amount_minor']
            payment['refunded_minor'] += body['amount_minor']
            tx.execute('UPDATE integrations.objects SET body=%s::jsonb,version=version+1 WHERE provider=%s AND id=%s', (_canonical(payment), provider, body['object_id']))
            tx.execute("INSERT INTO integrations.money_ledger VALUES(%s,%s,'refund',%s,%s) ON CONFLICT DO NOTHING", (body['pending_effect_key'], body['object_id'], body['amount_minor'], _now(tx)))
        elif target == 'succeeded':
            body['captured_minor'] = body['amount_minor']
            tx.execute("INSERT INTO integrations.money_ledger VALUES(%s,%s,'capture',%s,%s) ON CONFLICT DO NOTHING", (body['pending_effect_key'], body['id'], body['amount_minor'], _now(tx)))
        tx.execute('UPDATE integrations.objects SET body=%s::jsonb,version=version+1 WHERE provider=%s AND id=%s', (_canonical(body), provider, object_id))
    return body

def _provider_snapshot_shared(tx):
    tx.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', ('sme.integrations.survivor.snapshot.v1',))

def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON member')
        result[key] = value
    return result
