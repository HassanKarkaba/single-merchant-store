from __future__ import annotations
import hashlib
import json
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class ReturnDisposition:

    def bind_refund_plan(self, return_id, plan_receipt, *, actor=None, command_id=None):
        plan = _receipt(self, 'orders', plan_receipt, actor)

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            proposal = _json(row['refund_plan'])
            if not proposal or plan.get('kind') != 'return' or plan.get('order_id') != row['order_id'] or (set(plan.get('unit_ids', [])) != set(proposal['unit_ids'])):
                raise CommerceError('ineligible', 'Original approved unit-plan binding required')
            if proposal.get('order_plan_id') and proposal['order_plan_id'] != plan['id']:
                raise CommerceError('conflict', 'Return is already bound to another plan')
            proposal['order_plan_id'] = plan['id']
            tx.execute('UPDATE returns.requests SET refund_plan=%s::jsonb,version=version+1 WHERE id=%s', (_canonical(proposal), return_id))
            return {'id': return_id, 'return_id': return_id, 'order_id': row['order_id'], 'state': row['state'], 'version': row['version'] + 1, 'refund_plan': proposal}
        return _run(self, actor, 'bind_refund_plan', return_id, [plan['id']], command_id, work)

    def build_refund_plan(self, return_id, *, actor=None, command_id=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            if row['refund_plan']:
                return _json(row['refund_plan'])
            if row['state'] != 'inspected':
                raise CommerceError('ineligible', 'All units must be inspected; rejected-all is a disposition without refund')
            inspections = tx.execute('SELECT * FROM returns.unit_inspections WHERE return_id=%s ORDER BY unit_id', (return_id,)).fetchall()
            units = [u for u in _inspection_units(row, inspections) if u['accepted']]
            amount = sum((u['net_minor'] + u['tax_minor'] for u in units))
            ident = str(uuid.uuid5(uuid.UUID(return_id), 'refund-proposal'))
            proposal = {'id': ident, 'return_id': return_id, 'order_id': row['order_id'], 'state': 'approved', 'version': 1, 'currency': 'USD', 'amount_minor': amount, 'unit_ids': [u['unit_id'] for u in units], 'unit_allocations': units, 'shipping_refund_minor': 0, 'shipping_tax_refund_minor': 0, 'merchant_fault': row['merchant_fault'], 'policy_revision': _json(row['snapshot'])['policy'].get('revision')}
            tx.execute('UPDATE returns.requests SET refund_plan=%s::jsonb,version=version+1 WHERE id=%s', (_canonical(proposal), return_id))
            return proposal
        return _run(self, actor, 'build_refund_plan', return_id, [], command_id, work)

    def close_return(self, return_id, refund_receipt=None, *, actor=None, command_id=None):
        plan = _receipt(self, 'orders', refund_receipt, actor) if refund_receipt else None

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            if row['state'] == 'rejected_disposition':
                tx.execute("UPDATE returns.unit_claims SET state='released' WHERE return_id=%s", (return_id,))
                return dict(_one(tx, "UPDATE returns.requests SET state='closed_rejected',version=version+1 WHERE id=%s RETURNING id,order_id,state,version", (return_id,)))
            if not plan or plan.get('state') not in ('succeeded', 'ZERO_REFUND') or plan.get('order_id') != row['order_id'] or (set(plan.get('unit_ids', [])) != set(_json(row['refund_plan'])['unit_ids'])):
                raise CommerceError('ineligible', 'Original accepted-unit financial obligation must be settled')
            tx.execute("UPDATE returns.unit_claims SET state='returned' WHERE return_id=%s", (return_id,))
            state = 'exchange_pending' if row['replacement_checkout_id'] and row['replacement_state'] == 'pending' else 'closed'
            return dict(_one(tx, 'UPDATE returns.requests SET state=%s,version=version+1 WHERE id=%s RETURNING id,order_id,state,version', (state, return_id)))
        return _run(self, actor, 'close_return', return_id, [plan.get('id') if plan else None], command_id, work)

    def create_exchange_link(self, return_id, replacement_checkout_id, *, actor=None, command_id=None):
        checkout = _receipt(self, 'checkout', {'id': replacement_checkout_id}, actor)
        observed = self.get_return(return_id, actor=actor)
        proposal = observed.get('refund_plan') or {}
        plan_id = proposal.get('order_plan_id')
        if not plan_id:
            raise CommerceError('ineligible', 'Actual inspected original refund plan required')
        plan = _receipt(self, 'orders', {'id': plan_id}, actor)

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            settled = plan.get('state') in ('succeeded', 'ZERO_REFUND')
            if _json(row['snapshot']).get('resolution') != 'exchange' or row['state'] not in ('inspected', 'refund_pending', 'exchange_pending', 'closed') or (row['state'] == 'closed' and (not settled)) or (not row['refund_plan']) or (checkout.get('owner_id') != row['owner_id']) or (checkout.get('replacement_return_id') != return_id) or (plan.get('owner_id') != row['owner_id']) or (plan.get('order_id') != row['order_id']) or (plan.get('state') not in ('approved', 'succeeded', 'ZERO_REFUND')) or (set(plan.get('unit_ids', [])) != set(_json(row['refund_plan'])['unit_ids'])):
                raise CommerceError('ineligible', 'Actual same-customer return-bound checkout and approved original plan required')
            if row['replacement_checkout_id']:
                if row['replacement_checkout_id'] != replacement_checkout_id:
                    raise CommerceError('conflict', 'Replacement reference already linked')
                return {k: row[k] for k in ('id', 'order_id', 'state', 'version', 'replacement_checkout_id', 'replacement_state')}
            return dict(_one(tx, "UPDATE returns.requests SET replacement_checkout_id=%s,replacement_state='pending',state='exchange_pending',version=version+1 WHERE id=%s RETURNING id,order_id,state,version,replacement_checkout_id,replacement_state", (replacement_checkout_id, return_id)))
        return _run(self, actor, 'create_exchange_link', return_id, [replacement_checkout_id], command_id, work)

    def record_exchange_outcome(self, return_id, *, actor=None, command_id=None):
        row = self.get_return(return_id, actor=actor)
        if not row.get('replacement_checkout_id'):
            raise CommerceError('ineligible', 'No replacement checkout is linked')
        checkout = _receipt(self, 'checkout', {'id': row['replacement_checkout_id']}, actor)
        if checkout.get('owner_id') != row['owner_id'] or checkout.get('replacement_return_id') != return_id:
            raise CommerceError('invalid_dependency', 'Actual same-customer return-bound checkout required')
        state = str(checkout.get('purchase_state', checkout.get('state'))).upper()
        target = 'completed' if state == 'PAID' and isinstance(checkout.get('purchase_confirmation'), dict) and (checkout['purchase_confirmation'].get('payment_state') == 'paid') else 'declined' if state in ('DECLINED', 'CANCELED', 'FAILED', 'EXPIRED') else 'pending'
        plan_id = (row.get('refund_plan') or {}).get('order_plan_id')
        plan = _receipt(self, 'orders', {'id': plan_id}, actor) if plan_id else None
        settled = plan is not None and plan.get('state') in ('succeeded', 'ZERO_REFUND')

        def work(tx):
            current = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            if current['replacement_state'] == 'completed' and target != 'completed':
                raise CommerceError('conflict', 'Completed replacement cannot regress')
            result_state = 'closed' if settled and target in ('completed', 'declined') else current['state']
            return dict(_one(tx, 'UPDATE returns.requests SET replacement_state=%s,state=%s,version=version+1 WHERE id=%s RETURNING id,order_id,state,version,replacement_checkout_id,replacement_state', (target, result_state, return_id)))
        return _run(self, actor, 'record_exchange_outcome', return_id, [checkout['id'], target], command_id, work)

    def record_inspection(self, return_id, unit_results, expected_revision, *, actor=None, command_id=None):
        _integer(expected_revision, 1)
        if not isinstance(unit_results, list) or not unit_results or len({u.get('unit_id') for u in unit_results}) != len(unit_results):
            raise CommerceError('invalid_input', 'Per-unit results required')
        for result in unit_results:
            if set(result) != {'unit_id', 'accepted', 'disposition'} or type(result['accepted']) is not bool or result['disposition'] not in ('sellable', 'quarantine', 'discard', 'rejected') or (result['accepted'] == (result['disposition'] == 'rejected')):
                raise CommerceError('invalid_input', 'Accepted units need explicit sellable/quarantine/discard disposition')

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            _version(row, expected_revision)
            if row['state'] not in ('received', 'inspecting'):
                raise CommerceError('ineligible', 'Physical receipt must precede inspection')
            received = {r['unit_id'] for r in tx.execute('SELECT unit_id FROM returns.received_units WHERE return_id=%s', (return_id,)).fetchall()}
            for result in sorted(unit_results, key=lambda u: u['unit_id']):
                if result['unit_id'] not in received:
                    raise CommerceError('ineligible', 'Unit has not physically arrived')
                old = tx.execute('SELECT * FROM returns.unit_inspections WHERE return_id=%s AND unit_id=%s FOR UPDATE', (return_id, result['unit_id'])).fetchone()
                if old and (old['accepted'] != result['accepted'] or old['disposition'] != result['disposition']):
                    raise CommerceError('conflict', 'Original inspection disposition is immutable')
                tx.execute('INSERT INTO returns.unit_inspections VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', (return_id, result['unit_id'], result['accepted'], result['disposition'], _now(tx)))
            inspections = tx.execute('SELECT * FROM returns.unit_inspections WHERE return_id=%s ORDER BY unit_id', (return_id,)).fetchall()
            complete = len(inspections) == len(_json(row['units']))
            state = 'inspected' if complete and any((i['accepted'] for i in inspections)) else 'rejected_disposition' if complete else 'inspecting'
            tx.execute('UPDATE returns.requests SET state=%s,version=version+1 WHERE id=%s', (state, return_id))
            return {'id': return_id, 'return_id': return_id, 'order_id': row['order_id'], 'state': state, 'version': row['version'] + 1, 'units': _inspection_units(row, inspections), 'unit_ids': [i['unit_id'] for i in inspections if i['accepted']], 'merchant_fault': row['merchant_fault']}
        return _run(self, actor, 'record_inspection', return_id, [unit_results, expected_revision], command_id, work)

    def record_received(self, return_id, unit_ids, expected_revision, *, actor=None, command_id=None):
        if not isinstance(unit_ids, list) or not unit_ids or len(unit_ids) != len(set(unit_ids)):
            raise CommerceError('invalid_input', 'Distinct physically received units required')
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM returns.requests WHERE id=%s FOR UPDATE', (return_id,))
            _version(row, expected_revision)
            if row['state'] not in ('approved', 'received', 'inspecting') or not set(unit_ids) <= set(_json(row['units'])):
                raise CommerceError('ineligible', 'Approved remaining return units required')
            for unit in sorted(unit_ids):
                tx.execute('INSERT INTO returns.received_units VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (return_id, unit, _now(tx)))
            return dict(_one(tx, "UPDATE returns.requests SET received=true,state='received',version=version+1 WHERE id=%s RETURNING id,order_id,state,version", (return_id,)))
        return _run(self, actor, 'record_received', return_id, [unit_ids, expected_revision], command_id, work)

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
