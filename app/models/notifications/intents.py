from __future__ import annotations
import hashlib
import json
import re
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class NotificationIntents:

    def enqueue_transactional(self, kind, aggregate_id, recipient_ref, template_version, variables, event_id, *, actor=None, command_id=None):
        source = _notification_source(self, kind, aggregate_id, actor)
        if recipient_ref != source['recipient'] or variables != source['variables']:
            raise CommerceError('invalid_input', 'Notification content must match authoritative safe owner view')

        def work(tx):
            return _notification_intent(tx, actor, kind, aggregate_id, recipient_ref, template_version, variables, event_id, source['owner_id'])
        return _run(self, actor, 'enqueue_transactional', aggregate_id, [kind, recipient_ref, template_version, variables, event_id], command_id or event_id, work)

    def get_delivery_status(self, intent_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT id,owner_id,state,attempts,next_at,version FROM notifications.intents WHERE id=%s', (intent_id,))
            _ownership(row, actor)
            return row
        return _read(self, actor, 'get_delivery_status', intent_id, work)

    def list_inbox(self, cursor=None, *, actor=None):

        def work(tx):
            rows = [dict(r) for r in tx.execute('SELECT id,kind,aggregate_id,content,created_at FROM notifications.synthetic_inbox WHERE owner_id=%s AND id>%s ORDER BY id LIMIT 51', (actor['id'], cursor or '')).fetchall()]
            return {'items': rows[:50], 'next_cursor': rows[49]['id'] if len(rows) > 50 else None, 'simulated': True}
        return _read(self, actor, 'list_inbox', None, work)

    def receive_event(self, event, *, actor=None):
        if event.get('producer') != 'orders' or event.get('payload', {}).get('operation') != 'confirm_payment':
            return {'id': event['event_id'], 'state': 'ignored', 'version': 1}
        source = _notification_source(self, 'order_confirmation', event['aggregate_id'], actor)
        digest = hashlib.sha256(_canonical(event).encode()).hexdigest()

        def work(tx):
            prior = tx.execute('SELECT * FROM notifications.consumer_inbox WHERE event_id=%s', (event['event_id'],)).fetchone()
            if prior:
                if prior['digest'] != digest:
                    raise CommerceError('conflict', 'Event identity changed')
                return {'id': prior['intent_id'], 'state': 'recorded', 'version': 1}
            intent = _notification_intent(tx, actor, 'order_confirmation', event['aggregate_id'], source['recipient'], 'portfolio-v1', source['variables'], event['event_id'], source['owner_id'])
            tx.execute('INSERT INTO notifications.consumer_inbox VALUES(%s,%s,%s)', (event['event_id'], digest, intent['id']))
            return intent
        return _run(self, actor, 'receive_event', event['aggregate_id'], [event['event_id'], digest], event['event_id'], work)

    def suppress_recipient(self, recipient_ref, reason, *, actor=None, command_id=None):

        def work(tx):
            tx.execute('INSERT INTO notifications.suppressions VALUES(%s,%s) ON CONFLICT(recipient) DO UPDATE SET reason=excluded.reason', (recipient_ref, reason))
            tx.execute("UPDATE notifications.intents SET state='suppressed',version=version+1 WHERE recipient=%s AND state IN ('queued','retry') AND kind NOT IN ('verification','reset','security_notice')", (recipient_ref,))
            return {'id': hashlib.sha256(recipient_ref.encode()).hexdigest(), 'state': 'suppressed', 'version': 1}
        return _run(self, actor, 'suppress_recipient', None, [recipient_ref, reason], command_id, work)

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

def _json(value):
    """psycopg already decodes jsonb, including JSON string scalars."""
    return value

def _notification_intent(tx, actor, kind, aggregate, recipient, version, variables, event_id, owner_id):
    tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('notification:' + kind + ':' + aggregate,))
    previous = tx.execute("SELECT * FROM notifications.intents WHERE event_id=%s OR (%s='order_confirmation' AND kind=%s AND aggregate_id=%s) ORDER BY created_at,id LIMIT 1 FOR UPDATE", (event_id, kind, kind, aggregate)).fetchone()
    if previous:
        if previous['owner_id'] != owner_id or previous['aggregate_id'] != aggregate or previous['kind'] != kind or (previous['recipient'] != recipient) or (_json(previous['variables']) != variables):
            raise CommerceError('conflict', 'Notification source event changed')
        return {'id': previous['id'], 'state': previous['state'], 'version': previous['version']}
    ident = str(uuid.uuid4())
    now = _now(tx)
    tx.execute('INSERT INTO notifications.intents(id,event_id,kind,aggregate_id,recipient,template_version,variables,next_at,created_at,owner_id,workflow_id) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)', (ident, event_id, kind, aggregate, recipient, version, _canonical(variables), now, now, owner_id, actor.get('obligation_id')))
    return {'id': ident, 'state': 'queued', 'version': 1}

def _notification_source(service, kind, aggregate_id, actor):
    if service.source_port is None:
        raise CommerceError('dependency_unavailable', 'Authoritative notification source port required')
    if kind not in ('order_confirmation', 'payment_action', 'payment_failure', 'shipment', 'return', 'refund', 'security_notice'):
        raise CommerceError('invalid_input', 'Unsupported portfolio notification')
    source = service.source_port(kind, aggregate_id, actor=actor)
    if not isinstance(source, dict) or set(source) != {'owner_id', 'recipient', 'variables'} or (not re.fullmatch('[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\\.invalid', source['recipient'])):
        raise CommerceError('invalid_dependency', 'Synthetic source recipient required')
    if any((any((word in key.lower() for word in ('password', 'secret', 'token', 'grant', 'csrf', 'card', 'pan', 'cvc'))) for key in source['variables'])):
        raise CommerceError('invalid_dependency', 'Forbidden notification variable')
    return source

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
