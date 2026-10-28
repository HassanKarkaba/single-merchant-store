from __future__ import annotations
import base64
import hashlib
import json
import re
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class OrderRecords:

    def create_provisional(self, checkout_id, quote_snapshot, contact_snapshot, shipping_snapshot, policy_snapshot=None, *, actor=None, command_id=None):
        if quote_snapshot.get('currency') != 'USD':
            raise CommerceError('invalid_input', 'Exact USD quote required')
        units = _order_units(quote_snapshot)
        total = _integer(quote_snapshot.get('total_minor'))
        owner_id = contact_snapshot.get('actor_id', contact_snapshot.get('owner_id'))
        if not owner_id:
            raise CommerceError('invalid_input', 'Verified checkout owner required')
        if actor.get('kind') != 'recovery' and owner_id != actor.get('id'):
            raise CommerceError('not_found', 'Checkout unavailable')
        snapshot = {'quote': quote_snapshot, 'contact': contact_snapshot, 'shipping': shipping_snapshot, 'policy': policy_snapshot or {'revision': 'portfolio-policy-1', 'return_days': 30}, 'lines': quote_snapshot['lines'], 'currency': 'USD', 'total_minor': total}
        expected = sum((u['net_minor'] + u['tax_minor'] for u in units)) + _integer(quote_snapshot.get('shipping_minor')) + _integer(quote_snapshot.get('shipping_tax_minor'))
        if expected != total:
            raise CommerceError('invalid_input', 'Unit and shipping allocations do not conserve total')

        def work(tx):
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('order-checkout:' + checkout_id,))
            old = tx.execute('SELECT * FROM orders.orders WHERE checkout_id=%s FOR UPDATE', (checkout_id,)).fetchone()
            if old:
                if _json(old['snapshot']) != snapshot or old['owner_id'] != owner_id:
                    raise CommerceError('idempotency_conflict', 'Checkout already has a different immutable order')
                return _order_view(old)
            ident = str(uuid.uuid4())
            tx.execute('INSERT INTO orders.orders(id,checkout_id,owner_id,snapshot,created_at,unit_schema_version) VALUES(%s,%s,%s,%s::jsonb,%s,1)', (ident, checkout_id, owner_id, _canonical(snapshot), _now(tx)))
            for unit in units:
                tx.execute('INSERT INTO orders.units(order_id,unit_id,variant_id,allocation) VALUES(%s,%s,%s,%s::jsonb)', (ident, unit['unit_id'], unit['variant_id'], _canonical(unit)))
            return _order_view(_one(tx, 'SELECT * FROM orders.orders WHERE id=%s', (ident,)))
        return _run(self, actor, 'create_provisional', checkout_id, [quote_snapshot, contact_snapshot, shipping_snapshot, policy_snapshot], command_id, work)

    def export_orders(self, *, actor=None):
        return self.list_merchant_orders(actor=actor)

    def get_order(self, order_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s', (order_id,))
            _ownership(row, actor)
            return _order_detail(tx, row)
        return _read(self, actor, 'get_order', order_id, work)

    def get_receipt(self, evidence, *, actor=None):
        ident = evidence.get('plan_id') or evidence.get('id') or evidence.get('order_id')

        def work(tx):
            plan = tx.execute('SELECT * FROM orders.refund_plans WHERE id=%s', (ident,)).fetchone()
            if plan:
                _ownership(plan, actor)
                return {**_json(plan['snapshot']), 'state': plan['state'], 'version': plan['version'], 'owner': 'orders'}
            row = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s', (ident,))
            _ownership(row, actor)
            return _order_detail(tx, row)
        return _read(self, actor, 'get_receipt', ident, work)

    def list_customer_orders(self, customer_id, *, actor=None, cursor=None, filters=None):
        if actor.get('kind') != 'recovery' and actor.get('kind') != 'staff' and (customer_id != actor['id']):
            raise CommerceError('not_found', 'Orders unavailable')
        return _order_page(self, customer_id, filters or {}, cursor, actor)

    def list_merchant_orders(self, *, actor=None, cursor=None, filters=None):
        return _order_page(self, None, filters or {}, cursor, actor)

    def merchant_summary(self, *, actor=None):
        return self.summary(actor=actor)


    def summary(self, *, actor=None):

        def work(tx):
            row = _one(tx, "SELECT count(*) AS order_count,count(*) FILTER(WHERE payment_state='paid') AS paid_order_count,COALESCE(sum((snapshot->>'total_minor')::bigint) FILTER(WHERE payment_state='paid'),0) AS simulated_captured_minor,COALESCE(sum(refunded_minor),0) AS simulated_refunded_minor FROM orders.orders WHERE COALESCE((snapshot->>'benchmark_only')::boolean,false)=false")
            return {key: int(value) for key, value in row.items()}
        return _read(self, actor, 'merchant_summary', None, work)

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

def _now(tx):
    return tx.execute('SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint AS value').fetchone()['value']

def _one(tx, query, params=()):
    row = tx.execute(query, params).fetchone()
    if row is None:
        raise CommerceError('not_found', 'Resource unavailable')
    return dict(row)

def _order_detail(tx, row):
    result = _order_view(row)
    units = [dict(u) for u in tx.execute('SELECT * FROM orders.units WHERE order_id=%s ORDER BY unit_id', (row['id'],)).fetchall()]
    result['unit_records'] = units
    for field, predicate in (('shipped', 'shipped'), ('canceled', 'canceled'), ('returned', 'returned')):
        result[field] = {}
        for unit in units:
            if unit[predicate]:
                result[field][unit['variant_id']] = result[field].get(unit['variant_id'], 0) + 1
    result['shipped_unit_ids'] = [u['unit_id'] for u in units if u['shipped']]
    result['canceled_unit_ids'] = [u['unit_id'] for u in units if u['canceled']]
    result['returned_unit_ids'] = [u['unit_id'] for u in units if u['returned']]
    result['delivered_unit_times'] = {u['unit_id']: u['delivered_at'] for u in units if u['delivered_at'] is not None}
    result['refund_plans'] = [{**_json(p['snapshot']), 'state': p['state'], 'version': p['version']} for p in tx.execute('SELECT * FROM orders.refund_plans WHERE order_id=%s ORDER BY id', (row['id'],)).fetchall()]
    result['shipments'] = [_json(r['evidence']) for r in tx.execute("SELECT evidence FROM orders.facts WHERE order_id=%s AND kind='shipment' ORDER BY created_at,id", (row['id'],)).fetchall()]
    result['legacy_read_only'] = row['unit_schema_version'] != 1
    result['fulfillment_state'] = 'unfulfilled' if not result['shipped_unit_ids'] else 'fulfilled' if units and all((u['shipped'] or u['canceled'] for u in units)) else 'partial'
    return result

def _order_page(service, customer_id, filters, cursor, actor):
    if set(filters) - {'query', 'payment_state', 'limit'}:
        raise CommerceError('invalid_input', 'Unsupported order filter')
    query = filters.get('query', '')
    state = filters.get('payment_state', '')
    limit = _integer(filters.get('limit', 50), 1, 50)
    if not isinstance(query, str) or len(query) > 200 or state not in ('', 'pending', 'paid', 'failed'):
        raise CommerceError('invalid_input', 'Invalid order filter')
    fingerprint = hashlib.sha256(_canonical([customer_id, {k: v for k, v in filters.items() if k != 'limit'}]).encode()).hexdigest()
    tail = _cursor_decode(cursor, fingerprint) or ''

    def work(tx):
        rows = tx.execute("SELECT * FROM orders.orders WHERE id>%s AND (%s::text IS NULL OR owner_id=%s) AND (%s='' OR id ILIKE %s) AND (%s='' OR payment_state=%s) ORDER BY id LIMIT %s", (tail, customer_id, customer_id, query, '%' + query + '%', state, state, limit + 1)).fetchall()
        items = [_order_detail(tx, r) for r in rows[:limit]]
        return {'items': items, 'next_cursor': _cursor_encode(items[-1]['id'], fingerprint) if len(rows) > limit else None}
    return _read(service, actor, 'list_customer_orders' if customer_id else 'list_merchant_orders', customer_id, work)

def _order_units(quote):
    result = []
    seen = set()
    for line in quote.get('lines', []):
        quantity = _integer(line.get('quantity'), 1, 99)
        units = line.get('units')
        if not isinstance(units, list) or len(units) != quantity:
            raise CommerceError('invalid_input', 'Original per-unit quote allocations required')
        for unit in units:
            ident = unit.get('unit_id')
            if not isinstance(ident, str) or ident in seen:
                raise CommerceError('invalid_input', 'Distinct original unit IDs required')
            gross = _integer(unit.get('gross_minor'))
            discount = _integer(unit.get('discount_minor'))
            net = _integer(unit.get('net_minor'))
            tax = _integer(unit.get('tax_minor'))
            if gross - discount != net:
                raise CommerceError('invalid_input', 'Original unit net does not conserve money')
            seen.add(ident)
            result.append({**unit, 'variant_id': line['variant_id']})
    if not 1 <= len(result) <= 200:
        raise CommerceError('invalid_input', 'One to two hundred units required')
    return result

def _order_view(row):
    row = dict(row)
    snapshot = _json(row['snapshot'])
    return {**row, 'snapshot': snapshot, 'lines': snapshot['lines'], 'currency': 'USD', 'total_minor': snapshot['total_minor'], 'quantities': {l['variant_id']: l['quantity'] for l in snapshot['lines']}}

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
