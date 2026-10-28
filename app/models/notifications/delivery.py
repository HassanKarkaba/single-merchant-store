from __future__ import annotations
import hashlib
import json
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class NotificationDelivery:

    def dispatch(self, intent_id, *, actor=None):
        if self.provider is None:
            raise CommerceError('dependency_unavailable', 'Durable deterministic simulator required')
        key = str(uuid.uuid5(uuid.UUID(intent_id), 'synthetic-delivery'))
        now_receipt = _read(self, actor, 'get_delivery_status', intent_id, lambda tx: _one(tx, 'SELECT * FROM notifications.intents WHERE id=%s', (intent_id,)))
        if now_receipt['state'] == 'simulated_delivered':
            return {'id': intent_id, 'state': 'simulated_delivered', 'version': now_receipt['version']}

        def claim(tx):
            row = _one(tx, 'SELECT * FROM notifications.intents WHERE id=%s FOR UPDATE', (intent_id,))
            now = _now(tx)
            if row['state'] == 'sending' and row['lease_until'] and (row['lease_until'] > now):
                return {'id': intent_id, 'state': 'pending', 'version': row['version']}
            token = str(uuid.uuid4())
            tx.execute("UPDATE notifications.intents SET state='sending',attempts=attempts+1,lease_token=%s,lease_until=%s,version=version+1 WHERE id=%s", (token, now + 30000, intent_id))
            return {'id': intent_id, 'state': 'sending', 'version': row['version'] + 1, 'token': token}
        claim_result = _run(self, actor, 'dispatch_claim', intent_id, [now_receipt['version']], str(uuid.uuid5(uuid.UUID(key), 'claim:' + str(now_receipt['version']))), claim)
        if claim_result['state'] == 'pending':
            return claim_result
        try:
            provider = self.provider.inspect_effect('ses', key, actor=actor)
            if provider is None:
                provider = self.provider.invoke('ses', 'send', {'recipient': now_receipt['recipient'], 'kind': now_receipt['kind'], 'variables': now_receipt['variables'], 'aggregate_id': now_receipt['aggregate_id']}, key, actor=actor)
        except Exception:

            def unknown(tx):
                row = _one(tx, 'SELECT * FROM notifications.intents WHERE id=%s FOR UPDATE', (intent_id,))
                next_at = _now(tx) + 1000
                tx.execute("UPDATE notifications.intents SET state='unknown',next_at=%s,lease_until=NULL,version=version+1 WHERE id=%s", (next_at, intent_id))
                return {'id': intent_id, 'state': 'unknown', 'version': row['version'] + 1}
            return _run(self, actor, 'dispatch_unknown', intent_id, [key, claim_result['token']], str(uuid.uuid5(uuid.UUID(key), 'unknown:' + claim_result['token'])), unknown)

        def settle(tx):
            row = _one(tx, 'SELECT * FROM notifications.intents WHERE id=%s FOR UPDATE', (intent_id,))
            content = {'kind': row['kind'], 'template_version': row['template_version'], 'variables': row['variables'], 'disclosure': 'Synthetic demonstration inbox; no external email sent'}
            tx.execute('INSERT INTO notifications.synthetic_inbox VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(intent_id) DO NOTHING', (str(uuid.uuid5(uuid.UUID(intent_id), 'inbox')), intent_id, row['owner_id'], row['kind'], row['aggregate_id'], _canonical(content), _now(tx)))
            return dict(_one(tx, "UPDATE notifications.intents SET state='simulated_delivered',lease_until=NULL,version=version+1 WHERE id=%s RETURNING id,state,version", (intent_id,)))
        return _run(self, actor, 'dispatch_settle', intent_id, [provider['id']], str(uuid.uuid5(uuid.UUID(key), 'settle')), settle)

    def pending(self, limit=100, *, actor=None):

        def work(tx):
            return {'items': [dict(r) for r in tx.execute("SELECT id,aggregate_id,workflow_id,state,next_at FROM notifications.intents WHERE state IN ('queued','retry','unknown','sending') AND next_at<=%s ORDER BY next_at,id LIMIT %s", (_now(tx), _integer(limit, 1, 100))).fetchall()]}
        return _read(self, actor, 'pending', 'notifications', work)

    def receive_provider_event(self, event_id, event, *, authenticated=False, actor=None, command_id=None):
        if authenticated is not True:
            raise CommerceError('forbidden', 'Authenticated provider feedback required')

        def work(tx):
            tx.execute('INSERT INTO notifications.provider_events VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING', (event_id, _canonical(event)))
            if event.get('kind') in ('bounce', 'complaint'):
                tx.execute('INSERT INTO notifications.suppressions VALUES(%s,%s) ON CONFLICT DO NOTHING', (event['recipient'], event['kind']))
            return {'id': event_id, 'state': 'recorded', 'version': 1}
        return _run(self, actor, 'receive_provider_event', event_id, event, command_id or event_id, work)

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

