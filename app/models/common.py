"""Shared domain policy, authority contexts and exact-value validation."""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parents[2]

CURRENT_WORKLOAD_ACTOR=contextvars.ContextVar('sme_current_workload_actor',default=None)

CURRENT_WORKER_LEASE=contextvars.ContextVar('sme_current_worker_lease',default=None)

POLICY={'revision':'SYNTHETIC-20261023','return_window_days':30,'receipt_before_refund':True,'receipt_before_replacement':True,'outbound_shipping_refund':'FULL_MERCHANT_FAULT','financial_retention_years':7,'synthetic':True}

STAFF=frozenset({'catalog_editor','stock_operator','support','finance','merchant_admin'})

BUYERS=frozenset({'guest','customer'})

CATALOG_ROLES=frozenset({'catalog_editor','merchant_admin'})

STOCK_ROLES=frozenset({'stock_operator','merchant_admin'})

SUPPORT_ROLES=frozenset({'support','merchant_admin'})

FINANCE_ROLES=frozenset({'finance','merchant_admin'})

PF_WORK_OPS = tuple(sorted({
'application.step','application.operation','application.purchase.read','application.recovery.read',
'checkout.claim_purchase','checkout.get_checkout','checkout.inspect_purchase_claim','checkout.inspect_receipt','checkout.bind_reservation','checkout.revalidate_sellability','checkout.begin','checkout.inspect_reservation_input',
'pricing.quote','pricing.get_quote','pricing.reserve_redemption','pricing.commit_redemption','pricing.release_redemption_hold','pricing.inspect_redemption_hold',
'inventory.reserve','inventory.inspect_checkout_reservation','inventory.inspect_reservation','inventory.allocate','inventory.inspect_allocation','inventory.inspect_receipt','inventory.release_reservation','inventory.release_allocation','inventory.record_shipment','inventory.restock_return','inventory.get_availability',
'orders.create_provisional','orders.confirm_payment','orders.mark_payment_failed','orders.get_order','orders.get_receipt','orders.get_refund_plan','orders.plan_units','orders.record_fulfillment','orders.record_delivery','orders.record_return','orders.record_refund','orders.settle_zero_plan','orders.inspect_publication',
'payments.create_authorization','payments.authorize','payments.capture','payments.cancel','payments.cancel_authorization','payments.fence_capture','payments.refund','payments.inspect_attempt','payments.inspect_receipt','payments.read','payments.effect',
'integrations.invoke','integrations.inspect','integrations.receive_callback',
'shipping.quote','shipping.quote_for_order','shipping.get_offer','shipping.claim_units','shipping.create_shipment','shipping.get_shipment','shipping.inspect_receipt','shipping.purchase_label','shipping.label_intent','shipping.label_unknown','shipping.label_purchased','shipping.reserve_handover','shipping.confirm_handover','shipping.simulate_tracking','shipping.cancel_units','shipping.inspect_cancel','shipping.get_unit_status',
'returns.request_return','returns.approve_return','returns.record_received','returns.record_inspection','returns.bind_refund_plan','returns.get_return','returns.get_receipt','returns.build_refund_plan','returns.close_return','returns.create_exchange_link','returns.record_exchange_outcome',
'cart.create_replacement_cart','cart.get_cart','cart.clear_purchased_lines','cart.create_guest_cart','cart.set_line',
'notifications.enqueue_transactional','notifications.get_delivery_status','notifications.dispatch_claim','notifications.dispatch_unknown','notifications.dispatch_settle','notifications.receive_event',
'events.deliver','events.ack','events.defer','events.claim','events.dispatch_consumption',
'cart.inspect_publication','checkout.inspect_publication','pricing.inspect_publication','inventory.inspect_publication','tax.inspect_publication','shipping.inspect_publication','returns.inspect_publication','payments.inspect_publication','notifications.inspect_publication',
'tax.calculate','tax.get_calculation','tax.commit_tax','tax.inspect_transaction','tax.reverse_tax'}))

def canonical_id(value):
    if not isinstance(value,str):raise ValueError('invalid_uuid')
    try:
        if str(uuid.UUID(value))!=value:raise ValueError()
        return value
    except (ValueError,AttributeError):raise ValueError('invalid_uuid') from None

