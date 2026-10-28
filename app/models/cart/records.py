from __future__ import annotations
import hashlib
import json
import re
import secrets
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class CartRecords:

    def clear_purchased_lines(self, cart_id, purchase_receipt, *, actor=None, command_id=None):
        purchase = _receipt(self, 'orders', purchase_receipt, actor)
        if purchase.get('payment_state') != 'paid':
            raise CommerceError('ineligible', 'A durable paid order is required')
        quantities = {line['variant_id']: line['quantity'] for line in purchase['lines']}

        def work(tx):
            row = _one(tx, 'SELECT * FROM cart.carts WHERE id=%s FOR UPDATE', (cart_id,))
            lines = _json(row['lines'])
            if tx.execute('SELECT 1 FROM cart.consumptions WHERE purchase_id=%s', (purchase['id'],)).fetchone():
                return _cart_view(row)
            for variant, value in quantities.items():
                if lines.get(variant, 0) >= value:
                    lines[variant] -= value
                    if lines[variant] == 0:
                        del lines[variant]
            tx.execute('INSERT INTO cart.consumptions VALUES(%s,%s)', (purchase['id'], cart_id))
            return _cart_update(tx, row, lines, row['coupon'])
        return _run(self, actor, 'clear_purchased_lines', cart_id, [purchase['id']], command_id, work)

    def expire_cart(self, cart_id, *, actor=None, command_id=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM cart.carts WHERE id=%s FOR UPDATE', (cart_id,))
            _ownership(row, actor)
            if row['expires_at'] > _now(tx):
                raise CommerceError('ineligible', 'Cart has not expired')
            return dict(_one(tx, "UPDATE cart.carts SET state='expired',grant_hash=NULL,version=version+1 WHERE id=%s RETURNING id,state,version", (cart_id,)))
        return _run(self, actor, 'expire_cart', cart_id, [], command_id, work)

    def get_cart(self, cart_id, grant=None, *, actor=None):

        def work(tx):
            row = _cart(tx, cart_id, actor, grant)
            row.pop('grant_hash', None)
            row['lines'] = [{'variant_id': k, 'quantity': v} for k, v in _json(row['lines']).items()]
            return row
        return _read(self, actor, 'get_cart', cart_id, work)

    def remove_line(self, cart_id, variant_id, expected_revision, grant=None, *, actor=None, command_id=None):

        def work(tx):
            row = _cart(tx, cart_id, actor, grant, True)
            _version(row, expected_revision)
            lines = _json(row['lines'])
            lines.pop(variant_id, None)
            return _cart_update(tx, row, lines, row['coupon'])
        return _run(self, actor, 'remove_line', cart_id, [variant_id, expected_revision], command_id, work)

    def set_coupon(self, cart_id, coupon_code, expected_revision, grant=None, *, actor=None, command_id=None):
        if coupon_code is not None and (not isinstance(coupon_code, str) or not re.fullmatch('[A-Za-z0-9_-]{1,40}', coupon_code)):
            raise CommerceError('invalid_input', 'Invalid coupon')

        def work(tx):
            row = _cart(tx, cart_id, actor, grant, True)
            _version(row, expected_revision)
            return _cart_update(tx, row, _json(row['lines']), coupon_code.upper() if coupon_code else None)
        return _run(self, actor, 'set_coupon', cart_id, [coupon_code, expected_revision], command_id, work)

    def set_line(self, cart_id, variant_id, quantity, expected_revision, grant=None, *, actor=None, command_id=None):
        _integer(quantity, 1, 99)
        _integer(expected_revision, 1)
        if self.catalog_port is None or variant_id not in self.catalog_port([variant_id]):
            raise CommerceError('ineligible', 'Published variant is unavailable')

        def work(tx):
            row = _cart(tx, cart_id, actor, None, True)
            _version(row, expected_revision)
            lines = _json(row['lines'])
            if variant_id not in lines and len(lines) >= 100 or sum(lines.values()) - lines.get(variant_id, 0) + quantity > 200:
                raise CommerceError('limit_exceeded', 'Cart permits one hundred lines and two hundred units')
            lines[variant_id] = quantity
            return _cart_update(tx, row, lines, row['coupon'])
        return _run(self, actor, 'set_line', cart_id, [variant_id, quantity, expected_revision], command_id, work)

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

def _cart(tx, cart_id, actor, grant, lock=False):
    row = _one(tx, 'SELECT * FROM cart.carts WHERE id=%s' + (' FOR UPDATE' if lock else ''), (cart_id,))
    _ownership(row, actor)
    if row['state'] != 'active' or _now(tx) >= row['expires_at']:
        raise CommerceError('expired', 'Cart expired or merged')
    if grant is not None and (not secrets.compare_digest(hashlib.sha256(grant.encode()).hexdigest(), row['grant_hash'] or '')):
        raise CommerceError('not_found', 'Cart unavailable')
    return row

def _cart_update(tx, row, lines, coupon):
    now = _now(tx)
    end = now + (90 if row['account_id'] else 30) * 86400000
    result = dict(_one(tx, 'UPDATE cart.carts SET lines=%s::jsonb,coupon=%s,version=version+1,updated_at=%s,expires_at=%s WHERE id=%s RETURNING id,owner_id,account_id,lines,coupon,version,state,expires_at', (_canonical(lines), coupon, now, end, row['id'])))
    result['lines'] = [{'variant_id': k, 'quantity': v} for k, v in _json(result['lines']).items()]
    return result

def _cart_view(row):
    row = dict(row)
    row.pop('grant_hash', None)
    row['lines'] = [{'variant_id': k, 'quantity': v} for k, v in sorted(_json(row['lines']).items())]
    return row

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
