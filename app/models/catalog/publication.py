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

class CatalogPublication:

    def get_public_product(self, slug, *, actor=None):
        if not isinstance(slug, str) or not re.fullmatch('[a-z0-9-]{1,120}', slug):
            raise CommerceError('not_found', 'Product unavailable')

        def work(tx):
            row = tx.execute("SELECT public_snapshot FROM catalog.products WHERE public_snapshot->>'slug'=%s AND state='published'", (slug,)).fetchone()
            if row:
                return _json(row['public_snapshot'])
            redirect = tx.execute("SELECT p.public_snapshot->>'slug' AS slug FROM catalog.redirects r JOIN catalog.products p ON p.id=r.product_id WHERE r.slug=%s AND p.state='published'", (slug,)).fetchone()
            if redirect:
                return {'redirect': redirect['slug'], 'status': 301}
            raise CommerceError('not_found', 'Product unavailable')
        result = _read(self, actor, 'get_public_product', slug, work, True)
        return _public_enrich(self, [result])[0] if 'variants' in result else result

    def list_public_products(self, filters=None, cursor=None, *, actor=None):
        filters = filters or {}
        allowed = {'query', 'limit', 'sort', 'category', 'min_price', 'max_price', 'in_stock'}
        if not isinstance(filters, dict) or set(filters) - allowed:
            raise CommerceError('invalid_input', 'Unsupported catalog filter')
        query = filters.get('query', '')
        category = filters.get('category', '')
        sort = filters.get('sort', 'newest')
        limit = _integer(filters.get('limit', 50), 1, 50)
        if not isinstance(query, str) or len(query) > 200 or (not isinstance(category, str)) or (len(category) > 80) or (sort not in ('newest', 'relevance', 'price_asc', 'price_desc', 'name')):
            raise CommerceError('invalid_input', 'Invalid catalog query')
        for name in ('min_price', 'max_price'):
            if name in filters:
                _integer(filters[name])
        if 'in_stock' in filters and type(filters['in_stock']) is not bool:
            raise CommerceError('invalid_input', 'in_stock must be boolean')
        if filters.get('min_price', 0) > filters.get('max_price', 99999999):
            raise CommerceError('invalid_input', 'Invalid price range')
        fingerprint = hashlib.sha256(_canonical({k: v for k, v in filters.items() if k != 'limit'}).encode()).hexdigest()
        tail = _cursor_decode(cursor, fingerprint)

        def work(tx):
            return [_json(r['public_snapshot']) for r in tx.execute("SELECT public_snapshot FROM catalog.products WHERE state='published' AND (%s='' OR public_snapshot->>'name' ILIKE %s OR public_snapshot->>'description' ILIKE %s) AND (%s='' OR LOWER(public_snapshot->'attributes'->>'category')=LOWER(%s)) ORDER BY id LIMIT 10001", (query, '%' + query + '%', '%' + query + '%', category, category)).fetchall()]
        rows = _read(self, actor, 'list_public_products', None, work, True)
        if len(rows) > 10000:
            raise CommerceError('limit_exceeded', 'Demo catalog exceeds the qualified query bound')
        rows = _public_enrich(self, rows)
        rows = [r for r in rows if filters.get('min_price', 0) <= r['minimum_price_minor'] <= filters.get('max_price', 99999999) and (not filters.get('in_stock') or r['available'] > 0)]

        def key(row):
            value = row['minimum_price_minor'] if sort == 'price_asc' else -row['minimum_price_minor'] if sort == 'price_desc' else row['name'].casefold() if sort == 'name' else -row.get('published_at', 0) if sort == 'newest' else -sum((row['name'].casefold().count(word) for word in query.casefold().split()))
            return [value, row['id']]
        rows.sort(key=key)
        if tail is not None:
            rows = [r for r in rows if key(r) > tail]
        items = rows[:limit]
        return {'items': items, 'next_cursor': _cursor_encode(key(items[-1]), fingerprint) if len(rows) > limit else None, 'sort': sort}

    def publish(self, product_id, expected_revision, price_revision=None, publication_at=None, *, actor=None, command_id=None):
        _integer(expected_revision, 1)
        product = self.get_merchant_product(product_id, actor=actor)
        variants = [v for v in product['variants'] if not v['retired']]
        if not variants or self.pricing_port is None:
            raise CommerceError('ineligible', 'Active variants and current price owner are required')
        prices = self.pricing_port([v['id'] for v in variants])
        for variant in variants:
            price = prices.get(variant['id'])
            if not isinstance(price, dict):
                raise CommerceError('ineligible', 'Every variant requires a current price')
            _integer(price.get('unit_minor'))
            _integer(price.get('version'), 1)
        if product['attributes'].get('tax_code') != 'demo_physical_standard' or not product['attributes'].get('category'):
            raise CommerceError('ineligible', 'Category and demo tax classification are required')
        if not product['media'] or self.media_port is None:
            raise CommerceError('ineligible', 'A curated product image is required')
        media = []
        for record in product['media']:
            evidence = _json(record['evidence'])
            current = self.media_port(evidence['id'])
            _asset(current, evidence['id'], record['alt_text'])
            if current['sha256'] != evidence['sha256']:
                raise CommerceError('conflict', 'Asset registry changed')
            media.append({**current, 'alt_text': record['alt_text']})

        def work(tx):
            row = _one(tx, 'SELECT * FROM catalog.products WHERE id=%s FOR UPDATE', (product_id,))
            _version(row, expected_revision)
            if product['version'] != row['version']:
                raise CommerceError('version_conflict', 'Product changed while validating publication')
            now = _now(tx)
            if publication_at is not None and publication_at > now:
                raise CommerceError('not_supported', 'Scheduled publication is outside this demo')
            snapshot = {'id': product_id, 'slug': row['slug'], 'name': row['name'], 'description': _json(row['description']), 'attributes': _json(row['attributes']), 'variants': [{**{k: v[k] for k in ('id', 'sku', 'options', 'weight_g', 'dimensions_mm', 'version')}, 'unit_minor': prices[v['id']]['unit_minor'], 'price_revision': prices[v['id']]['version']} for v in variants], 'media': media, 'version': row['version'] + 1, 'state': 'published', 'published_at': now}
            tx.execute('INSERT INTO catalog.publications VALUES(%s,%s,%s,%s::jsonb,%s)', (str(uuid.uuid4()), product_id, snapshot['version'], _canonical(snapshot), now))
            tx.execute("UPDATE catalog.products SET state='published',draft_state='published',version=%s,public_snapshot=%s::jsonb,published_at=%s WHERE id=%s", (snapshot['version'], _canonical(snapshot), now, product_id))
            return snapshot
        return _run(self, actor, 'publish', product_id, [expected_revision, price_revision, publication_at], command_id, work)

    def search_public_catalog(self, query, filters=None, sort='relevance', cursor=None, *, actor=None):
        return self.list_public_products({**(filters or {}), 'query': query, 'sort': sort}, cursor, actor=actor)

    def unpublish(self, product_id, expected_revision, *, actor=None, command_id=None):
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM catalog.products WHERE id=%s FOR UPDATE', (product_id,))
            _version(row, expected_revision)
            return dict(_one(tx, "UPDATE catalog.products SET state='draft',draft_state='changed',version=version+1 WHERE id=%s RETURNING id,version,state,draft_state", (product_id,)))
        return _run(self, actor, 'unpublish', product_id, [expected_revision], command_id, work)

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

def _public_enrich(service, products):
    ids = [v['id'] for product in products for v in product['variants']]
    prices = service.pricing_port(ids) if service.pricing_port else {}
    stock = service.inventory_port(ids) if service.inventory_port else {}
    result = []
    for product in products:
        product = {**product, 'variants': [{**v, 'unit_minor': prices.get(v['id'], {}).get('unit_minor', v.get('unit_minor')), 'price_revision': prices.get(v['id'], {}).get('version', v.get('price_revision')), 'available': stock.get(v['id'], {}).get('available', 0)} for v in product['variants']]}
        product['minimum_price_minor'] = min((v['unit_minor'] for v in product['variants'] if type(v.get('unit_minor')) is int), default=0)
        product['available'] = sum((v['available'] for v in product['variants']))
        result.append(product)
    return result

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