def permission_for(operation):
    public={'application.actor','application.command','application.operation'}
    if operation in public:return BUYERS|STAFF
    if operation=='application.seed':return CATALOG_ROLES|STOCK_ROLES
    admission_permissions={'application.fulfillment.submit':STOCK_ROLES,'application.cancellation.submit':BUYERS|SUPPORT_ROLES,'application.return.submit':BUYERS|SUPPORT_ROLES,'application.return.manage':SUPPORT_ROLES,'application.refund.submit':FINANCE_ROLES}
    if operation in admission_permissions:return admission_permissions[operation]
    definitions={
      'catalog':{'read':{'get_merchant_product','get_variants','list_merchant_products','export_catalog'},'write':{'attach_media','create_product','update_product','upsert_variant','retire_variant','set_collection','publish','unpublish'}},
      'pricing':{'read':{'get_prices','list_promotions','get_quote','inspect_redemption_hold'},'write':{'set_price','create_promotion','activate_promotion','end_promotion'}},
      'inventory':{'read':{'get_availability','list_movements','inspect_reservation','inspect_allocation'},'write':{'adjust_stock'}},
      'orders':{'read':{'get_order','list_customer_orders','list_merchant_orders','merchant_summary','get_receipt','get_refund_plan','export_orders'},'write':set()},
      'shipping':{'read':{'get_shipment','list_order_shipments','get_unit_status','get_label','get_offer'},'write':{'claim_units','create_shipment','purchase_label','reserve_handover','confirm_handover','simulate_tracking','cancel_units','inspect_cancel'}},
      'returns':{'read':{'get_return','list_returns','list_order_returns','get_refund_plan'},'write':{'approve_return','record_received','record_inspection','create_exchange_link','close_return'}},
      'notifications':{'read':{'get_delivery_status'},'write':set()}}
    if operation in {'cart.pending_handoffs','cart.merge_guest_cart','cart.consume_guest_handoff'}:return BUYERS|STAFF
    if operation in {'cart.create_guest_cart','cart.get_cart','cart.set_line','cart.remove_line','cart.set_coupon','cart.merge_guest_cart','cart.prepare_guest_handoff','cart.capture_guest_handoff','cart.pending_handoffs','cart.get_for_principal','cart.consume_guest_handoff'}:return BUYERS
    if operation in {'checkout.begin','checkout.update_contact_and_address','checkout.select_delivery','checkout.evaluate','checkout.accept_quote','checkout.get_checkout','checkout.revalidate_sellability','pricing.quote','tax.calculate'}:return BUYERS|CATALOG_ROLES
    if operation=='shipping.quote':return BUYERS|STOCK_ROLES
    if operation=='returns.request_return':return BUYERS|SUPPORT_ROLES
    if operation in {'purchase.read','purchase.submit','purchase.cancel'}:return BUYERS|SUPPORT_ROLES
    if operation in {'recovery.read','application.recovery.read','payments.read','payments.inspect_attempt','payments.list_reconciliation_items'}:return FINANCE_ROLES|SUPPORT_ROLES
    owner,sep,method=operation.partition('.')
    if not sep:return None
    row=definitions.get(owner)
    if not row:return None
    if method in row['read']:
        if owner in ('orders','shipping','returns','notifications'):return BUYERS|STAFF
        return CATALOG_ROLES|STOCK_ROLES
    if method in row['write']:
        return {'catalog':CATALOG_ROLES,'pricing':CATALOG_ROLES,'inventory':STOCK_ROLES,'shipping':STOCK_ROLES,'returns':SUPPORT_ROLES}[owner]
    return None

def require_revision(value,zero=False):
    if type(value) is not int or value<(0 if zero else 1):raise ValueError('expected_version_required')
    return value

def safe(value):
    if value is None or type(value) in (str,bool):return value
    if type(value) is int:
        if abs(value)>9007199254740991:raise ValueError('wire_integer_limit')
        return value
    if isinstance(value,datetime):
        if value.tzinfo is None:raise ValueError('wire_utc_required')
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value,(list,tuple)):return [safe(x) for x in value]
    if isinstance(value,dict):
        if any(not isinstance(k,str) for k in value):raise ValueError('wire_string_keys_required')
        return {k:safe(v) for k,v in value.items()}
    raise ValueError('unsupported_wire_value')

def step_id(id,step):return str(uuid.uuid5(uuid.NAMESPACE_URL,'sme:'+str(id)+':'+step))