def _command_scope(service, actor, operation, resource, command_id):
    if not isinstance(actor, dict) or not actor.get('id'):
        raise CommerceError('unauthenticated', 'Current principal is required')
    scope = {'v': 2, 'merchant': actor.get('merchant_id', 'portfolio-demo'), 'principal': actor['id'], 'kind': 'recovery' if actor.get('kind') == 'recovery' else 'human', 'obligation': actor.get('obligation_id') if actor.get('kind') == 'recovery' else None, 'owner': service.owner, 'operation': operation, 'resource': resource, 'command_id': command_id}
    return str(uuid.uuid5(uuid.NAMESPACE_URL, _canonical(scope)))

def _integer(value, minimum=0, maximum=99999999):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CommerceError('invalid_input', 'Expected a bounded integer')
    return value

def _now(tx):
    return tx.execute('SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS value').fetchone()['value']

def _one(tx, query, params=()):
    row = tx.execute(query, params).fetchone()
    if row is None:
        raise CommerceError('not_found', 'Resource unavailable')
    return dict(row)

def _ownership(row, actor):
    if not isinstance(actor, dict):
        raise CommerceError('unauthenticated', 'Current principal is required')
    if actor.get('kind') == 'recovery':
        return
    if actor.get('kind') not in ('staff',) and row.get('owner_id') != actor.get('id'):
        raise CommerceError('not_found', 'Resource unavailable')

def _preflight_resource(service, tx, actor, operation, resource, write=False):
    if not isinstance(actor, dict) or not actor.get('id'):
        raise CommerceError('unauthenticated', 'Current principal required')
    if actor.get('kind') == 'service':
        raise CommerceError('forbidden', 'Unscoped service actors are not authority')
    if actor.get('kind') == 'recovery':
        if not actor.get('obligation_id') or service.resource_predicate is None or service.resource_predicate(tx, actor, service.owner + '.' + operation, resource) is not True:
            raise CommerceError('forbidden', 'A current admitted resource-bound obligation is required')
        return
    tables = {'cart': ('cart.carts', 'id'), 'orders': ('orders.orders', 'id'), 'returns': ('returns.requests', 'id'), 'shipping': ('shipping.shipments', 'id'), 'pricing': ('pricing.quotes', 'id'), 'tax': ('tax.calculations', 'id'), 'inventory': ('inventory.reservations', 'id'), 'notifications': ('notifications.intents', 'id')}
    if service.owner in tables and resource:
        table, column = tables[service.owner]
        row = tx.execute('SELECT owner_id FROM ' + table + ' WHERE ' + column + '=%s' + (' FOR UPDATE' if write else ' FOR SHARE'), (str(resource),)).fetchone()
        if row is None and service.owner == 'orders':
            row = tx.execute('SELECT owner_id FROM orders.refund_plans WHERE id=%s' + (' FOR UPDATE' if write else ' FOR SHARE'), (str(resource),)).fetchone()
        if row is None and service.owner == 'shipping':
            row = tx.execute('SELECT owner_id FROM shipping.rate_quotes WHERE id=%s' + (' FOR UPDATE' if write else ' FOR SHARE'), (str(resource),)).fetchone()
        if row is None and service.owner == 'inventory':
            row = tx.execute('SELECT owner_id FROM inventory.reservations WHERE checkout_id=%s UNION ALL SELECT owner_id FROM inventory.allocations WHERE id=%s OR order_id=%s', (str(resource), str(resource), str(resource))).fetchone()
        required = {'cart': {'get_cart', 'set_line', 'remove_line', 'set_coupon', 'clear_purchased_lines', 'capture_guest_handoff', 'merge_guest_cart', 'expire_cart'}, 'orders': {'confirm_payment', 'mark_payment_failed', 'record_fulfillment', 'record_delivery', 'record_return', 'record_refund', 'plan_units', 'get_order', 'get_receipt', 'get_refund_plan', 'settle_zero_plan'}, 'returns': {'get_return', 'get_receipt', 'approve_return', 'record_received', 'record_inspection', 'build_refund_plan', 'create_exchange_link', 'bind_refund_plan', 'close_return'}, 'notifications': {'get_delivery_status'}}
        if row is None and operation in required.get(service.owner, set()):
            raise CommerceError('not_found', 'Resource unavailable')
        if row:
            if service.owner == 'cart' and operation == 'merge_guest_cart':
                handoff = tx.execute('SELECT 1 FROM cart.handoffs WHERE guest_id=%s AND destination_principal=%s', (resource, actor['id'])).fetchone()
                if not handoff:
                    raise CommerceError('not_found', 'Handoff unavailable')
            else:
                _ownership(row, actor)
    if service.resource_predicate is not None and service.resource_predicate(tx, actor, service.owner + '.' + operation, resource) is not True:
        raise CommerceError('not_found', 'Resource unavailable')

