from __future__ import annotations
import hashlib
import json
import secrets
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class CartAssociation:

    def capture_guest_handoff(self, guest_cart_id, destination_principal, expected_revision, *, actor=None, command_id=None):
        _client_command(destination_principal)
        _integer(expected_revision, 1)
        if actor.get('kind') != 'guest':
            raise CommerceError('ineligible', 'A current guest is required for handoff capture')

        def work(tx):
            row = _cart(tx, guest_cart_id, actor, None, True)
            _version(row, expected_revision)
            target = tx.execute('SELECT id FROM cart.carts WHERE account_id=%s', (destination_principal,)).fetchone()
            destination = target['id'] if target else str(uuid.uuid5(uuid.NAMESPACE_URL, 'account-cart:' + destination_principal))
            ident = str(uuid.uuid4())
            snapshot = {'lines': _json(row['lines']), 'coupon': row['coupon'], 'version': row['version']}
            tx.execute('INSERT INTO cart.handoffs(id,guest_id,source_principal,destination_principal,destination_cart_id,source_revision,snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)', (ident, guest_cart_id, actor['id'], destination_principal, destination, row['version'], _canonical(snapshot)))
            tx.execute("UPDATE cart.carts SET state='handoff_pending',version=version+1 WHERE id=%s", (guest_cart_id,))
            return {'id': ident, 'guest_cart_id': guest_cart_id, 'source_principal': actor['id'], 'destination_principal': destination_principal, 'destination_cart_id': destination, 'state': 'prepared', 'version': 1}
        return _run(self, actor, 'capture_guest_handoff', guest_cart_id, [destination_principal, expected_revision], command_id, work)

    def create_guest_cart(self, account_id=None, *, actor=None, command_id=None):
        if account_id is not None and account_id != actor.get('id'):
            raise CommerceError('not_found', 'Account unavailable')

        def work(tx):
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('cart-owner:' + actor['id'],))
            old = tx.execute("SELECT * FROM cart.carts WHERE owner_id=%s AND state='active' AND expires_at>%s ORDER BY updated_at DESC,id LIMIT 1", (actor['id'], _now(tx))).fetchone()
            if old:
                return _cart_view(old)
            ident = str(uuid.uuid5(uuid.NAMESPACE_URL, 'account-cart:' + account_id)) if account_id else str(uuid.uuid4())
            now = _now(tx)
            end = now + (90 if account_id else 30) * 86400000
            tx.execute('INSERT INTO cart.carts(id,owner_id,account_id,grant_hash,updated_at,expires_at) VALUES(%s,%s,%s,NULL,%s,%s)', (ident, actor['id'], account_id, now, end))
            return _cart_view(_one(tx, 'SELECT * FROM cart.carts WHERE id=%s', (ident,)))
        return _run(self, actor, 'create_guest_cart', None, [account_id], command_id, work)

    def create_replacement_cart(self, return_receipt, variant_id, *, actor=None, command_id=None):
        if actor.get('kind') != 'recovery':
            raise CommerceError('forbidden', 'Admitted inspected-return obligation required')
        actual = _receipt(self, 'returns', return_receipt, actor)
        if actual.get('state') != 'inspected' or not actual.get('unit_ids') or self.catalog_port is None or (variant_id not in self.catalog_port([variant_id])):
            raise CommerceError('ineligible', 'Accepted return and published replacement required')
        original = self.receipt_inspectors['returns']({'id': actual['return_id'], 'kind': 'record'}, actor=actor)
        owner_id = original.get('owner_id') or actual.get('owner_id')
        if not owner_id:
            raise CommerceError('invalid_dependency', 'Return owner is required')

        def work(tx):
            ident = str(uuid.uuid4())
            now = _now(tx)
            tx.execute("INSERT INTO cart.carts(id,owner_id,lines,updated_at,expires_at,purpose,principal_kind) VALUES(%s,%s,%s::jsonb,%s,%s,%s,'customer')", (ident, owner_id, _canonical({variant_id: 1}), now, now + 90 * 86400000, 'replacement:' + actual['return_id']))
            return {'id': ident, 'owner_id': owner_id, 'state': 'active', 'version': 1, 'lines': [{'variant_id': variant_id, 'quantity': 1}], 'replacement_return_id': actual['return_id']}
        return _run(self, actor, 'create_replacement_cart', actual['return_id'], [variant_id], command_id, work)

    def get_for_principal(self, *, actor=None):

        def work(tx):
            row = tx.execute("SELECT * FROM cart.carts WHERE owner_id=%s AND state='active' AND expires_at>%s ORDER BY updated_at DESC,id LIMIT 1", (actor['id'], _now(tx))).fetchone()
            if not row:
                raise CommerceError('not_found', 'No active cart for this principal')
            return _cart_view(row)
        return _read(self, actor, 'get_for_principal', None, work)

    def merge_guest_cart(self, guest_cart_id, account_id, expected_revision, *, handoff_receipt=None, actor=None, command_id=None):
        if actor.get('id') != account_id or actor.get('kind') not in ('customer', 'staff') or (not isinstance(handoff_receipt, dict)):
            raise CommerceError('not_found', 'Bound authenticated handoff required')
        _integer(expected_revision, 1)

        def work(tx):
            handoff = _one(tx, 'SELECT * FROM cart.handoffs WHERE id=%s FOR UPDATE', (handoff_receipt.get('id'),))
            if handoff['guest_id'] != guest_cart_id or handoff['destination_principal'] != account_id:
                raise CommerceError('not_found', 'Handoff unavailable')
            if handoff['state'] == 'consumed':
                return _json(handoff['merge_result'])
            now = _now(tx)
            tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('cart-owner:' + account_id,))
            tx.execute('INSERT INTO cart.carts(id,owner_id,account_id,updated_at,expires_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', (handoff['destination_cart_id'], account_id, account_id, now, now + 90 * 86400000))
            rows = {r['id']: dict(r) for r in tx.execute('SELECT * FROM cart.carts WHERE id=ANY(%s) ORDER BY id FOR UPDATE', ([guest_cart_id, handoff['destination_cart_id']],)).fetchall()}
            guest = rows[guest_cart_id]
            target = rows[handoff['destination_cart_id']]
            _version(target, expected_revision)
            if guest['state'] != 'handoff_pending' or guest['owner_id'] != handoff['source_principal']:
                raise CommerceError('conflict', 'Guest handoff state changed')
            lines = _json(target['lines'])
            source = _json(handoff['snapshot'])
            caps = []
            dropped = []
            for variant, quantity in sorted(source['lines'].items()):
                before = lines.get(variant, 0)
                capacity = max(0, 200 - sum(lines.values()))
                addition = min(quantity, 99 - before, capacity)
                if variant not in lines and len(lines) >= 100:
                    addition = 0
                if addition:
                    lines[variant] = before + addition
                if addition < quantity:
                    caps.append({'variant_id': variant, 'requested': before + quantity, 'quantity': before + addition})
                    dropped.append({'variant_id': variant, 'quantity': quantity - addition})
            conflict = bool(target['coupon'] and source['coupon'] and (target['coupon'] != source['coupon']))
            result = {**_cart_update(tx, target, lines, target['coupon'] or source['coupon']), 'capped_lines': caps, 'dropped_lines': dropped, 'coupon_conflict': conflict, 'handoff_id': handoff['id']}
            tx.execute("UPDATE cart.carts SET state='merged',grant_hash=NULL,version=version+1 WHERE id=%s", (guest_cart_id,))
            tx.execute("UPDATE cart.handoffs SET state='consumed',merge_result=%s::jsonb WHERE id=%s", (_canonical(result), handoff['id']))
            return result
        return _run(self, actor, 'merge_guest_cart', guest_cart_id, [account_id, expected_revision, handoff_receipt.get('id')], command_id, work)

    def pending_handoffs(self, *, actor=None):

        def work(tx):
            return {'items': [{'id': r['id'], 'guest_cart_id': r['guest_id'], 'destination_principal': r['destination_principal'], 'destination_cart_id': r['destination_cart_id'], 'source_revision': r['source_revision'], 'destination_version': (tx.execute('SELECT version FROM cart.carts WHERE id=%s', (r['destination_cart_id'],)).fetchone() or {'version': 1})['version'], 'state': r['state'], 'version': 1} for r in tx.execute('SELECT * FROM cart.handoffs WHERE destination_principal=%s ORDER BY id', (actor['id'],)).fetchall()]}
        return _read(self, actor, 'pending_handoffs', None, work)

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
