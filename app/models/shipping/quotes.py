from __future__ import annotations
import hashlib
import json
import re
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class ShippingQuotes:

    def get_offer(self, offer_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM shipping.rate_quotes WHERE id=%s', (offer_id,))
            _ownership(row, actor)
            return _json(row['snapshot'])
        return _read(self, actor, 'get_offer', offer_id, work)

    def quote(self, destination, package=None, lines=None, *, order_id=None, actor=None, command_id=None):
        _fixture_address(destination)
        if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
            raise CommerceError('invalid_input', 'Shipment lines are required')
        order = self.order_port(order_id, actor=actor) if order_id and self.order_port else None
        if order is not None and destination != order['snapshot']['contact']['address']:
            raise CommerceError('invalid_input', 'Parcel destination must match original paid snapshot')
        authoritative = {line['variant_id']: {**line, 'id': line['variant_id']} for line in order['lines']} if order else self.catalog_port([line['variant_id'] for line in lines]) if self.catalog_port else {}
        snapshots = []
        for line in lines:
            variant = authoritative.get(line['variant_id'])
            if not variant:
                raise CommerceError('dependency_unavailable', 'Authoritative variant weights are required')
            quantity = _integer(line.get('quantity'), 1, 99)
            snapshots.append({'variant_id': variant['id'], 'quantity': quantity, 'weight_g': _integer(variant.get('weight_g'), 1, 50000), 'dimensions_mm': variant.get('dimensions_mm')})
        parcel = _parcel(snapshots)
        if package is not None and package != parcel:
            raise CommerceError('invalid_input', 'Parcel must match deterministic PF-SHIP1 packing')
        fingerprint = hashlib.sha256(_canonical([destination, snapshots, parcel, 'PF-SHIP1']).encode()).hexdigest()

        def work(tx):
            ident = str(uuid.uuid4())
            now = _now(tx)
            extra = max(0, (parcel['weight_g'] - 1000 + 999) // 1000)
            offers = []
            for service, name, amount, estimate in (('demo_standard', 'Simulated Standard', 500 + 100 * extra, 'Simulated 3–5 business days'), ('demo_express', 'Simulated Express', 1000 + 200 * extra, 'Simulated 1–2 business days')):
                offer_id = str(uuid.uuid5(uuid.UUID(ident), service))
                offer = {'id': offer_id, 'offer_id': offer_id, 'quote_id': ident, 'owner_id': actor['id'], 'version': 1, 'state': 'offered', 'currency': 'USD', 'service': service, 'name': name, 'amount_minor': amount, 'merchant_cost_minor': amount, 'estimate': estimate, 'expires_at': now + 600000, 'issued_at': now, 'request_fingerprint': fingerprint, 'fixture_revision': 'PF-SHIP1', 'destination': destination, 'lines': snapshots, 'package': parcel, 'simulated': True}
                tx.execute('INSERT INTO shipping.rate_quotes(id,snapshot,expires_at,owner_id) VALUES(%s,%s::jsonb,%s,%s)', (offer_id, _canonical(offer), offer['expires_at'], actor['id']))
                offers.append(offer)
            return {'id': ident, 'state': 'quoted', 'version': 1, 'offers': offers, 'expires_at': now + 600000, 'request_fingerprint': fingerprint, 'package': parcel, 'fixture_revision': 'PF-SHIP1'}
        return _run(self, actor, 'quote', None, [destination, parcel, snapshots], command_id, work)

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

def _fixture_address(address):
    if not isinstance(address, dict) or set(address) - {'recipient', 'name', 'line1', 'line2', 'city', 'region', 'postal_code', 'country', 'email'}:
        raise CommerceError('invalid_input', 'Closed synthetic destination required')
    if address.get('recipient', address.get('name')) != 'Demo Customer' or address.get('line1') != '100 Example Street' or address.get('city') != 'Demo City' or (address.get('country') != 'US') or ({'MI': '48104', 'CA': '94105', 'TX': '78701'}.get(address.get('region')) != address.get('postal_code')):
        raise CommerceError('invalid_input', 'Use one of the three PF-ADDR1 synthetic destinations')
    if address.get('line2', '') and (not re.fullmatch('Unit (?:[1-9]|[1-9][0-9])', address['line2'])):
        raise CommerceError('invalid_input', 'Synthetic apartment must be Unit 1 through Unit 99')
    if 'email' in address and (not isinstance(address['email'], str) or not re.fullmatch('[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\\.invalid', address['email'])):
        raise CommerceError('invalid_input', 'Use synthetic .invalid email only')

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

def _parcel(lines):
    weight = 0
    length = 0
    width = 0
    height = 0
    for line in lines:
        quantity = _integer(line['quantity'], 1, 99)
        dimensions = line.get('dimensions_mm')
        if not isinstance(dimensions, list) or len(dimensions) != 3:
            raise CommerceError('invalid_input', 'Variant dimensions are missing')
        for value in dimensions:
            _integer(value, 1, 2000)
        weight += _integer(line['weight_g'], 1, 50000) * quantity
        length = max(length, dimensions[0])
        width = max(width, dimensions[1])
        height += dimensions[2] * quantity
    _integer(weight, 1, 50000)
    _integer(height, 1, 2000)
    return {'weight_g': weight, 'dimensions_mm': [length, width, height]}

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
