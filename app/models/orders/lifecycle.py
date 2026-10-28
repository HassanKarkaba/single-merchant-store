from __future__ import annotations
import hashlib
import json
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class OrderLifecycle:

    def _fact(self, order_id, kind, evidence, actor, command_id):

        def work(tx):
            row = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s FOR UPDATE', (order_id,))
            _ownership(row, actor)
            if row['payment_state'] != 'paid':
                raise CommerceError('ineligible', 'Paid order required')
            key = kind + ':' + str(evidence.get('id') or evidence.get('receipt_id') or '')
            if key.endswith(':'):
                raise CommerceError('invalid_input', 'Effect receipt identity required')
            old = tx.execute('SELECT evidence FROM orders.facts WHERE id=%s', (key,)).fetchone()
            if old:
                if _json(old['evidence']) != evidence:
                    raise CommerceError('idempotency_conflict', 'Receipt content changed')
                return _order_view(row)
            snapshot = _json(row['snapshot'])
            ordered = {l['variant_id']: l['quantity'] for l in snapshot['lines']}
            if kind in ('shipment', 'return'):
                column = 'shipped' if kind == 'shipment' else 'returned'
                counts = _json(row[column])
                maximum = ordered if kind == 'shipment' else _json(row['shipped'])
                for variant, value in evidence.get('quantities', {}).items():
                    _integer(value, 1, 99)
                    ceiling = maximum.get(variant, 0) - (_json(row['canceled']).get(variant, 0) if kind == 'shipment' else 0)
                    if counts.get(variant, 0) + value > ceiling:
                        raise CommerceError('ineligible', 'Quantity exceeds remaining units')
                    counts[variant] = counts.get(variant, 0) + value
                tx.execute('UPDATE orders.orders SET ' + column + '=%s::jsonb WHERE id=%s', (_canonical(counts), order_id))
            if kind == 'refund':
                value = _integer(evidence.get('amount_minor'), 1)
                if row['refunded_minor'] + value > snapshot['total_minor']:
                    raise CommerceError('ineligible', 'Refund exceeds captured amount')
                tx.execute('UPDATE orders.orders SET refunded_minor=refunded_minor+%s WHERE id=%s', (value, order_id))
            tx.execute('INSERT INTO orders.facts VALUES(%s,%s,%s,%s::jsonb,%s)', (key, order_id, kind, _canonical(evidence), _now(tx)))
            return _order_view(_one(tx, 'UPDATE orders.orders SET version=version+1 WHERE id=%s RETURNING *', (order_id,)))
        return _run(self, actor, 'record_' + kind, order_id, evidence, command_id, work)

    def confirm_payment(self, order_id, payment_receipt, allocation_receipt, *, actor=None, command_id=None):
        payment = _receipt(self, 'payments', payment_receipt, actor)
        allocation = _receipt(self, 'inventory', allocation_receipt, actor)
        if str(payment.get('state')).upper() not in ('CAPTURED', 'ZERO_TOTAL', 'ZERO_SETTLEMENT') or allocation.get('order_id') != order_id or allocation.get('state') != 'allocated':
            raise CommerceError('ineligible', 'Actual settlement and matching allocation receipts required')

        def work(tx):
            row = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s FOR UPDATE', (order_id,))
            snapshot = _json(row['snapshot'])
            if payment.get('amount_minor', payment.get('captured_minor')) != snapshot['total_minor'] or payment.get('currency') != 'USD':
                raise CommerceError('conflict', 'Settlement amount/currency mismatch')
            if row['payment_state'] == 'paid':
                return _order_view(row)
            tx.execute("INSERT INTO orders.facts VALUES(%s,%s,'payment',%s::jsonb,%s)", ('payment:' + order_id, order_id, _canonical({'payment': payment, 'allocation': allocation}), _now(tx)))
            return _order_view(_one(tx, "UPDATE orders.orders SET payment_state='paid',state='paid',version=version+1 WHERE id=%s RETURNING *", (order_id,)))
        return _run(self, actor, 'confirm_payment', order_id, [payment.get('receipt_id', payment.get('id')), allocation['id']], command_id, work)

    def get_refund_plan(self, plan_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM orders.refund_plans WHERE id=%s', (plan_id,))
            _ownership(row, actor)
            return {**_json(row['snapshot']), 'state': row['state'], 'version': row['version']}
        return _read(self, actor, 'get_refund_plan', plan_id, work)

    def mark_payment_failed(self, order_id, evidence, *, actor=None, command_id=None):
        payment = _receipt(self, 'payments', evidence, actor)
        if str(payment.get('state')).upper() not in ('DECLINED', 'CANCELED', 'FAILED') or payment.get('captured_minor', 0):
            raise CommerceError('unknown_outcome', 'Definitive no-capture receipt required')

        def work(tx):
            row = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s FOR UPDATE', (order_id,))
            if row['payment_state'] == 'paid':
                raise CommerceError('conflict', 'Paid order cannot be rewritten as failed')
            return _order_view(_one(tx, "UPDATE orders.orders SET payment_state='failed',state='payment_failed',version=version+1 WHERE id=%s RETURNING *", (order_id,)))
        return _run(self, actor, 'mark_payment_failed', order_id, [payment.get('id'), payment.get('state')], command_id, work)

    def plan_units(self, order_id, unit_ids, kind, reason, source_receipt, merchant_fault=False, expected_revision=None, *, actor=None, command_id=None):
        if kind not in ('cancellation', 'return') or not isinstance(unit_ids, list) or (not unit_ids) or (len(unit_ids) != len(set(unit_ids))) or (type(merchant_fault) is not bool) or (not isinstance(reason, str)) or (not 1 <= len(reason) <= 300):
            raise CommerceError('invalid_input', 'Approved original-unit plan required')
        source = _receipt(self, 'shipping' if kind == 'cancellation' else 'returns', source_receipt, actor)
        if source.get('order_id') != order_id or source.get('state') not in (('cancel_acknowledged',) if kind == 'cancellation' else ('approved', 'inspected')) or (not set(unit_ids) <= set(source.get('unit_ids', []))):
            raise CommerceError('ineligible', 'Matching cancellation fence or approved inspection plan required')

        def work(tx):
            order = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s FOR UPDATE', (order_id,))
            _version(order, expected_revision)
            if order['payment_state'] != 'paid' or order['unit_schema_version'] != 1:
                raise CommerceError('ineligible', 'Paid exact-allocation order required')
            rows = [dict(r) for r in tx.execute('SELECT * FROM orders.units WHERE order_id=%s ORDER BY unit_id FOR UPDATE', (order_id,)).fetchall()]
            selected = [r for r in rows if r['unit_id'] in unit_ids]
            if len(selected) != len(unit_ids) or any((r['refund_plan_id'] or r['refunded'] or (kind == 'cancellation' and r['shipped']) or (kind == 'return' and (not r['returned'])) for r in selected)):
                raise CommerceError('ineligible', 'Units are already claimed or ineligible')
            ident = str(uuid.uuid4())
            allocations = [_json(r['allocation']) for r in selected]
            shipping = 0
            shipping_tax = 0
            snapshot = _json(order['snapshot'])
            whole_fault = merchant_fault and all((r['unit_id'] in unit_ids or ((r['canceled'] or r['returned']) and r['merchant_fault']) for r in rows))
            if whole_fault and (not tx.execute('SELECT 1 FROM orders.shipping_refund_claims WHERE order_id=%s', (order_id,)).fetchone()):
                shipping = _integer(snapshot['quote']['shipping_minor'])
                shipping_tax = _integer(snapshot['quote']['shipping_tax_minor'])
                tx.execute("INSERT INTO orders.shipping_refund_claims VALUES(%s,%s,%s,%s,'held')", (order_id, ident, shipping, shipping_tax))
            for row in selected:
                tx.execute("UPDATE orders.units SET refund_plan_id=%s,canceled=CASE WHEN %s='cancellation' THEN true ELSE canceled END,merchant_fault=%s WHERE order_id=%s AND unit_id=%s", (ident, kind, merchant_fault, order_id, row['unit_id']))
            amount = sum((u['net_minor'] + u['tax_minor'] for u in allocations)) + shipping + shipping_tax
            plan = {'id': ident, 'plan_id': ident, 'order_id': order_id, 'owner_id': order['owner_id'], 'kind': kind, 'reason': reason, 'merchant_fault': merchant_fault, 'unit_ids': sorted(unit_ids), 'unit_allocations': allocations, 'amount_minor': amount, 'shipping_refund_minor': shipping, 'shipping_tax_refund_minor': shipping_tax, 'currency': 'USD', 'state': 'approved', 'version': 1, 'source_receipt_id': source.get('receipt_id', source.get('id'))}
            tx.execute('INSERT INTO orders.refund_plans(id,order_id,owner_id,kind,snapshot) VALUES(%s,%s,%s,%s,%s::jsonb)', (ident, order_id, order['owner_id'], kind, _canonical(plan)))
            tx.execute('UPDATE orders.orders SET version=version+1 WHERE id=%s', (order_id,))
            return plan
        return _run(self, actor, 'plan_units', order_id, [unit_ids, kind, reason, source.get('receipt_id', source.get('id')), merchant_fault, expected_revision], command_id, work)

    def record_delivery(self, order_id, shipment_receipt, *, actor=None, command_id=None):
        shipment = _receipt(self, 'shipping', shipment_receipt, actor)
        if shipment.get('tracking_state') != 'delivered' or shipment.get('order_id') != order_id or type(shipment.get('delivered_at')) is not int:
            raise CommerceError('ineligible', 'Actual parcel delivered-unit facts required')
        return _unit_fact(self, order_id, 'delivery', shipment, actor, command_id)

    def record_fulfillment(self, order_id, shipment_receipt, *, inventory_receipt=None, actor=None, command_id=None):
        shipment = _receipt(self, 'shipping', shipment_receipt, actor)
        movement = _receipt(self, 'inventory', {'id': (inventory_receipt or {}).get('id', shipment['id']), 'kind': 'shipment'}, actor)
        if shipment.get('state') != 'handed_over' or shipment.get('order_id') != order_id or movement.get('state') != 'shipped' or (movement.get('order_id') != order_id):
            raise CommerceError('ineligible', 'Actual handover and stock movement required')
        return _unit_fact(self, order_id, 'shipment', shipment, actor, command_id)

    def record_refund(self, order_id, refund_receipt, *, plan_id=None, actor=None, command_id=None):
        payment = _receipt(self, 'payments', refund_receipt, actor)
        if str(payment.get('state')).upper() != 'SUCCEEDED' or not plan_id:
            raise CommerceError('ineligible', 'Confirmed original-plan refund required')

        def work(tx):
            order = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s FOR UPDATE', (order_id,))
            row = _one(tx, 'SELECT * FROM orders.refund_plans WHERE id=%s FOR UPDATE', (plan_id,))
            plan = _json(row['snapshot'])
            if row['order_id'] != order_id or payment.get('amount_minor') != plan['amount_minor'] or payment.get('plan_id', plan_id) != plan_id:
                raise CommerceError('conflict', 'Refund does not match original approved plan')
            if row['state'] == 'succeeded':
                return {'id': plan_id, 'order_id': order_id, 'state': 'succeeded', 'version': row['version']}
            if order['refunded_minor'] + plan['amount_minor'] > _json(order['snapshot'])['total_minor']:
                raise CommerceError('ineligible', 'Refund exceeds original captured amount')
            tx.execute("UPDATE orders.refund_plans SET state='succeeded',version=version+1 WHERE id=%s", (plan_id,))
            tx.execute('UPDATE orders.units SET refunded=true WHERE order_id=%s AND refund_plan_id=%s', (order_id, plan_id))
            tx.execute("UPDATE orders.shipping_refund_claims SET state='settled' WHERE plan_id=%s", (plan_id,))
            tx.execute("INSERT INTO orders.facts VALUES(%s,%s,'refund',%s::jsonb,%s) ON CONFLICT DO NOTHING", ('refund:' + plan_id, order_id, _canonical(payment), _now(tx)))
            tx.execute('UPDATE orders.orders SET refunded_minor=refunded_minor+%s,version=version+1 WHERE id=%s', (plan['amount_minor'], order_id))
            return {'id': plan_id, 'order_id': order_id, 'state': 'succeeded', 'amount_minor': plan['amount_minor'], 'version': row['version'] + 1}
        return _run(self, actor, 'record_refund', order_id, [plan_id, payment.get('receipt_id', payment.get('id'))], command_id, work)

    def record_return(self, order_id, return_receipt, *, actor=None, command_id=None):
        inspection = _receipt(self, 'returns', return_receipt, actor)
        if inspection.get('state') != 'inspected' or inspection.get('order_id') != order_id:
            raise CommerceError('ineligible', 'Actual accepted inspection required')
        return _unit_fact(self, order_id, 'return', inspection, actor, command_id)

    def request_cancellation(self, *args, **kwargs):
        raise CommerceError('not_supported', 'Use the durable unit cancellation coordinator and Shipping fence')

    def settle_zero_plan(self, plan_id, *, actor=None, command_id=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM orders.refund_plans WHERE id=%s FOR UPDATE', (plan_id,))
            plan = _json(row['snapshot'])
            if plan['amount_minor'] != 0:
                raise CommerceError('ineligible', 'Only an approved zero-value plan can settle locally')
            tx.execute("UPDATE orders.refund_plans SET state='succeeded',version=version+1 WHERE id=%s", (plan_id,))
            tx.execute('UPDATE orders.units SET refunded=true WHERE order_id=%s AND refund_plan_id=%s', (row['order_id'], plan_id))
            return {'id': plan_id, 'plan_id': plan_id, 'order_id': row['order_id'], 'state': 'ZERO_REFUND', 'amount_minor': 0, 'unit_ids': plan['unit_ids'], 'version': row['version'] + 1}
        return _run(self, actor, 'settle_zero_plan', plan_id, [], command_id, work)

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

def _unit_fact(service, order_id, kind, evidence, actor, command_id):

    def work(tx):
        order = _one(tx, 'SELECT * FROM orders.orders WHERE id=%s FOR UPDATE', (order_id,))
        units = evidence.get('unit_ids') or [u['unit_id'] for u in evidence.get('units', []) if u.get('accepted')]
        if not units:
            raise CommerceError('ineligible', 'Exact unit facts required')
        key = kind + ':' + evidence['id']
        old = tx.execute('SELECT * FROM orders.facts WHERE id=%s', (key,)).fetchone()
        if old:
            return _order_detail(tx, order)
        rows = tx.execute('SELECT * FROM orders.units WHERE order_id=%s AND unit_id=ANY(%s) ORDER BY unit_id FOR UPDATE', (order_id, units)).fetchall()
        if len(rows) != len(units):
            raise CommerceError('ineligible', 'Foreign unit facts')
        for row in rows:
            if kind == 'shipment':
                if row['canceled'] or row['shipped']:
                    raise CommerceError('ineligible', 'Canceled or already shipped unit')
                tx.execute('UPDATE orders.units SET shipped=true WHERE order_id=%s AND unit_id=%s', (order_id, row['unit_id']))
            elif kind == 'delivery':
                if not row['shipped']:
                    raise CommerceError('ineligible', 'Delivery requires authoritative handover')
                tx.execute('UPDATE orders.units SET delivered_at=COALESCE(delivered_at,%s) WHERE order_id=%s AND unit_id=%s', (evidence['delivered_at'], order_id, row['unit_id']))
            else:
                if row['delivered_at'] is None:
                    raise CommerceError('ineligible', 'Only delivered units may be returned')
                tx.execute('UPDATE orders.units SET returned=true,merchant_fault=%s WHERE order_id=%s AND unit_id=%s', (bool(evidence.get('merchant_fault')), order_id, row['unit_id']))
        tx.execute('INSERT INTO orders.facts VALUES(%s,%s,%s,%s::jsonb,%s)', (key, order_id, kind, _canonical(evidence), _now(tx)))
        order = _one(tx, 'UPDATE orders.orders SET version=version+1 WHERE id=%s RETURNING *', (order_id,))
        return _order_detail(tx, order)
    return _run(service, actor, 'record_fulfillment' if kind == 'shipment' else 'record_' + kind, order_id, [evidence['id'], evidence.get('version')], command_id, work)

def _version(row, expected):
    if expected is not None and row['version'] != _integer(expected, 1):
        raise CommerceError('version_conflict', 'Resource changed; reload before editing')
