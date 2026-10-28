from __future__ import annotations
import hashlib
import json
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class EventSubscriptions:

    def ack_consumption(self, event_id, consumer_id, result, fence=None, *, actor=None, command_id=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM events.deliveries WHERE event_id=%s AND consumer_id=%s FOR UPDATE', (event_id, consumer_id))
            if row['state'] == 'done':
                return {'id': event_id, 'state': 'done', 'version': 1}
            if fence is None or row['fence'] != fence or row['state'] != 'claimed' or (row['lease_until'] <= _now(tx)):
                raise CommerceError('conflict', 'Current delivery fence required')
            event = _json(row['envelope'])
            offset = tx.execute('SELECT * FROM events.offsets WHERE consumer_id=%s AND aggregate_id=%s FOR UPDATE', (consumer_id, event['aggregate_id'])).fetchone()
            current = offset['version'] if offset else 0
            if event['aggregate_version'] > current + 1:
                raise CommerceError('generation_gap', 'Earlier aggregate event is missing')
            tx.execute('INSERT INTO events.offsets VALUES(%s,%s,%s) ON CONFLICT(consumer_id,aggregate_id) DO UPDATE SET version=greatest(events.offsets.version,excluded.version)', (consumer_id, event['aggregate_id'], event['aggregate_version']))
            tx.execute("UPDATE events.deliveries SET state='done',result=%s::jsonb WHERE event_id=%s AND consumer_id=%s", (_canonical(result), event_id, consumer_id))
            return {'id': event_id, 'state': 'done', 'version': 1}
        return _run(self, actor, 'ack', event_id, [consumer_id, result, fence], command_id, work)

    def bind_consumer(self, consumer_id, port, projection='ordered'):
        if not callable(port) or projection not in ('ordered', 'authoritative_owner_reread'):
            raise CommerceError('invalid_input', 'Supported consumer port and declared projection rule required')
        self.consumer_ports[consumer_id] = (port, projection)

    def claim(self, consumer_id, batch=100, lease=30, *, actor=None):
        _integer(batch, 1, 100)
        _integer(lease, 1, 30)

        def work(tx):
            now = _now(tx)
            rows = tx.execute("SELECT * FROM events.deliveries WHERE consumer_id=%s AND (state='ready' OR (state='claimed' AND lease_until<=%s)) AND next_at<=%s ORDER BY next_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED", (consumer_id, now, now, batch)).fetchall()
            result = []
            for row in rows:
                if row['attempts'] >= 8:
                    tx.execute("UPDATE events.deliveries SET state='dead' WHERE event_id=%s AND consumer_id=%s", (row['event_id'], consumer_id))
                    continue
                fence = row['fence'] + 1
                tx.execute("UPDATE events.deliveries SET state='claimed',fence=%s,lease_until=%s,attempts=attempts+1 WHERE event_id=%s AND consumer_id=%s", (fence, now + lease * 1000, row['event_id'], consumer_id))
                result.append({'event': row['envelope'], 'fence': fence, 'lease_until': now + lease * 1000})
            return {'id': str(uuid.uuid4()), 'version': 1, 'state': 'claimed', 'items': result}
        return _run(self, actor, 'claim', consumer_id, [batch, lease], None, work)['items']

    def defer_consumption(self, event_id, consumer_id, fence, reason, delay_ms=1000, *, actor=None, command_id=None):
        _integer(delay_ms, 1, 3600000)

        def work(tx):
            row = _one(tx, 'SELECT * FROM events.deliveries WHERE event_id=%s AND consumer_id=%s FOR UPDATE', (event_id, consumer_id))
            if row['fence'] != fence:
                raise CommerceError('conflict', 'Stale delivery fence')
            state = 'dead' if row['attempts'] >= 8 else 'ready'
            tx.execute('UPDATE events.deliveries SET state=%s,next_at=%s WHERE event_id=%s AND consumer_id=%s', (state, _now(tx) + delay_ms, event_id, consumer_id))
            return {'id': event_id, 'version': 1, 'state': state}
        return _run(self, actor, 'defer', event_id, [consumer_id, fence, reason, delay_ms], command_id, work)

    def dispatch_consumption(self, consumer_id, *, actor=None, batch=100):
        if consumer_id not in self.consumer_ports:
            raise CommerceError('dependency_unavailable', 'Consumer port is not bound')
        port, projection = self.consumer_ports[consumer_id]
        results = []
        for claim in self.claim(consumer_id, batch, actor=actor):
            event = claim['event']
            effective = self.actor_factory(event) if self.actor_factory else actor
            try:
                result = port(event, actor=effective)
                results.append(self.ack_consumption(event['event_id'], consumer_id, result, claim['fence'], actor=effective))
            except Exception:
                self.defer_consumption(event['event_id'], consumer_id, claim['fence'], 'consumer_failed', actor=effective)
        return results

    def list_dead_letters(self, *, actor=None):
        return _read(self, actor, 'list_dead_letters', None, lambda tx: {'items': [dict(r) for r in tx.execute("SELECT * FROM events.deliveries WHERE state IN ('dead','quarantined') ORDER BY event_id LIMIT 100").fetchall()]})

    def pending(self, limit=100, *, actor=None):
        return _read(self, actor, 'pending', 'events', lambda tx: {'items': [{'event_id': r['event_id'], 'consumer_id': r['consumer_id'], 'workflow_id': _json(r['envelope']).get('workflow_id')} for r in tx.execute("SELECT * FROM events.deliveries WHERE state IN ('ready','claimed') ORDER BY next_at,event_id LIMIT %s", (_integer(limit, 1, 100),)).fetchall()]})

    def replay_dead_letter(self, event_id, consumer_id, reason, *, actor=None, command_id=None):
        if not isinstance(reason, str) or not reason.strip():
            raise CommerceError('invalid_input', 'Replay reason required')

        def work(tx):
            row = _one(tx, 'SELECT * FROM events.deliveries WHERE event_id=%s AND consumer_id=%s FOR UPDATE', (event_id, consumer_id))
            if _json(row['envelope'])['schema_version'] not in (1, 2):
                raise CommerceError('not_supported', 'Schema remains unsupported')
            tx.execute("UPDATE events.deliveries SET state='ready',attempts=0,next_at=%s,fence=fence+1 WHERE event_id=%s AND consumer_id=%s", (_now(tx), event_id, consumer_id))
            return {'id': event_id, 'state': 'ready', 'version': 1}
        return _run(self, actor, 'replay', event_id, [consumer_id, reason], command_id, work)

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
