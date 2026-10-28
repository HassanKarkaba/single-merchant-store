from __future__ import annotations
import base64
import hashlib
import json
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class ReturnAuthorization:

    def approve_return(self, return_id, decision, reason, expected_revision, *, merchant_fault=False, actor=None, command_id=None):
        if decision not in ('approved', 'rejected') or type(merchant_fault) is not bool or (not isinstance(reason, str)) or (not 1 <= len(reason) <= 300):
            raise CommerceError('invalid_input', 'Explicit return decision required')
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            _version(row, expected_revision)
            if row['state'] != 'requested':
                raise CommerceError('conflict', 'Return already decided')
            if decision == 'rejected':
                tx.execute("UPDATE returns.unit_claims SET state='released' WHERE return_id=%s", (return_id,))
            return dict(_one(tx, 'UPDATE returns.requests SET state=%s,merchant_fault=%s,version=version+1 WHERE id=%s RETURNING id,order_id,owner_id,state,version,merchant_fault,units AS unit_ids', (decision, merchant_fault, return_id)))
        return _run(self, actor, 'approve_return', return_id, [decision, reason, expected_revision, merchant_fault], command_id, work)

    def get_receipt(self, evidence, *, actor=None):
        ident = evidence.get('return_id') or evidence.get('id')

        def work(tx):
            row = tx.execute("SELECT * FROM returns.requests WHERE id=%s OR refund_plan->>'id'=%s", (ident, ident)).fetchone()
            if not row:
                raise CommerceError('not_found', 'Return unavailable')
            _ownership(row, actor)
            view = _return_view(tx, row)
            if evidence.get('kind') == 'record':
                return view
            if evidence.get('kind') == 'plan' or ident != row['id']:
                if not row['refund_plan']:
                    raise CommerceError('ineligible', 'Return refund proposal unavailable')
                return {**_json(row['refund_plan']), 'merchant_fault': row['merchant_fault'], 'return_id': row['id']}
            return {'id': row['id'], 'return_id': row['id'], 'owner_id': row['owner_id'], 'order_id': row['order_id'], 'state': 'inspected' if row['state'] in ('inspected', 'refund_pending', 'exchange_pending', 'closed') else row['state'], 'version': row['version'], 'units': view['inspections'], 'unit_ids': [u['unit_id'] for u in view['inspections'] if u['accepted']], 'merchant_fault': row['merchant_fault']}
        return _read(self, actor, 'get_receipt', ident, work)

    def get_refund_plan(self, return_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s', (return_id,))
            _ownership(row, actor)
            if not row['refund_plan'] or not _json(row['refund_plan']).get('order_plan_id'):
                raise CommerceError('ineligible', 'Actual inspected and approved original refund plan required')
            return {**_json(row['refund_plan']), 'return_id': row['id'], 'order_id': row['order_id'], 'owner_id': row['owner_id'], 'return_state': row['state']}
        return _read(self, actor, 'get_refund_plan', return_id, work)

    def get_return(self, return_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s', (return_id,))
            _ownership(row, actor)
            return _return_view(tx, row)
        return _read(self, actor, 'get_return', return_id, work)

    def list_order_returns(self, order_id, cursor=None, *, actor=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Actual Orders owner port required')
        self.order_port(order_id, actor=actor)
        fingerprint = hashlib.sha256(order_id.encode()).hexdigest()
        tail = _cursor_decode(cursor, fingerprint) or ''

        def work(tx):
            rows = tx.execute('SELECT * FROM returns.requests WHERE order_id=%s AND id>%s ORDER BY id LIMIT 51', (order_id, tail)).fetchall()
            items = []
            for row in rows[:50]:
                _ownership(row, actor)
                items.append({**dict(row), 'unit_ids': _json(row['units'])})
            return {'items': items, 'next_cursor': _cursor_encode(items[-1]['id'], fingerprint) if len(rows) > 50 else None}
        return _read(self, actor, 'list_order_returns', order_id, work)

    def list_returns(self, *, actor=None):

        def work(tx):
            if actor.get('kind') in ('staff', 'service'):
                rows = tx.execute('SELECT * FROM returns.requests ORDER BY id LIMIT 100').fetchall()
            else:
                rows = tx.execute('SELECT * FROM returns.requests WHERE owner_id=%s ORDER BY id LIMIT 100', (actor['id'],)).fetchall()
            return {'items': [dict(r) for r in rows], 'next_cursor': None}
        return _read(self, actor, 'list_returns', None, work)

    def request_return(self, order_id, lines, reason, requested_resolution='refund', *, actor=None, command_id=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Authoritative order port required')
        order = self.order_port(order_id, actor=actor)
        _ownership(order, actor)
        if requested_resolution not in ('refund', 'exchange') or not isinstance(reason, str) or (not 1 <= len(reason) <= 300):
            raise CommerceError('invalid_input', 'Return reason and supported resolution required')
        selected = []
        values = {u['unit_id']: u for line in order['lines'] for u in line['units']}
        for line in lines:
            ids = line.get('unit_ids')
            if not isinstance(ids, list) or len(ids) != _integer(line.get('quantity'), 1, 99) or any((unit not in values or values[unit]['variant_id'] != line['variant_id'] for unit in ids)):
                raise CommerceError('invalid_input', 'Explicit original unit IDs required')
            selected.extend(ids)
        if not selected or len(selected) != len(set(selected)):
            raise CommerceError('invalid_input', 'Distinct return units required')
        times = order.get('delivered_unit_times', {})
        policy = order['snapshot']['policy']
        if policy.get('return_days', policy.get('return_window_days')) != 30 or any((unit not in times or unit in order.get('returned_unit_ids', []) for unit in selected)):
            raise CommerceError('ineligible', 'Only delivered remaining units are returnable')

        def work(tx):
            now = _now(tx)
            if any((now >= times[unit] + 30 * 86400000 for unit in selected)):
                raise CommerceError('expired', 'Original thirty-day return window expired')
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('return:' + order_id,))
            ident = str(uuid.uuid4())
            for unit in sorted(selected):
                prior = tx.execute('SELECT * FROM returns.unit_claims WHERE order_id=%s AND unit_id=%s FOR UPDATE', (order_id, unit)).fetchone()
                if prior and prior['state'] != 'released':
                    raise CommerceError('conflict', 'Unit already claimed by another return')
                tx.execute("INSERT INTO returns.unit_claims VALUES(%s,%s,%s,'reserved') ON CONFLICT(order_id,unit_id) DO UPDATE SET return_id=excluded.return_id,state='reserved'", (order_id, unit, ident))
            snapshot = {'order': order, 'reason': reason, 'resolution': requested_resolution, 'policy': policy, 'unit_values': {unit: values[unit] for unit in selected}}
            tx.execute('INSERT INTO returns.requests(id,order_id,owner_id,snapshot,units) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb)', (ident, order_id, order['owner_id'], _canonical(snapshot), _canonical(sorted(selected))))
            return {'id': ident, 'order_id': order_id, 'owner_id': order['owner_id'], 'state': 'requested', 'version': 1, 'unit_ids': sorted(selected)}
        return _run(self, actor, 'request_return', order_id, [lines, reason, requested_resolution], command_id, work)

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

def _inspection_units(row, inspections):
    values = _json(row['snapshot'])['unit_values']
    return [{**values[i['unit_id']], 'unit_id': i['unit_id'], 'accepted': i['accepted'], 'disposition': i['disposition'], 'return_id': row['id']} for i in inspections]

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

def _return_view(tx, row):
    result = dict(row)
    result['unit_ids'] = _json(row['units'])
    result['received_unit_ids'] = [r['unit_id'] for r in tx.execute('SELECT unit_id FROM returns.received_units WHERE return_id=%s ORDER BY unit_id', (row['id'],)).fetchall()]
    inspections = tx.execute('SELECT * FROM returns.unit_inspections WHERE return_id=%s ORDER BY unit_id', (row['id'],)).fetchall()
    result['inspections'] = _inspection_units(row, inspections)
    by_id = {u['unit_id']: u for u in result['inspections']}
    result['unit_records'] = [{**value, 'received': unit in result['received_unit_ids'], **by_id.get(unit, {})} for unit, value in _json(row['snapshot'])['unit_values'].items()]
    return result

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
