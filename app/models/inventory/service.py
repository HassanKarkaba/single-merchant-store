from __future__ import annotations
import uuid
from .stock_ledger import StockLedger
from .reservations import Reservations

MIGRATIONS = [(1, '\nCREATE SCHEMA IF NOT EXISTS inventory;\nCREATE TABLE IF NOT EXISTS inventory.stock_items(variant_id text PRIMARY KEY,on_hand bigint NOT NULL CHECK(on_hand>=0),reserved bigint NOT NULL DEFAULT 0 CHECK(reserved>=0),allocated bigint NOT NULL DEFAULT 0 CHECK(allocated>=0),version bigint NOT NULL DEFAULT 1,CHECK(on_hand>=reserved+allocated));\nCREATE TABLE IF NOT EXISTS inventory.movements(id text PRIMARY KEY,variant_id text NOT NULL,delta bigint NOT NULL,reason text NOT NULL,created_at bigint NOT NULL);\nCREATE TABLE IF NOT EXISTS inventory.reservations(id text PRIMARY KEY,checkout_id text UNIQUE NOT NULL,lines jsonb NOT NULL,expires_at bigint NOT NULL,state text NOT NULL,version bigint NOT NULL DEFAULT 1);\nCREATE TABLE IF NOT EXISTS inventory.allocations(id text PRIMARY KEY,order_id text UNIQUE NOT NULL,reservation_id text UNIQUE NOT NULL,lines jsonb NOT NULL,remaining jsonb NOT NULL,state text NOT NULL,version bigint NOT NULL DEFAULT 1);\nCREATE TABLE IF NOT EXISTS inventory.shipments(id text PRIMARY KEY,allocation_id text NOT NULL,quantities jsonb NOT NULL);\n'), (2, '\nALTER TABLE inventory.movements ADD COLUMN IF NOT EXISTS actor_id text;\nALTER TABLE inventory.movements ADD COLUMN IF NOT EXISTS reference text;\nCREATE INDEX IF NOT EXISTS inventory_movement_page ON inventory.movements(variant_id,created_at,id);\n\nALTER TABLE inventory.reservations ADD COLUMN IF NOT EXISTS owner_id text;\nALTER TABLE inventory.allocations ADD COLUMN IF NOT EXISTS owner_id text;\n')]

class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class InventoryService(StockLedger, Reservations):
    MIGRATIONS = MIGRATIONS

    def __init__(self, db, *, authorize=None, profile='local-sandbox', variant_port=None, order_port=None, checkout_port=None):
        self.owner = 'inventory'
        _configure(self, db, authorize, profile, variant_port=variant_port, order_port=order_port, checkout_port=checkout_port)

    def ack_publication(self, event_id, fence, *, actor=None):
        return _ack_publication(self, event_id, fence, actor)

    def claim_publications(self, batch=100, lease=30, *, actor=None):
        return _claim_publications(self, batch, lease, actor)

    def configure_resource_predicate(self, predicate):
        if not callable(predicate):
            raise CommerceError('invalid_input', 'Trusted resource predicate required')
        self.resource_predicate = predicate

    def defer_publication(self, event_id, fence, reason, *, actor=None):
        return _ack_publication(self, event_id, fence, actor, True, reason)

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

    def safety_snapshot(self, *, actor):

        def work(tx):
            counts = {}
            counts['reservations'] = int(_one(tx, "SELECT count(*) n FROM inventory.reservations WHERE state='reserved'")['n'])
            counts['allocations'] = int(_one(tx, "SELECT count(*) n FROM inventory.allocations WHERE state='allocated' AND EXISTS(SELECT 1 FROM jsonb_each_text(remaining) AS q WHERE q.value::bigint>0)")['n'])
            return {'owner': self.owner, 'complete': True, 'unresolved': sum(counts.values()), 'counts': counts, 'failures': []}
        return _read(self, actor, 'safety_snapshot', self.owner, work)

def _ack_publication(service, event_id, fence, actor, defer=False, reason=None):

    def work(tx):
        row = _one(tx, 'SELECT * FROM commerce_outbox WHERE owner=%s AND message_key=%s FOR UPDATE', (service.owner, event_id))
        if row['state'] == 'DELIVERED' and (not defer):
            return {'id': event_id, 'state': 'delivered'}
        if row['state'] != 'CLAIMED' or row['lease_id'] != fence or tx.execute('SELECT %s>clock_timestamp() AS active', (row['lease_until'],)).fetchone()['active'] is not True:
            raise CommerceError('conflict', 'Current publication lease required')
        state = 'DEAD' if defer and row['attempts'] >= 8 else 'PENDING' if defer else 'DELIVERED'
        tx.execute("UPDATE commerce_outbox SET state=%s,available_at=clock_timestamp()+interval '1 second' WHERE id=%s", (state, row['id']))
        return {'id': event_id, 'state': state.lower()}
    return _read(service, actor, 'defer_publication' if defer else 'ack_publication', event_id, work)

def _claim_publications(service, batch, lease, actor):
    _integer(batch, 1, 100)
    _integer(lease, 1, 30)

    def work(tx):
        rows = tx.execute("SELECT * FROM commerce_outbox WHERE owner=%s AND (state IN ('PENDING','UNKNOWN') OR (state='CLAIMED' AND lease_until<=clock_timestamp())) AND available_at<=clock_timestamp() ORDER BY available_at,id LIMIT %s FOR UPDATE SKIP LOCKED", (service.owner, batch)).fetchall()
        result = []
        for row in rows:
            if row['attempts'] >= 8:
                tx.execute("UPDATE commerce_outbox SET state='DEAD' WHERE id=%s", (row['id'],))
                continue
            fence = str(uuid.uuid4())
            tx.execute("UPDATE commerce_outbox SET state='CLAIMED',lease_id=%s,lease_until=clock_timestamp()+(%s*interval '1 second'),attempts=attempts+1 WHERE id=%s", (fence, lease, row['id']))
            result.append({'event': row['payload'], 'event_id': row['message_key'], 'fence': fence})
        return result
    return _read(service, actor, 'claim_publications', service.owner, work)

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

def _integer(value, minimum=0, maximum=99999999):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CommerceError('invalid_input', 'Expected a bounded integer')
    return value

def _json(value):
    """psycopg already decodes jsonb, including JSON string scalars."""
    return value

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
