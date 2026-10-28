from __future__ import annotations
import hashlib
import json
import uuid
from .publication import EventPublication
from .subscriptions import EventSubscriptions

MIGRATIONS = [(1, "\nCREATE SCHEMA IF NOT EXISTS events;\nCREATE TABLE IF NOT EXISTS events.deliveries(event_id text NOT NULL,consumer_id text NOT NULL,envelope jsonb NOT NULL,state text NOT NULL DEFAULT 'ready',fence bigint NOT NULL DEFAULT 0,lease_until bigint NOT NULL DEFAULT 0,next_at bigint NOT NULL DEFAULT 0,attempts integer NOT NULL DEFAULT 0,result jsonb,PRIMARY KEY(event_id,consumer_id));\nCREATE TABLE IF NOT EXISTS events.offsets(consumer_id text NOT NULL,aggregate_id text NOT NULL,version bigint NOT NULL,PRIMARY KEY(consumer_id,aggregate_id));\n"), (2, "CREATE TABLE IF NOT EXISTS events.publication_archive(owner text NOT NULL,event_id text NOT NULL,topic text NOT NULL,payload_digest text NOT NULL,payload jsonb NOT NULL,recorded_at bigint NOT NULL,PRIMARY KEY(owner,event_id,topic));\nCREATE TABLE IF NOT EXISTS events.audit_archive(owner text NOT NULL,event_id text NOT NULL,payload_digest text NOT NULL,payload jsonb NOT NULL,receipt_id text UNIQUE NOT NULL,state text NOT NULL CHECK(state='DEMO_RECORDED'),recorded_at bigint NOT NULL,PRIMARY KEY(owner,event_id));")]

