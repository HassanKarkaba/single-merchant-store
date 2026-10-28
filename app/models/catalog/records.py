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

class CatalogRecords:

    def attach_media(self, product_id, object_id, alt_text, position, expected_revision, *, actor=None, command_id=None):
        if self.media_port is None:
            raise CommerceError('dependency_unavailable', 'Curated asset registry is not bound')
        evidence = self.media_port(object_id)
        _asset(evidence, object_id, alt_text)
        _integer(position, 0, 9)
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM catalog.products WHERE id=%s FOR UPDATE', (product_id,))
            _version(row, expected_revision)
            ident = str(uuid.uuid5(uuid.NAMESPACE_URL, 'catalog-media:' + product_id + ':' + str(position)))
            tx.execute('INSERT INTO catalog.media VALUES(%s,%s,%s::jsonb,%s,%s) ON CONFLICT(id) DO UPDATE SET evidence=excluded.evidence,alt_text=excluded.alt_text', (ident, product_id, _canonical(evidence), alt_text.strip(), position))
            result = _one(tx, "UPDATE catalog.products SET draft_state='changed',version=version+1 WHERE id=%s RETURNING version,state,draft_state", (product_id,))
            return {'id': ident, 'asset_id': object_id, **result}
        return _run(self, actor, 'attach_media', product_id, [object_id, alt_text, position, expected_revision], command_id, work)

    def create_product(self, name, description, slug, attributes=None, *, actor=None, command_id=None):
        attributes = _product_values(name, description, slug, attributes or {})

        def work(tx):
            if tx.execute('SELECT 1 FROM catalog.redirects WHERE slug=%s', (slug,)).fetchone():
                raise CommerceError('conflict', 'Slug is permanently reserved')
            ident = str(uuid.uuid4())
            return dict(_one(tx, 'INSERT INTO catalog.products(id,slug,name,description,attributes) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING id,slug,name,attributes,version,state,draft_state', (ident, slug, name.strip(), _canonical(description), _canonical(attributes))))
        return _run(self, actor, 'create_product', None, [name, description, slug, attributes], command_id, work)

    def get_merchant_product(self, product_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM catalog.products WHERE id=%s', (product_id,))
            row['variants'] = [dict(v) for v in tx.execute('SELECT * FROM catalog.variants WHERE product_id=%s ORDER BY id', (product_id,)).fetchall()]
            row['media'] = [dict(v) for v in tx.execute('SELECT * FROM catalog.media WHERE product_id=%s ORDER BY position,id', (product_id,)).fetchall()]
            return row
        return _read(self, actor, 'get_merchant_product', product_id, work)

    def get_variants(self, variant_ids, *, published_only=False, actor=None):
        if not isinstance(variant_ids, list) or len(variant_ids) > 50000 or any((not isinstance(v, str) for v in variant_ids)):
            raise CommerceError('invalid_input', 'Bounded variant list required')

        def work(tx):
            rows = tx.execute('SELECT v.*,p.state AS product_state,p.name,p.attributes,p.public_snapshot FROM catalog.variants v JOIN catalog.products p ON p.id=v.product_id WHERE v.id=ANY(%s) ORDER BY v.id', (variant_ids,)).fetchall()
            result = {}
            for row in rows:
                if published_only:
                    snapshot = _json(row['public_snapshot']) if row['public_snapshot'] else None
                    public = next((v for v in snapshot.get('variants', []) if v['id'] == row['id']), None) if snapshot and row['product_state'] == 'published' else None
                    if not public or row['retired']:
                        continue
                    result[row['id']] = {**public, 'product_id': row['product_id'], 'name': snapshot['name'], 'attributes': snapshot['attributes'], 'product_revision': snapshot['version'], 'state': 'published'}
                else:
                    result[row['id']] = {k: v for k, v in dict(row).items() if k != 'public_snapshot'}
            return result
        return _read(self, actor, 'get_variants', None, work, public=published_only)

    def list_merchant_products(self, filters=None, cursor=None, *, actor=None):
        filters = filters or {}
        if set(filters) - {'query', 'state', 'category', 'limit'}:
            raise CommerceError('invalid_input', 'Unsupported merchant filter')
        query = filters.get('query', '')
        state = filters.get('state', '')
        category = filters.get('category', '')
        limit = _integer(filters.get('limit', 50), 1, 50)
        if not isinstance(query, str) or len(query) > 200 or state not in ('', 'draft', 'published') or (not isinstance(category, str)):
            raise CommerceError('invalid_input', 'Invalid merchant filter')
        fingerprint = hashlib.sha256(_canonical({k: v for k, v in filters.items() if k != 'limit'}).encode()).hexdigest()
        tail = _cursor_decode(cursor, fingerprint) or ''

        def work(tx):
            rows = [dict(r) for r in tx.execute("SELECT id,slug,name,description,attributes,version,state,draft_state FROM catalog.products WHERE id>%s AND (%s='' OR name ILIKE %s) AND (%s='' OR state=%s) AND (%s='' OR attributes->>'category'=%s) ORDER BY id LIMIT %s", (tail, query, '%' + query + '%', state, state, category, category, limit + 1)).fetchall()]
            items = rows[:limit]
            ids = [r['id'] for r in items]
            parts = tx.execute("SELECT 'variant' AS kind,product_id,to_jsonb(v) AS value FROM catalog.variants v WHERE product_id=ANY(%s) UNION ALL SELECT 'media' AS kind,product_id,to_jsonb(m) AS value FROM catalog.media m WHERE product_id=ANY(%s)", (ids, ids)).fetchall()
            for item in items:
                item['variants'] = [r['value'] for r in parts if r['kind'] == 'variant' and r['product_id'] == item['id']]
                item['media'] = [r['value'] for r in parts if r['kind'] == 'media' and r['product_id'] == item['id']]
            return {'items': items, 'next_cursor': _cursor_encode(items[-1]['id'], fingerprint) if len(rows) > limit else None}
        return _read(self, actor, 'list_merchant_products', None, work)

    def retire_variant(self, variant_id, expected_revision, *, actor=None, command_id=None):

        def work(tx):
            variant = _one(tx, 'SELECT * FROM catalog.variants WHERE id=%s FOR UPDATE', (variant_id,))
            row = _one(tx, 'SELECT * FROM catalog.products WHERE id=%s FOR UPDATE', (variant['product_id'],))
            _version(row, expected_revision)
            tx.execute('UPDATE catalog.variants SET retired=true WHERE id=%s', (variant_id,))
            return dict(_one(tx, "UPDATE catalog.products SET state='draft',version=version+1 WHERE id=%s RETURNING id,version,state", (variant['product_id'],)))
        return _run(self, actor, 'retire_variant', variant_id, [expected_revision], command_id, work)

    def set_collection(self, slug, product_ids, *, actor=None, command_id=None):
        if not re.fullmatch('[a-z0-9-]+', slug or '') or len(product_ids) > 1000:
            raise CommerceError('invalid_input', 'Invalid collection')

        def work(tx):
            tx.execute('DELETE FROM catalog.collections WHERE slug=%s', (slug,))
            for ident in sorted(set(product_ids)):
                tx.execute('INSERT INTO catalog.collections VALUES(%s,%s)', (slug, ident))
            return {'id': slug, 'version': 1, 'state': 'active', 'product_ids': sorted(set(product_ids))}
        return _run(self, actor, 'set_collection', slug, product_ids, command_id, work)

    def update_product(self, product_id, changes, expected_revision, *, actor=None, command_id=None):
        if not isinstance(changes, dict) or not changes or set(changes) - {'name', 'description', 'slug', 'attributes', 'category', 'tax_code'}:
            raise CommerceError('invalid_input', 'Unsupported product change')
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM catalog.products WHERE id=%s FOR UPDATE', (product_id,))
            _version(row, expected_revision)
            attrs = {**_json(row['attributes']), **changes.get('attributes', {})}
            for key in ('category', 'tax_code'):
                if key in changes:
                    attrs[key] = changes[key]
            name = changes.get('name', row['name'])
            description = changes.get('description', _json(row['description']))
            slug = changes.get('slug', row['slug'])
            _product_values(name, description, slug, attrs)
            if slug != row['slug']:
                collision = tx.execute('SELECT 1 FROM catalog.products WHERE slug=%s AND id<>%s UNION ALL SELECT 1 FROM catalog.redirects WHERE slug=%s AND product_id<>%s', (slug, product_id, slug, product_id)).fetchone()
                if collision:
                    raise CommerceError('conflict', 'Slug already exists')
                tx.execute('INSERT INTO catalog.redirects VALUES(%s,%s) ON CONFLICT DO NOTHING', (row['slug'], product_id))
            return dict(_one(tx, "UPDATE catalog.products SET name=%s,description=%s::jsonb,slug=%s,attributes=%s::jsonb,draft_state='changed',version=version+1 WHERE id=%s RETURNING id,name,description,slug,attributes,state,draft_state,version", (name.strip(), _canonical(description), slug, _canonical(attrs), product_id)))
        return _run(self, actor, 'update_product', product_id, [changes, expected_revision], command_id, work)

    def upsert_variant(self, product_id, variant_id=None, sku=None, options=None, weight_g=None, dimensions_mm=None, expected_revision=None, *, variant_revision=None, actor=None, command_id=None):
        if not isinstance(sku, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', sku) or (not isinstance(options, dict)) or (len(options) > 10) or any((not isinstance(k, str) or not isinstance(v, str) or (not 1 <= len(k) <= 50) or (not 1 <= len(v) <= 80) for k, v in options.items())):
            raise CommerceError('invalid_input', 'Valid SKU and option values required')
        _integer(weight_g, 1, 50000)
        if not isinstance(dimensions_mm, list) or len(dimensions_mm) != 3:
            raise CommerceError('invalid_input', 'Three positive dimensions required')
        for value in dimensions_mm:
            _integer(value, 1, 2000)
        _integer(expected_revision, 1)

        def work(tx):
            product = _one(tx, 'SELECT * FROM catalog.products WHERE id=%s FOR UPDATE', (product_id,))
            _version(product, expected_revision)
            ident = variant_id or str(uuid.uuid4())
            old = tx.execute('SELECT * FROM catalog.variants WHERE id=%s FOR UPDATE', (ident,)).fetchone()
            if old and old['product_id'] != product_id:
                raise CommerceError('not_found', 'Variant unavailable')
            if old and variant_revision is not None:
                _version(old, variant_revision)
            if not old and tx.execute('SELECT count(*) AS n FROM catalog.variants WHERE product_id=%s', (product_id,)).fetchone()['n'] >= 10:
                raise CommerceError('limit_exceeded', 'Demo products allow ten variants')
            tx.execute('INSERT INTO catalog.variants(id,product_id,sku,options,weight_g,dimensions_mm) VALUES(%s,%s,%s,%s::jsonb,%s,%s::jsonb) ON CONFLICT(id) DO UPDATE SET sku=excluded.sku,options=excluded.options,weight_g=excluded.weight_g,dimensions_mm=excluded.dimensions_mm,version=catalog.variants.version+1', (ident, product_id, sku, _canonical(options), weight_g, _canonical(dimensions_mm)))
            product = _one(tx, "UPDATE catalog.products SET draft_state='changed',version=version+1 WHERE id=%s RETURNING version,state,draft_state", (product_id,))
            variant = _one(tx, 'SELECT version FROM catalog.variants WHERE id=%s', (ident,))
            return {'id': ident, 'product_id': product_id, 'sku': sku, 'options': options, 'weight_g': weight_g, 'dimensions_mm': dimensions_mm, 'variant_revision': variant['version'], **product}
        return _run(self, actor, 'upsert_variant', product_id, [variant_id, sku, options, weight_g, dimensions_mm, expected_revision, variant_revision], command_id, work)

def _asset(evidence, asset_id, alt_text):
    if not isinstance(evidence, dict) or evidence.get('id') != asset_id or (not re.fullmatch('[0-9a-f]{64}', evidence.get('sha256', ''))) or (evidence.get('mime') not in ('image/svg+xml', 'image/png', 'image/jpeg', 'image/webp')) or (not isinstance(evidence.get('width'), int)) or (not isinstance(evidence.get('height'), int)) or (evidence['width'] <= 0) or (evidence['height'] <= 0) or (not str(evidence.get('url', '')).startswith('/assets/')):
        raise CommerceError('invalid_dependency', 'Invalid curated asset registry receipt')
    if not isinstance(alt_text, str) or not 1 <= len(alt_text.strip()) <= 300:
        raise CommerceError('invalid_input', 'Image alternative text required')

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

def _product_values(name, description, slug, attributes):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 or (not isinstance(description, str)) or (len(description.encode('utf-8')) > 20000) or (not isinstance(slug, str)) or (len(slug) > 120) or (not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*', slug)):
        raise CommerceError('invalid_input', 'Valid name, plain-text description and lowercase slug required')
    if not isinstance(attributes, dict) or set(attributes) - {'category', 'tax_code', 'synthetic', 'tags'}:
        raise CommerceError('invalid_input', 'Unsupported product attributes')
    if 'category' in attributes and (not isinstance(attributes['category'], str) or not 1 <= len(attributes['category']) <= 80):
        raise CommerceError('invalid_input', 'Bounded category required')
    if 'tax_code' in attributes and attributes['tax_code'] != 'demo_physical_standard':
        raise CommerceError('invalid_input', 'Unsupported demo tax classification')
    return attributes

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

def _version(row, expected):
    if expected is not None and row['version'] != _integer(expected, 1):
        raise CommerceError('version_conflict', 'Resource changed; reload before editing')
