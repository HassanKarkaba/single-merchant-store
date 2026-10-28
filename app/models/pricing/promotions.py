from __future__ import annotations
import hashlib
import json
import re
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class Promotions:

    def activate_promotion(self, promotion_id, expected_revision, *, actor=None, command_id=None):
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM pricing.promotions WHERE id=%s FOR UPDATE', (promotion_id,))
            _version(row, expected_revision)
            if row['state'] != 'draft':
                raise CommerceError('conflict', 'Only a draft promotion can activate')
            return dict(_one(tx, "UPDATE pricing.promotions SET state='active',version=version+1 WHERE id=%s RETURNING id,state,version", (promotion_id,)))
        return _run(self, actor, 'activate_promotion', promotion_id, [expected_revision], command_id, work)

    def commit_redemption(self, quote_id, order_id, *, actor=None, command_id=None):

        def work(tx):
            row = tx.execute('SELECT * FROM pricing.redemptions WHERE quote_id=%s FOR UPDATE', (quote_id,)).fetchone()
            if not row:
                return {'id': order_id, 'state': 'not_required', 'version': 1}
            if row['state'] == 'released':
                raise CommerceError('conflict', 'Redemption was released')
            if row['order_id'] and row['order_id'] != order_id:
                raise CommerceError('conflict', 'Quote already consumed')
            return dict(_one(tx, "UPDATE pricing.redemptions SET state='committed',order_id=%s,version=version+1 WHERE attempt_id=%s RETURNING attempt_id AS id,version,state", (order_id, row['attempt_id'])))
        return _run(self, actor, 'commit_redemption', order_id, [quote_id], command_id, work)

    def create_promotion(self, kind, value, eligible_variants=None, min_subtotal=0, start_at=0, end_at=9999999999999, max_redemptions=None, per_customer_limit=None, *, coupon_code=None, actor=None, command_id=None):
        if kind not in ('percentage', 'fixed', 'free_shipping'):
            raise CommerceError('invalid_input', 'Unsupported promotion')
        _integer(value, 0 if kind == 'free_shipping' else 1, 10000 if kind == 'percentage' else 99999999)
        _integer(min_subtotal)
        _integer(start_at, 0, 9999999999999)
        _integer(end_at, 1, 9999999999999)
        if end_at <= start_at:
            raise CommerceError('invalid_input', 'Promotion end must follow start')
        if not isinstance(eligible_variants or [], list) or len(eligible_variants or []) > 5000 or len(set(eligible_variants or [])) != len(eligible_variants or []) or any((not isinstance(v, str) for v in eligible_variants or [])):
            raise CommerceError('invalid_input', 'Distinct eligible variants required')
        for cap in (max_redemptions, per_customer_limit):
            if cap is not None:
                _integer(cap, 1, 1000000)
        if coupon_code is not None and (not isinstance(coupon_code, str) or not re.fullmatch('[A-Za-z0-9_-]{1,40}', coupon_code)):
            raise CommerceError('invalid_input', 'Invalid coupon code')

        def work(tx):
            ident = str(uuid.uuid4())
            tx.execute("INSERT INTO pricing.promotions VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,'draft',1)", (ident, coupon_code.upper() if coupon_code else None, kind, value, _canonical(eligible_variants or []), min_subtotal, start_at, end_at, max_redemptions, per_customer_limit))
            return {'id': ident, 'state': 'draft', 'version': 1}
        return _run(self, actor, 'create_promotion', None, [kind, value, eligible_variants, min_subtotal, start_at, end_at, max_redemptions, per_customer_limit, coupon_code], command_id, work)

    def end_promotion(self, promotion_id, expected_revision, *, actor=None, command_id=None):
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM pricing.promotions WHERE id=%s FOR UPDATE', (promotion_id,))
            _version(row, expected_revision)
            return dict(_one(tx, "UPDATE pricing.promotions SET state='ended',version=version+1 WHERE id=%s RETURNING id,state,version", (promotion_id,)))
        return _run(self, actor, 'end_promotion', promotion_id, [expected_revision], command_id, work)

    def get_quote(self, quote_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM pricing.quotes WHERE id=%s', (quote_id,))
            _ownership(row, actor)
            return _json(row['snapshot'])
        return _read(self, actor, 'get_quote', quote_id, work)

    def inspect_redemption_hold(self, attempt_id, *, actor=None):

        def work(tx):
            row = tx.execute('SELECT * FROM pricing.redemptions WHERE attempt_id=%s', (attempt_id,)).fetchone()
            return dict(row) if row else {'id': attempt_id, 'state': 'not_required', 'version': 1}
        return _read(self, actor, 'inspect_redemption_hold', attempt_id, work)

    def list_promotions(self, cursor=None, *, actor=None):
        return _read(self, actor, 'list_promotions', None, lambda tx: {'items': [dict(r) for r in tx.execute('SELECT * FROM pricing.promotions WHERE id>%s ORDER BY id LIMIT 50', (cursor or '',)).fetchall()], 'next_cursor': None})

    def quote(self, lines, coupon_code=None, customer_id=None, quote_at=None, *, actor=None, command_id=None):
        if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
            raise CommerceError('invalid_input', 'One to 100 lines required')
        if self.catalog_port is None:
            raise CommerceError('dependency_unavailable', 'Catalog owner port required')
        authority = self.catalog_port([line.get('variant_id') for line in lines])
        for line in lines:
            if line.get('variant_id') not in authority:
                raise CommerceError('ineligible', 'Variant is not currently sellable')
        lines = [{**authority[line['variant_id']], 'variant_id': line['variant_id'], 'quantity': line['quantity'], 'tax_code': authority[line['variant_id']].get('tax_code', authority[line['variant_id']].get('attributes', {}).get('tax_code'))} for line in lines]
        ids = []
        for line in lines:
            _integer(line.get('quantity'), 1, 99)
            if not isinstance(line.get('variant_id'), str) or line['variant_id'] in ids:
                raise CommerceError('invalid_input', 'Unique variant IDs required')
            ids.append(line['variant_id'])

        def work(tx):
            now = _now(tx)
            prices = {r['variant_id']: r for r in tx.execute('SELECT * FROM pricing.prices WHERE variant_id=ANY(%s)', (ids,)).fetchall()}
            if len(prices) != len(ids):
                raise CommerceError('ineligible', 'Price unavailable')
            result = []
            for line in sorted(lines, key=lambda l: l['variant_id']):
                price = prices[line['variant_id']]
                result.append({**line, 'unit_minor': price['unit_minor'], 'price_revision': price['version'], 'gross_minor': price['unit_minor'] * line['quantity']})
            gross = sum((line['gross_minor'] for line in result))
            _integer(gross)
            promotion = None
            if coupon_code:
                promotion = tx.execute("SELECT * FROM pricing.promotions WHERE code=%s AND state='active' AND start_at<=%s AND end_at>%s", (coupon_code.upper(), now, now)).fetchone()
                if not promotion:
                    raise CommerceError('ineligible', 'Coupon is unavailable')
                if promotion['per_customer_limit'] and (not customer_id):
                    raise CommerceError('ineligible', 'This coupon requires an account')
                if gross < promotion['min_subtotal']:
                    raise CommerceError('ineligible', 'Coupon minimum is not met')
            else:
                promotion = tx.execute("SELECT * FROM pricing.promotions WHERE code IS NULL AND state='active' AND start_at<=%s AND end_at>%s AND min_subtotal<=%s ORDER BY id LIMIT 1", (now, now, gross)).fetchone()
            eligible = [l for l in result if promotion and (not promotion['eligible'] or l['variant_id'] in promotion['eligible'])]
            eligible_total = sum((l['gross_minor'] for l in eligible))
            discount = 0
            if promotion and promotion['kind'] == 'fixed':
                discount = min(eligible_total, promotion['value'])
            if promotion and promotion['kind'] == 'percentage':
                discount = (eligible_total * promotion['value'] + 5000) // 10000
            allocations = allocate_minor(discount, [l['gross_minor'] if l in eligible else 0 for l in result], [l['variant_id'] for l in result])
            for line, allocated in zip(result, allocations):
                line['discount_minor'] = allocated
                line['net_minor'] = line['gross_minor'] - allocated
                unit_discounts = allocate_minor(allocated, [1] * line['quantity'], [f"{line['variant_id']}:{i:02}" for i in range(line['quantity'])])
                line['units'] = [{'unit_id': f"{line['variant_id']}:{i + 1}", 'variant_id': line['variant_id'], 'gross_minor': line['unit_minor'], 'discount_minor': d, 'net_minor': line['unit_minor'] - d} for i, d in enumerate(unit_discounts)]
            ident = str(uuid.uuid4())
            snapshot = {'id': ident, 'quote_id': ident, 'version': 1, 'state': 'quoted', 'currency': 'USD', 'lines': result, 'item_gross_minor': gross, 'discount_minor': discount, 'item_net_minor': gross - discount, 'shipping_discount_rule': 'free' if promotion and promotion['kind'] == 'free_shipping' else None, 'promotion_id': promotion['id'] if promotion else None, 'customer_id': customer_id, 'expires_at': now + 600000}
            snapshot['fingerprint'] = hashlib.sha256(_canonical(snapshot).encode()).hexdigest()
            tx.execute('INSERT INTO pricing.quotes(id,snapshot,expires_at,version,owner_id) VALUES(%s,%s::jsonb,%s,1,%s)', (ident, _canonical(snapshot), snapshot['expires_at'], actor['id']))
            return snapshot
        return _run(self, actor, 'quote', None, [lines, coupon_code, customer_id], command_id, work)

    def release_redemption_hold(self, attempt_id, *, evidence=None, actor=None, command_id=None):
        actual = _receipt(self, 'payments', evidence or {'id': attempt_id}, actor)
        if not actual.get('capture_fenced') or str(actual.get('state')).upper() not in ('CANCELED', 'DECLINED', 'FAILED', 'NOT_CREATED'):
            raise CommerceError('unknown_outcome', 'Current no-future-capture evidence is required')

        def work(tx):
            row = tx.execute('SELECT * FROM pricing.redemptions WHERE attempt_id=%s FOR UPDATE', (attempt_id,)).fetchone()
            if not row:
                return {'id': attempt_id, 'state': 'not_required', 'version': 1}
            if row['state'] == 'committed':
                return {'id': attempt_id, 'state': 'committed', 'version': row['version']}
            return dict(_one(tx, "UPDATE pricing.redemptions SET state='released',version=version+1 WHERE attempt_id=%s RETURNING attempt_id AS id,state,version", (attempt_id,)))
        return _run(self, actor, 'release_redemption_hold', attempt_id, [actual.get('id'), actual.get('state')], command_id, work)

    def reserve_redemption(self, quote_id, attempt_id, expected_revision=None, expires_at=None, *, actor=None, command_id=None):

        def work(tx):
            quote = _json(_one(tx, 'SELECT snapshot FROM pricing.quotes WHERE id=%s', (quote_id,))['snapshot'])
            now = _now(tx)
            if expires_at is None or type(expires_at) is not int or now >= expires_at:
                raise CommerceError('expired', 'Original reservation expiry is required')
            if now >= quote['expires_at']:
                admission = _receipt(self, 'checkout', {'quote_id': quote_id, 'attempt_id': attempt_id}, actor)
                if admission.get('frozen_quote_until') != expires_at:
                    raise CommerceError('expired', 'Quote has no matching timely admitted hold')
            if not quote['promotion_id']:
                return {'id': attempt_id, 'version': 1, 'state': 'not_required'}
            promotion = _one(tx, 'SELECT * FROM pricing.promotions WHERE id=%s FOR UPDATE', (quote['promotion_id'],))
            old = tx.execute('SELECT * FROM pricing.redemptions WHERE attempt_id=%s', (attempt_id,)).fetchone()
            if old:
                if old['quote_id'] != quote_id:
                    raise CommerceError('idempotency_conflict', 'Attempt already reserves another quote')
                return dict(old)
            count = tx.execute("SELECT count(*) AS value FROM pricing.redemptions WHERE promotion_id=%s AND state IN ('held','committed')", (promotion['id'],)).fetchone()['value']
            if promotion['max_redemptions'] is not None and count >= promotion['max_redemptions']:
                raise CommerceError('ineligible', 'Coupon redemption limit reached')
            if promotion['per_customer_limit']:
                if not quote['customer_id']:
                    raise CommerceError('ineligible', 'Account required')
                count = tx.execute("SELECT count(*) AS value FROM pricing.redemptions WHERE promotion_id=%s AND customer_id=%s AND state IN ('held','committed')", (promotion['id'], quote['customer_id'])).fetchone()['value']
                if count >= promotion['per_customer_limit']:
                    raise CommerceError('ineligible', 'Customer coupon limit reached')
            end = expires_at
            tx.execute("INSERT INTO pricing.redemptions VALUES(%s,%s,%s,%s,NULL,'held',%s,1)", (attempt_id, quote_id, promotion['id'], quote['customer_id'], end))
            return {'id': attempt_id, 'version': 1, 'state': 'held', 'expires_at': end}
        return _run(self, actor, 'reserve_redemption', attempt_id, [quote_id, expected_revision, expires_at], command_id, work)

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

def allocate_minor(total, weights, tie_keys=None):
    _integer(total)
    if not isinstance(weights, list) or any((type(w) is not int or w < 0 for w in weights)):
        raise CommerceError('invalid_input', 'Integer allocation weights required')
    denominator = sum(weights)
    if denominator == 0:
        if total:
            raise CommerceError('invalid_input', 'Cannot allocate a positive total to zero weight')
        return [0] * len(weights)
    tie_keys = tie_keys or [str(i) for i in range(len(weights))]
    if len(tie_keys) != len(weights):
        raise CommerceError('invalid_input', 'Allocation tie keys mismatch')
    result = [total * w // denominator for w in weights]
    remainder = total - sum(result)
    ranked = sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % denominator), str(tie_keys[i])))
    for index in ranked[:remainder]:
        result[index] += 1
    return result
