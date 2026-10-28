from __future__ import annotations
import hashlib
import json
import re
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class TaxQuotes:

    def calculate(self, checkout_id, address, discounted_lines, shipping_minor, *, actor=None, command_id=None):
        _fixture_address(address)
        _integer(shipping_minor)
        if not isinstance(discounted_lines, list) or not 1 <= len(discounted_lines) <= 100:
            raise CommerceError('invalid_input', 'Bounded lines required')
        rate = {'MI': 600, 'CA': 725, 'TX': 625}[address['region']]

        def work(tx):
            amounts = []
            for line in discounted_lines:
                if line.get('tax_code', line.get('attributes', {}).get('tax_code')) != 'demo_physical_standard':
                    raise CommerceError('invalid_input', 'The fixed demo tax code is required')
                net = _integer(line.get('net_minor'))
                quantity = _integer(line.get('quantity'), 1, 99)
                units = line.get('units')
                if not isinstance(units, list) or len(units) != quantity or len({u.get('unit_id') for u in units}) != quantity or (sum((_integer(u.get('net_minor')) for u in units)) != net):
                    raise CommerceError('invalid_input', 'Exact original unit net allocations required')
                tax = (net * rate + 5000) // 10000
                allocation = _allocate(tax, [u['net_minor'] for u in units])
                amounts.append({'variant_id': line['variant_id'], 'net_minor': net, 'tax_minor': tax, 'units': [{**u, 'tax_minor': minor} for u, minor in zip(units, allocation)]})
            shipping_tax = (shipping_minor * rate + 5000) // 10000
            total = sum((l['tax_minor'] for l in amounts)) + shipping_tax
            _integer(total)
            ident = str(uuid.uuid4())
            now = _now(tx)
            result = {'id': ident, 'calculation_id': ident, 'checkout_id': checkout_id, 'owner_id': actor['id'], 'version': 1, 'state': 'calculated', 'profile': self.profile, 'fixture_revision': 'PF-TAX1', 'currency': 'USD', 'tax_minor': total, 'total_tax_minor': total, 'lines': amounts, 'shipping_tax_minor': shipping_tax, 'shipping_minor': shipping_minor, 'jurisdictions': [{'region': address['region'], 'rate_basis_points': rate, 'synthetic': True}], 'expires_at': now + 600000, 'fingerprint': hashlib.sha256(_canonical([address, discounted_lines, shipping_minor, 'PF-TAX1']).encode()).hexdigest()}
            tx.execute('INSERT INTO tax.calculations(id,checkout_id,snapshot,expires_at,owner_id) VALUES(%s,%s,%s::jsonb,%s,%s)', (ident, checkout_id, _canonical(result), result['expires_at'], actor['id']))
            return result
        return _run(self, actor, 'calculate', checkout_id, [address, discounted_lines, shipping_minor], command_id, work)

    def get_calculation(self, calculation_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM tax.calculations WHERE id=%s', (calculation_id,))
            _ownership(row, actor)
            return _json(row['snapshot'])
        return _read(self, actor, 'get_calculation', calculation_id, work)

def _allocate(total, weights):
    denominator = sum(weights)
    if not denominator:
        return [0] * len(weights)
    result = [total * w // denominator for w in weights]
    for i in sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % denominator), i))[:total - sum(result)]:
        result[i] += 1
    return result

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