def _read(service, actor, operation, resource, fn, public=False):

    def guard(tx):
        if public and actor is None:
            return True
        if service.authorize is None:
            raise CommerceError('forbidden', 'Current transaction authority is required')
        if service.authorize(tx, actor, service.owner + '.' + operation, resource) is not True:
            raise CommerceError('forbidden', 'Authority denied')
        _preflight_resource(service, tx, actor, operation, resource)
        return True
    with service.db.transaction(service.owner, authority_guard=guard) as tx:
        return fn(tx)

def _run(service, actor, operation, resource, payload, command_id, fn):
    client = _client_command(command_id)
    key = _command_scope(service, actor, operation, resource, client)
    digest = hashlib.sha256(_canonical({'identity_version': 2, 'operation': operation, 'resource': resource, 'input': payload, 'policy': 'portfolio-policy-1'}).encode()).hexdigest()

    def guard(tx):
        if service.authorize is None or service.authorize(tx, actor, service.owner + '.' + operation, resource) is not True:
            raise CommerceError('forbidden', 'Current transaction authority is required')
        _preflight_resource(service, tx, actor, operation, resource, True)
        legacy = tx.execute('SELECT state FROM commerce_commands WHERE owner=%s AND command_id=%s', (service.owner, client)).fetchone()
        if legacy is not None and client != key:
            raise CommerceError('legacy_replay_quarantined', 'Legacy receipt needs owning migration; it cannot become a new effect')
        return True
    try:
        with service.db.transaction(service.owner, command_id=key, request_digest=digest, authority_guard=guard) as tx:
            prior = tx.command_lookup()
            if prior is not None:
                return prior
            result = fn(tx)
            if service.owner == 'events' and operation in ('deliver', 'ack', 'defer', 'claim', 'replay'):
                result.setdefault('id', str(resource or key))
                result.setdefault('version', 1)
                result.setdefault('state', 'recorded')
                tx.command_complete(result)
                return result
            result.setdefault('owner', service.owner)
            result.setdefault('id', str(resource or key))
            result.setdefault('version', 1)
            result.setdefault('state', 'recorded')
            result.setdefault('effect_certainty', 'committed')
            result.setdefault('receipt_id', key)
            result.setdefault('command_id', client)
            result.setdefault('next_action', None)
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, service.owner + ':' + key))
            result['event_id'] = event_id
            tx.command_complete(result)
            fact = {'event_id': event_id, 'producer': service.owner, 'aggregate_type': service.owner, 'aggregate_id': str(result['id'] if operation in ('create_product', 'create_provisional', 'create_guest_cart', 'create_shipment', 'quote', 'calculate') else resource or result['id']), 'aggregate_version': result['version'], 'occurred_at': _now(tx), 'schema_version': 1, 'correlation_id': client, 'causation_id': None, 'workflow_id': actor.get('obligation_id'), 'policy_revision': 'portfolio-policy-1', 'release_id': getattr(service, 'release_id', 'portfolio-construction'), 'payload': {'operation': operation, 'receipt_id': key, 'state': result['state']}}
            tx.stage_outbox(operation, event_id, fact)
            tx.stage_audit(event_id, {'owner': service.owner, 'operation': operation, 'receipt_id': key, 'resource': str(result['id'] if operation in ('create_product', 'create_provisional', 'create_guest_cart', 'create_shipment', 'quote', 'calculate') else resource or result['id']), 'principal_id': actor['id'], 'occurred_at': fact['occurred_at']})
            return result
    except Exception as error:
        state = getattr(error, 'sqlstate', None)
        if state == '23505':
            raise CommerceError('conflict', 'A unique value already exists; reload or choose another') from None
        if state in ('23503', '23514'):
            raise CommerceError('ineligible', 'Resource changed or violates its invariant') from None
        if state in ('40001', '40P01', '55P03'):
            raise CommerceError('resource_busy', 'A concurrent operation must settle; retry the same identity') from None
        raise
