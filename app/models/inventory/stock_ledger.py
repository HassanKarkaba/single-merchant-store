from __future__ import annotations
import base64
import hashlib
import json
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class StockLedger:

    def adjust_stock(self, variant_id, delta, reason, expected_revision, *, actor=None, command_id=None):
        _integer(delta, -99999999, 99999999)
        _integer(expected_revision, 0)
        if delta == 0 or not isinstance(reason, str) or (not 1 <= len(reason.strip()) <= 300):
            raise CommerceError('invalid_input', 'Nonzero adjustment and bounded reason required')
        if self.variant_port is None or not self.variant_port(variant_id, actor=actor):
            raise CommerceError('not_found', 'An existing variant is required')

        def work(tx):
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('stock:' + variant_id,))
            row = tx.execute('SELECT * FROM inventory.stock_items WHERE variant_id=%s FOR UPDATE', (variant_id,)).fetchone()
            if row:
                _version(row, expected_revision)
            else:
                if expected_revision != 0:
                    raise CommerceError('version_conflict', 'New stock requires revision zero')
                tx.execute('INSERT INTO inventory.stock_items(variant_id,on_hand,version) VALUES(%s,0,1)', (variant_id,))
                row = _one(tx, 'SELECT * FROM inventory.stock_items WHERE variant_id=%s FOR UPDATE', (variant_id,))
            if row['on_hand'] + delta < row['reserved'] + row['allocated']:
                raise CommerceError('ineligible', 'Adjustment would consume held or allocated stock')
            result = dict(_one(tx, 'UPDATE inventory.stock_items SET on_hand=on_hand+%s,version=version+1 WHERE variant_id=%s RETURNING *,variant_id AS id', (delta, variant_id)))
            movement = str(uuid.uuid4())
            tx.execute('INSERT INTO inventory.movements(id,variant_id,delta,reason,created_at,actor_id,reference) VALUES(%s,%s,%s,%s,%s,%s,%s)', (movement, variant_id, delta, reason.strip(), _now(tx), actor['id'], command_id))
            result['movement_id'] = movement
            result['available'] = result['on_hand'] - result['reserved'] - result['allocated']
            return result
        return _run(self, actor, 'adjust_stock', variant_id, [delta, reason, expected_revision], command_id, work)

    def get_availability(self, variant_ids, *, actor=None):

        def work(tx):
            return {r['variant_id']: {**dict(r), 'available': r['on_hand'] - r['reserved'] - r['allocated']} for r in tx.execute('SELECT * FROM inventory.stock_items WHERE variant_id=ANY(%s) ORDER BY variant_id', (variant_ids,)).fetchall()}
        return _read(self, actor, 'get_availability', None, work, True)

    def list_movements(self, variant_id, cursor=None, *, actor=None):
        fingerprint = hashlib.sha256(variant_id.encode()).hexdigest()
        tail = _cursor_decode(cursor, fingerprint) or [0, '']

        def work(tx):
            rows = [dict(r) for r in tx.execute('SELECT * FROM inventory.movements WHERE variant_id=%s AND (created_at,id)>(%s,%s) ORDER BY created_at,id LIMIT 51', (variant_id, tail[0], tail[1])).fetchall()]
            balance = _one(tx, 'SELECT *,on_hand-reserved-allocated AS available FROM inventory.stock_items WHERE variant_id=%s', (variant_id,))
            items = rows[:50]
            return {'id': variant_id, 'items': items, 'balances': balance, 'next_cursor': _cursor_encode([items[-1]['created_at'], items[-1]['id']], fingerprint) if len(rows) > 50 else None}
        return _read(self, actor, 'list_movements', variant_id, work)

    def record_shipment(self, allocation_id, quantities, shipment_id, *, actor=None, command_id=None):
        quantities = _quantities(quantities)
        shipping = _receipt(self, 'shipping', {'id': shipment_id}, actor)
        if shipping.get('state') != 'handed_over' or shipping.get('quantities') != quantities:
            raise CommerceError('ineligible', 'Exact authoritative handover units required')

        def work(tx):
            row = _one(tx, 'SELECT * FROM inventory.allocations WHERE id=%s FOR UPDATE', (allocation_id,))
            if shipping['order_id'] != row['order_id']:
                raise CommerceError('ineligible', 'Handover allocation/order mismatch')
            old = tx.execute('SELECT * FROM inventory.shipments WHERE id=%s', (shipment_id,)).fetchone()
            if old:
                if _json(old['quantities']) != quantities or old['allocation_id'] != allocation_id:
                    raise CommerceError('idempotency_conflict', 'Shipment already recorded with different quantities')
                return {'id': shipment_id, 'allocation_id': allocation_id, 'state': 'shipped', 'version': row['version'], 'quantities': quantities}
            remaining = _json(row['remaining'])
            _lock_stock(tx, quantities)
            if any((value > remaining.get(key, 0) for key, value in quantities.items())):
                raise CommerceError('ineligible', 'Shipment exceeds allocation')
            for key, value in quantities.items():
                remaining[key] -= value
                tx.execute('UPDATE inventory.stock_items SET allocated=allocated-%s,on_hand=on_hand-%s,version=version+1 WHERE variant_id=%s', (value, value, key))
                tx.execute("INSERT INTO inventory.movements(id,variant_id,delta,reason,created_at,actor_id,reference) VALUES(%s,%s,%s,'shipment',%s,%s,%s)", (shipment_id + ':' + key, key, -value, _now(tx), actor['id'], shipment_id))
            tx.execute('INSERT INTO inventory.shipments VALUES(%s,%s,%s::jsonb)', (shipment_id, allocation_id, _canonical(quantities)))
            tx.execute('UPDATE inventory.allocations SET remaining=%s::jsonb,version=version+1 WHERE id=%s', (_canonical(remaining), allocation_id))
            return {'id': shipment_id, 'allocation_id': allocation_id, 'order_id': row['order_id'], 'state': 'shipped', 'version': row['version'] + 1, 'quantities': quantities}
        return _run(self, actor, 'record_shipment', allocation_id, [quantities, shipment_id], command_id, work)

    def restock_return(self, return_receipt_id, variant_id, quantity, condition, *, actor=None, command_id=None):
        inspection = _receipt(self, 'returns', condition, actor)
        if inspection.get('state') != 'inspected':
            raise CommerceError('ineligible', 'Physical inspection required')
        units = [u for u in inspection.get('units', []) if u.get('variant_id') == variant_id and u.get('disposition') == 'sellable' and u.get('accepted')]
        _integer(quantity, 1, 99)
        if len(units) != quantity:
            raise CommerceError('ineligible', 'Restock must match exact sellable inspected units')

        def work(tx):
            key = 'return:' + return_receipt_id + ':' + variant_id
            old = tx.execute('SELECT * FROM inventory.movements WHERE id=%s', (key,)).fetchone()
            if old:
                if old['delta'] != quantity:
                    raise CommerceError('idempotency_conflict', 'Restock quantity changed')
                return {'id': key, 'state': 'restocked', 'version': 1, 'quantity': quantity, 'unit_ids': [u['unit_id'] for u in units]}
            _lock_stock(tx, {variant_id: quantity})
            tx.execute('UPDATE inventory.stock_items SET on_hand=on_hand+%s,version=version+1 WHERE variant_id=%s', (quantity, variant_id))
            tx.execute("INSERT INTO inventory.movements(id,variant_id,delta,reason,created_at,actor_id,reference) VALUES(%s,%s,%s,'inspected_return',%s,%s,%s)", (key, variant_id, quantity, _now(tx), actor['id'], return_receipt_id))
            return {'id': key, 'state': 'restocked', 'version': 1, 'quantity': quantity, 'unit_ids': [u['unit_id'] for u in units]}
        return _run(self, actor, 'restock_return', return_receipt_id, [variant_id, quantity, inspection.get('receipt_id')], command_id, work)

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

def _cursor_decode(cursor, fingerprint):
    if cursor is None:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if set(data) != {'v', 'filter', 'tail'} or data['v'] != 1 or data['filter'] != fingerprint:
            raise ValueError()
        return data['tail']
    except Exception:
        raise CommerceError('invalid_input', 'Cursor does not match this query')

def _cursor_encode(tail, fingerprint):
    return base64.urlsafe_b64encode(_canonical({'v': 1, 'filter': fingerprint, 'tail': tail}).encode()).decode().rstrip('=')

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
