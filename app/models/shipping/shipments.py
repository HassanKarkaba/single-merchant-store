from __future__ import annotations
import hashlib
import json
import re
import uuid


class CommerceError(ValueError):

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class Shipments:

    def cancel_units(self, order_id, unit_ids, reason, expected_revision=None, *, actor=None, command_id=None):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300 or (not unit_ids) or (len(unit_ids) != len(set(unit_ids))):
            raise CommerceError('invalid_input', 'Distinct units and cancellation reason required')
        order = self.order_port(order_id, actor=actor)
        valid = {u['unit_id'] for l in order['lines'] for u in l.get('units', [])}
        if not set(unit_ids) <= valid:
            raise CommerceError('ineligible', 'Foreign order units')

        def work(tx):
            arbiter = _arbiter(tx, order_id, expected_revision)
            for unit in sorted(unit_ids):
                row = tx.execute('SELECT * FROM shipping.claims WHERE order_id=%s AND unit_id=%s FOR UPDATE', (order_id, unit)).fetchone()
                if row and row['state'] in ('shipped', 'handover_reserved'):
                    raise CommerceError('ineligible', 'Handover already owns this unit')
                tx.execute("INSERT INTO shipping.claims(order_id,unit_id,shipment_id,state) VALUES(%s,%s,NULL,'canceled') ON CONFLICT(order_id,unit_id) DO UPDATE SET state='canceled'", (order_id, unit))
            tx.execute('UPDATE shipping.arbiters SET version=version+1 WHERE order_id=%s', (order_id,))
            return {'id': str(uuid.uuid4()), 'order_id': order_id, 'state': 'cancel_acknowledged', 'version': arbiter['version'] + 1, 'unit_ids': sorted(unit_ids), 'reason': reason}
        return _run(self, actor, 'cancel_units', order_id, [unit_ids, reason, expected_revision], command_id, work)

    def claim_units(self, order_id, unit_ids, *, actor=None, command_id=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Orders port is required')
        order = self.order_port(order_id, actor=actor)
        all_units = {u['unit_id'] for l in order['lines'] for u in l.get('units', [])}
        if order.get('payment_state') != 'paid' or not unit_ids or len(unit_ids) != len(set(unit_ids)) or (not set(unit_ids) <= all_units) or set(unit_ids) & set(order.get('canceled_unit_ids', [])):
            raise CommerceError('ineligible', 'Paid eligible original units required')

        def work(tx):
            arbiter = _arbiter(tx, order_id)
            claim_id = str(uuid.uuid4())
            for unit in sorted(unit_ids):
                old = tx.execute('SELECT * FROM shipping.claims WHERE order_id=%s AND unit_id=%s FOR UPDATE', (order_id, unit)).fetchone()
                if old and old['state'] != 'released':
                    raise CommerceError('conflict', 'Unit already claimed')
                tx.execute("INSERT INTO shipping.claims(order_id,unit_id,shipment_id,state,claim_id) VALUES(%s,%s,NULL,'claimed',%s) ON CONFLICT(order_id,unit_id) DO UPDATE SET state='claimed',shipment_id=NULL,claim_id=excluded.claim_id", (order_id, unit, claim_id))
            tx.execute('UPDATE shipping.arbiters SET version=version+1 WHERE order_id=%s', (order_id,))
            return {'id': claim_id, 'order_id': order_id, 'version': arbiter['version'] + 1, 'state': 'claimed', 'unit_ids': sorted(unit_ids)}
        return _run(self, actor, 'claim_units', order_id, [unit_ids], command_id, work)

    def confirm_handover(self, shipment_id, evidence, *, actor=None, command_id=None):
        if evidence != 'demo_handover':
            raise CommerceError('invalid_input', 'Explicit simulated handover confirmation required')

        def work(tx):
            row = _one(tx, 'SELECT * FROM shipping.shipments WHERE id=%s FOR UPDATE', (shipment_id,))
            _arbiter(tx, row['order_id'])
            if row['state'] not in ('handover_reserved', 'handed_over'):
                raise CommerceError('ineligible', 'Durable handover reservation required')
            if row['state'] != 'handed_over':
                tx.execute("UPDATE shipping.shipments SET state='handed_over',handed_at=%s,version=version+1 WHERE id=%s", (_now(tx), shipment_id))
                tx.execute("UPDATE shipping.claims SET state='shipped' WHERE shipment_id=%s", (shipment_id,))
                tx.execute('UPDATE shipping.arbiters SET version=version+1 WHERE order_id=%s', (row['order_id'],))
            return dict(_one(tx, 'SELECT id,order_id,state,version,units AS unit_ids,quantities,handed_at FROM shipping.shipments WHERE id=%s', (shipment_id,)))
        return _run(self, actor, 'confirm_handover', shipment_id, [evidence], command_id, work)

    def create_shipment(self, order_id, lines, package, service_quote_id, *, allocation_id=None, claim_receipt=None, actor=None, command_id=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Orders port is required')
        order = self.order_port(order_id, actor=actor)
        if order.get('payment_state') != 'paid':
            raise CommerceError('ineligible', 'Paid order required')
        units = _unit_ids(order, lines)
        quantities = {l['variant_id']: l['quantity'] for l in lines}
        offer = self.get_offer(service_quote_id, actor=actor)
        authoritative = {line['variant_id']: {**line, 'id': line['variant_id']} for line in order['lines']}
        parcel = _parcel([{**authoritative[k], 'variant_id': k, 'quantity': v} for k, v in quantities.items()])
        if package != parcel or offer['package'] != parcel:
            raise CommerceError('invalid_input', 'Parcel and selected offer must match original selected units')

        def work(tx):
            _arbiter(tx, order_id)
            if offer['expires_at'] <= _now(tx):
                raise CommerceError('expired', 'Parcel offer expired')
            ident = str(uuid.uuid4())
            claim_id = claim_receipt.get('id') if claim_receipt else ident
            for unit in units:
                old = tx.execute('SELECT * FROM shipping.claims WHERE order_id=%s AND unit_id=%s FOR UPDATE', (order_id, unit)).fetchone()
                if old and (old['state'] != 'claimed' or old['shipment_id'] is not None or old['claim_id'] != claim_id):
                    raise CommerceError('conflict', 'Unit is already assigned')
                tx.execute("INSERT INTO shipping.claims(order_id,unit_id,shipment_id,state,claim_id) VALUES(%s,%s,%s,'claimed',%s) ON CONFLICT(order_id,unit_id) DO UPDATE SET shipment_id=excluded.shipment_id", (order_id, unit, ident, claim_id))
            snapshot = {'package': parcel, 'rate': offer, 'allocation_id': allocation_id, 'lines': lines, 'label_effect_key': str(uuid.uuid5(uuid.UUID(ident), 'label'))}
            tx.execute('INSERT INTO shipping.shipments(id,order_id,owner_id,snapshot,units,quantities) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)', (ident, order_id, order['owner_id'], _canonical(snapshot), _canonical(units), _canonical(quantities)))
            tx.execute('UPDATE shipping.arbiters SET version=version+1 WHERE order_id=%s', (order_id,))
            return {'id': ident, 'order_id': order_id, 'allocation_id': allocation_id, 'state': 'created', 'version': 1, 'quantities': quantities, 'unit_ids': units, 'package': parcel}
        return _run(self, actor, 'create_shipment', order_id, [lines, package, service_quote_id, allocation_id, claim_receipt.get('id') if claim_receipt else None], command_id, work)

    def get_label(self, shipment_id, *, actor=None):
        shipment = self.get_shipment(shipment_id, actor=actor)
        if shipment['label_state'] != 'purchased':
            raise CommerceError('not_found', 'Label unavailable')
        return {'id': shipment_id, 'state': 'available', 'mime': 'text/plain; charset=utf-8', 'content': 'SIMULATED LABEL — NO POSTAGE\nParcel ' + shipment_id + '\nTracking ' + _json(shipment['snapshot'])['label']['tracking_code'] + '\n'}

    def get_shipment(self, shipment_id, *, actor=None):

        def work(tx):
            row = _one(tx, 'SELECT * FROM shipping.shipments WHERE id=%s', (shipment_id,))
            _ownership(row, actor)
            row['unit_ids'] = _json(row['units'])
            row['allocation_id'] = _json(row['snapshot']).get('allocation_id')
            return row
        return _read(self, actor, 'get_shipment', shipment_id, work)

    def get_unit_status(self, order_id, *, actor=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Orders port is required')
        self.order_port(order_id, actor=actor)

        def work(tx):
            row = tx.execute('SELECT version FROM shipping.arbiters WHERE order_id=%s', (order_id,)).fetchone()
            return {'id': order_id, 'version': row['version'] if row else 1, 'state': 'ready', 'claims': [dict(r) for r in tx.execute('SELECT * FROM shipping.claims WHERE order_id=%s ORDER BY unit_id', (order_id,)).fetchall()]}
        return _read(self, actor, 'get_unit_status', order_id, work)

    def inspect_cancel(self, order_id, unit_ids, *, actor=None):

        def work(tx):
            rows = tx.execute('SELECT unit_id,state FROM shipping.claims WHERE order_id=%s AND unit_id=ANY(%s)', (order_id, unit_ids)).fetchall()
            if len(rows) != len(unit_ids) or any((r['state'] != 'canceled' for r in rows)):
                raise CommerceError('ineligible', 'A current cancellation fence is required')
            return {'id': order_id, 'order_id': order_id, 'unit_ids': sorted(unit_ids), 'state': 'cancel_acknowledged', 'version': 1}
        return _read(self, actor, 'inspect_cancel', order_id, work)

    def inspect_receipt(self, evidence, *, actor=None):
        if evidence.get('state') == 'cancel_acknowledged' or evidence.get('kind') == 'cancel':
            return self.inspect_cancel(evidence['order_id'], evidence['unit_ids'], actor=actor)
        return self.get_shipment(evidence.get('id', evidence.get('shipment_id')), actor=actor)

    def list_fulfillment_queue(self, *, actor=None):
        return _read(self, actor, 'list_fulfillment_queue', None, lambda tx: {'items': [dict(r) for r in tx.execute("SELECT * FROM shipping.shipments WHERE state<>'handed_over' ORDER BY id LIMIT 100").fetchall()]})

    def list_order_shipments(self, order_id, *, actor=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Orders owner port required')
        self.order_port(order_id, actor=actor)
        return _read(self, actor, 'list_order_shipments', order_id, lambda tx: {'items': [{**dict(r), 'unit_ids': r['units'], 'allocation_id': r['snapshot'].get('allocation_id')} for r in tx.execute('SELECT * FROM shipping.shipments WHERE order_id=%s ORDER BY id', (order_id,)).fetchall()]})

    def purchase_label(self, shipment_id, operation_key, *, actor=None):
        if self.provider is None:
            raise CommerceError('dependency_unavailable', 'Simulator port is required')
        shipment = self.get_shipment(shipment_id, actor=actor)
        snapshot = _json(shipment['snapshot'])
        key = snapshot['label_effect_key']

        def intent(tx):
            row = _one(tx, 'SELECT * FROM shipping.shipments WHERE id=%s FOR UPDATE', (shipment_id,))
            if row['label_state'] == 'purchased':
                return {'id': shipment_id, 'state': row['state'], 'label_state': 'purchased', 'version': row['version']}
            return dict(_one(tx, "UPDATE shipping.shipments SET label_state='requested',version=version+1 WHERE id=%s RETURNING id,state,label_state,version", (shipment_id,)))
        _run(self, actor, 'label_intent', shipment_id, [key], str(uuid.uuid5(uuid.UUID(key), 'intent')), intent)
        try:
            obj = self.provider.inspect_effect('easypost', str(uuid.uuid5(uuid.UUID(key), 'create')), actor=actor)
            if obj is None:
                obj = self.provider.invoke('easypost', 'create_shipment', {'shipment_id': shipment_id, 'order_id': shipment['order_id'], 'amount_minor': snapshot['rate']['merchant_cost_minor'], 'currency': 'USD'}, str(uuid.uuid5(uuid.UUID(key), 'create')), actor=actor)
            result = self.provider.inspect_effect('easypost', key, actor=actor)
            if result is None:
                result = self.provider.invoke('easypost', 'buy_label', {'object_id': obj['id'], 'shipment_id': shipment_id, 'order_id': shipment['order_id']}, key, actor=actor)
        except TimeoutError:
            return _run(self, actor, 'label_unknown', shipment_id, [key], str(uuid.uuid5(uuid.UUID(key), 'unknown')), lambda tx: dict(_one(tx, "UPDATE shipping.shipments SET label_state='unknown',version=version+1 WHERE id=%s RETURNING id,state,label_state,version", (shipment_id,))))

        def settle(tx):
            current = _one(tx, 'SELECT * FROM shipping.shipments WHERE id=%s FOR UPDATE', (shipment_id,))
            data = _json(current['snapshot'])
            data['label'] = {'simulated': True, 'tracking_code': result['postage']['tracking_code'], 'download_ref': shipment_id}
            return dict(_one(tx, "UPDATE shipping.shipments SET provider_id=%s,label_state='purchased',snapshot=%s::jsonb,version=version+1 WHERE id=%s RETURNING id,order_id,state,label_state,version", (obj['id'], _canonical(data), shipment_id)))
        return _run(self, actor, 'label_purchased', shipment_id, [result['id']], str(uuid.uuid5(uuid.UUID(key), 'settle')), settle)

    def quote_for_order(self, order_id, unit_ids, *, actor=None, command_id=None):
        if self.order_port is None:
            raise CommerceError('dependency_unavailable', 'Orders owner port required')
        order = self.order_port(order_id, actor=actor)
        chosen = set(unit_ids)
        if not unit_ids or len(chosen) != len(unit_ids):
            raise CommerceError('invalid_input', 'Distinct original units required')
        lines = []
        for line in order['lines']:
            selected = [u['unit_id'] for u in line['units'] if u['unit_id'] in chosen]
            if selected:
                lines.append({'variant_id': line['variant_id'], 'quantity': len(selected), 'unit_ids': selected})
        _unit_ids(order, lines)
        return self.quote(order['snapshot']['contact']['address'], lines=lines, order_id=order_id, actor=actor, command_id=command_id)

    def refresh_tracking(self, shipment_id, event=None, *, actor=None, command_id=None):
        event = event or {}
        status = event.get('status', 'unknown')
        ranks = {'unknown': 0, 'pre_transit': 1, 'in_transit': 2, 'out_for_delivery': 3, 'delivered': 4, 'exception': 0}
        if status not in ranks:
            raise CommerceError('invalid_input', 'Unknown tracking state')
        if event.get('tracking_url') and (not re.fullmatch('https://(?:www\\.)?(?:ups.com|fedex.com|usps.com)/[^ ]*', event['tracking_url'])):
            raise CommerceError('invalid_input', 'Tracking link not allowlisted')

        def work(tx):
            row = _one(tx, 'SELECT * FROM shipping.shipments WHERE id=%s FOR UPDATE', (shipment_id,))
            _ownership(row, actor)
            ident = event.get('id') or str(uuid.uuid4())
            tx.execute('INSERT INTO shipping.tracking_events VALUES(%s,%s,%s::jsonb) ON CONFLICT DO NOTHING', (ident, shipment_id, _canonical(event)))
            target = status if ranks[status] >= ranks[row['tracking_state']] else row['tracking_state']
            return dict(_one(tx, 'UPDATE shipping.shipments SET tracking_state=%s,version=version+1 WHERE id=%s RETURNING id,order_id,units AS unit_ids,quantities,state,tracking_state,version', (target, shipment_id)))
        return _run(self, actor, 'refresh_tracking', shipment_id, event, command_id, work)

    def request_cancel_units(self, order_id, unit_ids, *, actor=None, command_id=None):

        def work(tx):
            for unit in sorted(unit_ids):
                row = tx.execute('SELECT * FROM shipping.claims WHERE order_id=%s AND unit_id=%s FOR UPDATE', (order_id, unit)).fetchone()
                if row and row['state'] in ('shipped', 'handover_reserved'):
                    raise CommerceError('ineligible', 'Unit has entered handover')
                tx.execute("INSERT INTO shipping.claims VALUES(%s,%s,NULL,'canceled') ON CONFLICT(order_id,unit_id) DO UPDATE SET state='canceled'", (order_id, unit))
            return {'id': order_id, 'state': 'cancel_acknowledged', 'version': 1, 'unit_ids': unit_ids}
        return _run(self, actor, 'request_cancel_units', order_id, [unit_ids], command_id, work)

    def reserve_handover(self, shipment_id, claim_version, *, actor=None, command_id=None):
        _integer(claim_version, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM shipping.shipments WHERE id=%s FOR UPDATE', (shipment_id,))
            _version(row, claim_version)
            _arbiter(tx, row['order_id'])
            if row['label_state'] != 'purchased' or row['state'] != 'created':
                raise CommerceError('ineligible', 'A purchased simulated label and eligible parcel are required')
            claims = tx.execute('SELECT * FROM shipping.claims WHERE shipment_id=%s ORDER BY unit_id FOR UPDATE', (shipment_id,)).fetchall()
            if any((c['state'] != 'claimed' for c in claims)):
                raise CommerceError('conflict', 'Cancellation already owns a unit')
            tx.execute("UPDATE shipping.claims SET state='handover_reserved' WHERE shipment_id=%s", (shipment_id,))
            tx.execute('UPDATE shipping.arbiters SET version=version+1 WHERE order_id=%s', (row['order_id'],))
            return dict(_one(tx, "UPDATE shipping.shipments SET state='handover_reserved',version=version+1 WHERE id=%s RETURNING id,order_id,state,version,units AS unit_ids,quantities", (shipment_id,)))
        return _run(self, actor, 'reserve_handover', shipment_id, [claim_version], command_id, work)

    def simulate_tracking(self, shipment_id, status, expected_revision, *, actor=None, command_id=None):
        if status not in ('IN_TRANSIT', 'DELIVERED', 'EXCEPTION'):
            raise CommerceError('invalid_input', 'Unsupported simulated tracking transition')
        _integer(expected_revision, 1)

        def work(tx):
            row = _one(tx, 'SELECT * FROM shipping.shipments WHERE id=%s FOR UPDATE', (shipment_id,))
            _version(row, expected_revision)
            if row['state'] != 'handed_over':
                raise CommerceError('ineligible', 'A label alone is not handover or delivery')
            now = _now(tx)
            ident = str(uuid.uuid4())
            tx.execute('INSERT INTO shipping.tracking_events VALUES(%s,%s,%s::jsonb)', (ident, shipment_id, _canonical({'status': status, 'occurred_at': now, 'simulated': True})))
            current = row['tracking_state']
            target = 'delivered' if status == 'DELIVERED' or current == 'delivered' else 'in_transit' if status == 'IN_TRANSIT' else current
            if status == 'DELIVERED':
                for unit in _json(row['units']):
                    tx.execute('INSERT INTO shipping.delivered_units VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (shipment_id, unit, now))
            tx.execute("UPDATE shipping.shipments SET tracking_state=%s,delivered_at=CASE WHEN %s='delivered' THEN COALESCE(delivered_at,%s) ELSE delivered_at END,version=version+1 WHERE id=%s", (target, target, now, shipment_id))
            return dict(_one(tx, 'SELECT id,order_id,state,tracking_state,delivered_at,units AS unit_ids,quantities,version FROM shipping.shipments WHERE id=%s', (shipment_id,)))
        return _run(self, actor, 'simulate_tracking', shipment_id, [status, expected_revision], command_id, work)

def _arbiter(tx, order_id, expected=None):
    tx.execute('INSERT INTO shipping.arbiters(order_id) VALUES(%s) ON CONFLICT DO NOTHING', (order_id,))
    row = _one(tx, 'SELECT * FROM shipping.arbiters WHERE order_id=%s FOR UPDATE', (order_id,))
    if expected is not None:
        _version(row, expected)
    return row

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

def _parcel(lines):
    weight = 0
    length = 0
    width = 0
    height = 0
    for line in lines:
        quantity = _integer(line['quantity'], 1, 99)
        dimensions = line.get('dimensions_mm')
        if not isinstance(dimensions, list) or len(dimensions) != 3:
            raise CommerceError('invalid_input', 'Variant dimensions are missing')
        for value in dimensions:
            _integer(value, 1, 2000)
        weight += _integer(line['weight_g'], 1, 50000) * quantity
        length = max(length, dimensions[0])
        width = max(width, dimensions[1])
        height += dimensions[2] * quantity
    _integer(weight, 1, 50000)
    _integer(height, 1, 2000)
    return {'weight_g': weight, 'dimensions_mm': [length, width, height]}

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

def _unit_ids(order, lines):
    available = {u['unit_id']: u for line in order['lines'] for u in line.get('units', [])}
    chosen = []
    for line in lines:
        units = line.get('unit_ids')
        if not isinstance(units, list) or not units or len(units) != _integer(line.get('quantity'), 1, 99):
            raise CommerceError('invalid_input', 'Explicit original unit IDs and matching quantity required')
        if any((unit not in available or available[unit].get('variant_id', line['variant_id']) != line['variant_id'] for unit in units)):
            raise CommerceError('ineligible', 'Unit does not belong to this variant/order')
        chosen.extend(units)
    if len(chosen) != len(set(chosen)) or len(chosen) > 200:
        raise CommerceError('invalid_input', 'Distinct bounded unit IDs required')
    canceled = set(order.get('canceled_unit_ids', []))
    shipped = set(order.get('shipped_unit_ids', []))
    if set(chosen) & (canceled | shipped):
        raise CommerceError('ineligible', 'Unit is canceled or shipped')
    return sorted(chosen)

def _version(row, expected):
    if expected is not None and row['version'] != _integer(expected, 1):
        raise CommerceError('version_conflict', 'Resource changed; reload before editing')
