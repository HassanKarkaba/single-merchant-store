from __future__ import annotations
import hashlib
import json
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class Reservations:

    def allocate(self, reservation_id, order_id, *, actor=None, command_id=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Provisional order owner port required')
        order = self.order_port(order_id, actor=actor)

        def work(tx):
            row = _one(tx, 'SELECT * FROM inventory.reservations WHERE id=%s FOR UPDATE', (reservation_id,))
            if order['checkout_id'] != row['checkout_id']:
                raise CommerceError('ineligible', 'Reservation and durable order checkout mismatch')
            old = tx.execute('SELECT * FROM inventory.allocations WHERE reservation_id=%s', (reservation_id,)).fetchone()
            if old:
                if old['order_id'] != order_id:
                    raise CommerceError('conflict', 'Reservation belongs to another order')
                return dict(old)
            if row['state'] != 'reserved' or row['expires_at'] <= _now(tx):
                raise CommerceError('expired', 'Reservation is no longer allocatable')
            quantities = _json(row['lines'])
            _lock_stock(tx, quantities)
            for key, value in quantities.items():
                tx.execute('UPDATE inventory.stock_items SET reserved=reserved-%s,allocated=allocated+%s,version=version+1 WHERE variant_id=%s', (value, value, key))
            ident = str(uuid.uuid4())
            tx.execute("INSERT INTO inventory.allocations(id,order_id,reservation_id,lines,remaining,state,version,owner_id) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,'allocated',1,%s)", (ident, order_id, reservation_id, _canonical(quantities), _canonical(quantities), row['owner_id']))
            tx.execute("UPDATE inventory.reservations SET state='allocated',version=version+1 WHERE id=%s", (reservation_id,))
            return {'id': ident, 'order_id': order_id, 'reservation_id': reservation_id, 'lines': quantities, 'remaining': quantities, 'expires_at': row['expires_at'], 'state': 'allocated', 'version': 1}
        return _run(self, actor, 'allocate', reservation_id, [order_id], command_id, work)

    def expire_reservations(self, limit=100, *, actor=None):
        ids = _read(self, actor, 'expire_reservations', None, lambda tx: [r['id'] for r in tx.execute("SELECT id FROM inventory.reservations WHERE state='reserved' AND expires_at<=%s ORDER BY expires_at,id LIMIT %s", (_now(tx), _integer(limit, 1, 100))).fetchall()])
        return [self.release_reservation(i, actor=actor, command_id='expiry:' + i) for i in ids]

    def inspect_allocation(self, allocation_id=None, *, order_id=None, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT a.*,r.expires_at FROM inventory.allocations a JOIN inventory.reservations r ON r.id=a.reservation_id WHERE ' + ('a.order_id' if order_id else 'a.id') + '=%s', (order_id or allocation_id,))
            _ownership(row, actor)
            return row
        return _read(self, actor, 'inspect_allocation', order_id or allocation_id, work)

    def inspect_checkout_reservation(self, checkout_id, *, actor=None):

        def work(tx):
            row = tx.execute('SELECT * FROM inventory.reservations WHERE checkout_id=%s', (checkout_id,)).fetchone()
            if row:
                _ownership(row, actor)
            return dict(row) if row else None
        return _read(self, actor, 'inspect_checkout_reservation', checkout_id, work)

    def inspect_receipt(self, evidence, *, actor=None):
        if evidence.get('kind') == 'shipment':

            def work(tx):
                row = _one(tx, 'SELECT s.id,s.allocation_id,s.quantities,a.order_id,a.owner_id FROM inventory.shipments s JOIN inventory.allocations a ON a.id=s.allocation_id WHERE s.id=%s', (evidence['id'],))
                _ownership(row, actor)
                return {**row, 'state': 'shipped', 'version': 1}
            return _read(self, actor, 'inspect_receipt', evidence['id'], work)
        return self.inspect_allocation(evidence.get('id'), order_id=evidence.get('order_id'), actor=actor)

    def inspect_reservation(self, reservation_id, *, actor=None):
        return _read(self, actor, 'inspect_reservation', reservation_id, lambda tx: _one(tx, 'SELECT * FROM inventory.reservations WHERE id=%s', (reservation_id,)))

    def release_allocation(self, order_id, quantities, reason, evidence=None, *, actor=None, command_id=None):
        quantities = _quantities(quantities)
        if not isinstance(evidence, dict):
            raise CommerceError('ineligible', 'Actual no-capture or cancellation-plan evidence required')
        owner = 'payments' if evidence.get('owner') == 'payments' else 'orders'
        actual = _receipt(self, owner, evidence, actor)
        if owner == 'payments':
            if str(actual.get('state')).upper() not in ('CANCELED', 'DECLINED', 'FAILED', 'NOT_CREATED') or actual.get('captured_minor', 0) or (not actual.get('capture_fenced')):
                raise CommerceError('unknown_outcome', 'Capture is not definitively fenced')
        elif actual.get('kind') != 'cancellation' or actual.get('state') not in ('approved', 'succeeded') or actual.get('order_id') != order_id:
            raise CommerceError('ineligible', 'Approved canceled-unit obligation required')
        if owner == 'orders':
            expected = {}
            for unit in actual['unit_allocations']:
                expected[unit['variant_id']] = expected.get(unit['variant_id'], 0) + 1
            if expected != quantities:
                raise CommerceError('ineligible', 'Released stock must equal exact canceled-unit plan')

        def work(tx):
            row = _one(tx, 'SELECT * FROM inventory.allocations WHERE order_id=%s FOR UPDATE', (order_id,))
            remaining = _json(row['remaining'])
            _lock_stock(tx, quantities)
            if any((value > remaining.get(key, 0) for key, value in quantities.items())):
                raise CommerceError('ineligible', 'Release exceeds current allocation')
            for key, value in quantities.items():
                remaining[key] -= value
                tx.execute('UPDATE inventory.stock_items SET allocated=allocated-%s,version=version+1 WHERE variant_id=%s', (value, key))
            state = 'released' if not sum(remaining.values()) else 'allocated'
            return dict(_one(tx, 'UPDATE inventory.allocations SET remaining=%s::jsonb,state=%s,version=version+1 WHERE id=%s RETURNING id,order_id,remaining,state,version', (_canonical(remaining), state, row['id'])))
        return _run(self, actor, 'release_allocation', order_id, [quantities, reason, actual.get('id')], command_id, work)

    def release_reservation(self, reservation_id, expected_revision=None, *, actor=None, command_id=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM inventory.reservations WHERE id=%s FOR UPDATE', (reservation_id,))
            _version(row, expected_revision)
            if row['state'] != 'reserved':
                return {'id': reservation_id, 'state': row['state'], 'version': row['version']}
            quantities = _json(row['lines'])
            _lock_stock(tx, quantities)
            for key, value in quantities.items():
                tx.execute('UPDATE inventory.stock_items SET reserved=reserved-%s,version=version+1 WHERE variant_id=%s', (value, key))
            return dict(_one(tx, "UPDATE inventory.reservations SET state='released',version=version+1 WHERE id=%s RETURNING id,state,version", (reservation_id,)))
        return _run(self, actor, 'release_reservation', reservation_id, [expected_revision], command_id, work)

    def reserve(self, checkout_id, lines, expires_at=None, *, actor=None, command_id=None):
        quantities = _quantities(lines)
        if sum(quantities.values()) > 200:
            raise CommerceError('limit_exceeded', 'At most two hundred units')
        if self.checkout_port is None:
            raise CommerceError('dependency_unavailable', 'Checkout owner port required')
        existing = self.inspect_checkout_reservation(checkout_id, actor=actor)
        checkout = {'owner_id': existing['owner_id']} if existing else self.checkout_port(checkout_id, actor=actor)

        def work(tx):
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('inventory-checkout:' + checkout_id,))
            old = tx.execute('SELECT * FROM inventory.reservations WHERE checkout_id=%s FOR UPDATE', (checkout_id,)).fetchone()
            if old:
                if _json(old['lines']) != quantities:
                    raise CommerceError('idempotency_conflict', 'Checkout reservation lines changed')
                return dict(old)
            rows = _lock_stock(tx, quantities)
            if any((rows[k]['on_hand'] - rows[k]['reserved'] - rows[k]['allocated'] < v for k, v in quantities.items())):
                raise CommerceError('unavailable', 'A requested item is unavailable')
            now = _now(tx)
            end = now + 900000
            if expires_at is not None and expires_at != end:
                raise CommerceError('invalid_input', 'Reservation duration is fixed and cannot be renewed')
            if end <= now:
                raise CommerceError('expired', 'Reservation deadline expired')
            ident = str(uuid.uuid4())
            for key, value in quantities.items():
                tx.execute('UPDATE inventory.stock_items SET reserved=reserved+%s,version=version+1 WHERE variant_id=%s', (value, key))
            tx.execute("INSERT INTO inventory.reservations(id,checkout_id,lines,expires_at,state,version,owner_id) VALUES(%s,%s,%s::jsonb,%s,'reserved',1,%s)", (ident, checkout_id, _canonical(quantities), end, checkout['owner_id']))
            return {'id': ident, 'checkout_id': checkout_id, 'lines': quantities, 'expires_at': end, 'state': 'reserved', 'version': 1}
        return _run(self, actor, 'reserve', checkout_id, [quantities, expires_at], command_id, work)

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

def _lock_stock(tx, quantities):
    rows = {row['variant_id']: row for row in tx.execute('SELECT * FROM inventory.stock_items WHERE variant_id=ANY(%s) ORDER BY variant_id FOR UPDATE', (sorted(quantities),)).fetchall()}
    if len(rows) != len(quantities):
        raise CommerceError('unavailable', 'Stock record unavailable')
    return rows

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

def _quantities(lines):
    if isinstance(lines, dict):
        values = lines
    elif isinstance(lines, list):
        values = {}
        for line in lines:
            if line.get('variant_id') in values:
                raise CommerceError('invalid_input', 'Duplicate variant')
            values[line.get('variant_id')] = line.get('quantity')
    else:
        raise CommerceError('invalid_input', 'Quantities required')
    if not 1 <= len(values) <= 100 or any((not isinstance(k, str) or not k for k in values)):
        raise CommerceError('invalid_input', 'One to 100 valid variants required')
    return {k: _integer(v, 1, 99) for k, v in sorted(values.items())}

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

def _receipt(service, owner, evidence, actor):
    inspector = service.receipt_inspectors.get(owner)
    if inspector is None:
        raise CommerceError('dependency_unavailable', 'Supported ' + owner + ' receipt inspector is not bound')
    actual = inspector(evidence, actor=actor)
    if not isinstance(actual, dict) or not actual.get('id'):
        raise CommerceError('invalid_dependency', 'Invalid owner receipt')
    return actual

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

def _version(row, expected):
    if expected is not None and row['version'] != _integer(expected, 1):
        raise CommerceError('version_conflict', 'Resource changed; reload before editing')