class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class EventService(EventPublication, EventSubscriptions):
    MIGRATIONS = MIGRATIONS

    def __init__(self, db, *, authorize=None, profile='local-sandbox'):
        self.owner = 'events'
        _configure(self, db, authorize, profile, publication_ports={}, consumer_ports={}, actor_factory=None, source_port=None)

    def ack_publication(self, owner, event_id, fence, *, actor=None):
        if owner not in self.publication_ports:
            raise CommerceError('dependency_unavailable', 'Owner publication port is not bound')
        return self.publication_ports[owner].ack_publication(event_id, fence, actor=actor)

    def archive_publication(self, owner, event_id, topic, *, actor):
        if self.source_port is None:
            raise CommerceError('dependency_unavailable', 'Supported owner delivery port required')
        actual = self.source_port(owner, actor).inspect_publication(event_id, topic=topic)
        payload = actual['payload']
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()

        def work(tx):
            prior = tx.execute('SELECT * FROM events.publication_archive WHERE owner=%s AND event_id=%s AND topic=%s FOR UPDATE', (owner, event_id, topic)).fetchone()
            if prior and prior['payload_digest'] != digest:
                raise CommerceError('conflict', 'Immutable original publication changed')
            tx.execute('INSERT INTO events.publication_archive VALUES(%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING', (owner, event_id, topic, digest, _canonical(payload), _now(tx)))
            return {'owner': owner, 'event_id': event_id, 'topic': topic, 'payload_digest': digest, 'state': 'DEMO_ARCHIVED'}
        return _read(self, actor, 'archive_publication', event_id, work)

    def bind_publications(self, owner, port):
        if not hasattr(port, 'claim_publications') or not hasattr(port, 'ack_publication'):
            raise CommerceError('invalid_input', 'Supported owner publication port required')
        self.publication_ports[owner] = port

    def claim_publications(self, owner, batch=100, lease=30, *, actor=None):
        if owner not in self.publication_ports:
            raise CommerceError('dependency_unavailable', 'Owner publication port is not bound')
        return self.publication_ports[owner].claim_publications(batch, lease, actor=actor)

    def configure_resource_predicate(self, predicate):
        if not callable(predicate):
            raise CommerceError('invalid_input', 'Trusted resource predicate required')
        self.resource_predicate = predicate

    def dispatch(self, owner, consumer_id, batch=100, *, actor=None):
        result = []
        for claim in self.claim_publications(owner, batch, 30, actor=actor):
            event = claim['event']
            effective = self.actor_factory(event) if self.actor_factory else actor
            try:
                receipt = self.deliver(event, consumer_id, actor=effective)
                self.ack_publication(owner, claim['event_id'], claim['fence'], actor=effective)
                result.append(receipt)
            except Exception:
                self.publication_ports[owner].defer_publication(claim['event_id'], claim['fence'], 'delivery_failed', actor=effective)
        return result

    def initialize(self):
        for index, (version, ddl) in enumerate(self.MIGRATIONS):
            self.db.verify_migration(self.owner, version, ddl, allow_newer=index < len(self.MIGRATIONS) - 1)
        return {'id': self.owner, 'state': 'initialized', 'version': self.MIGRATIONS[-1][0], 'profile': self.profile}

    def inspect_publication(self, event_id, *, actor=None):
        return _inspect_publication(self, event_id, actor)

    def migrate(self):
        for version, ddl in self.MIGRATIONS:
            self.db.migrate(self.owner, version, ddl)
        return {'id': self.owner, 'state': 'migrated', 'version': self.MIGRATIONS[-1][0]}

    def record_audit(self, owner, event_id, *, actor):
        if self.source_port is None:
            raise CommerceError('dependency_unavailable', 'Supported owner delivery port required')
        actual = self.source_port(owner, actor).inspect_audit(event_id)
        payload = actual['payload']
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, 'sme:demo-audit:' + owner + ':' + event_id + ':' + digest))

        def work(tx):
            prior = tx.execute('SELECT * FROM events.audit_archive WHERE owner=%s AND event_id=%s FOR UPDATE', (owner, event_id)).fetchone()
            if prior and prior['payload_digest'] != digest:
                raise CommerceError('conflict', 'Immutable original audit fact changed')
            tx.execute("INSERT INTO events.audit_archive VALUES(%s,%s,%s,%s::jsonb,%s,'DEMO_RECORDED',%s) ON CONFLICT DO NOTHING", (owner, event_id, digest, _canonical(payload), ident, _now(tx)))
            return {'owner': owner, 'event_id': event_id, 'payload_digest': digest, 'receipt_id': ident, 'state': 'DEMO_RECORDED'}
        return _read(self, actor, 'record_audit', event_id, work)

    def safety_snapshot(self, *, actor):

        def work(tx):
            counts = {}
            counts['consumer_deliveries'] = int(_one(tx, "SELECT count(*) n FROM events.deliveries WHERE state<>'done'")['n'])
            return {'owner': self.owner, 'complete': True, 'unresolved': sum(counts.values()), 'counts': counts, 'failures': []}
        return _read(self, actor, 'safety_snapshot', self.owner, work)

    def verify_audit_receipt(self, tx, owner, event_id, payload, receipt):
        if getattr(tx, 'owner', None) != owner or not getattr(tx, 'active', False):
            raise CommerceError('forbidden', 'Active native producer transaction required')
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        row = tx.execute('SELECT owner,event_id,payload_digest,receipt_id,state FROM events.audit_archive WHERE owner=%s AND event_id=%s FOR SHARE', (owner, event_id)).fetchone()
        if not row or dict(row) != receipt or row['payload_digest'] != digest or (row['state'] != 'DEMO_RECORDED'):
            raise CommerceError('invalid_dependency', 'Actual immutable demo audit receipt required')
        return dict(row)

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

def _configure(service, db, authorize, profile, **ports):
    if profile not in ('local-sandbox', 'deterministic-sandbox', 'demo-test', 'portfolio-demo'):
        raise CommerceError('unqualified_binding', 'Only selected deterministic profiles are supported')
    service.db, service.authorize, service.profile = (db, authorize, profile)
    service.resource_predicate = None
    service.receipt_inspectors = {}
    for key, value in ports.items():
        setattr(service, key, value)

def _inspect_publication(service, event_id, actor):

    def work(tx):
        row = _one(tx, 'SELECT payload,state FROM commerce_outbox WHERE owner=%s AND message_key=%s', (service.owner, event_id))
        return {'id': event_id, 'state': row['state'], 'event': _json(row['payload'])}
    return _read(service, actor, 'inspect_publication', event_id, work)

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
