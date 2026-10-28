"""Closed commerce request routing and authorized response selection."""
from __future__ import annotations
import re
from app.models.common import POLICY, STAFF, BUYERS, CATALOG_ROLES, STOCK_ROLES, SUPPORT_ROLES, FINANCE_ROLES, canonical_id, require_revision
READ_PERMISSIONS={'/api/v1/merchant/catalog':CATALOG_ROLES|STOCK_ROLES,'/api/v1/merchant/prices':CATALOG_ROLES,'/api/v1/merchant/pricing':CATALOG_ROLES,'/api/v1/merchant/promotions':CATALOG_ROLES,'/api/v1/merchant/stock':STOCK_ROLES,'/api/v1/merchant/movements':STOCK_ROLES,'/api/v1/merchant/orders':STAFF,'/api/v1/merchant/overview':STAFF,'/api/v1/merchant/returns':SUPPORT_ROLES|FINANCE_ROLES,'/api/v1/merchant/finance':FINANCE_ROLES,'/api/v1/merchant/investigation':FINANCE_ROLES|SUPPORT_ROLES,'/api/v1/merchant/settings':STAFF}
COMMANDS={}

class CommerceController:
    def __init__(self, store):
        self.store = store

    def dispatch(self,path,body,actor):
        if not isinstance(body,dict) or set(body)-{'operation','command_id','expected_version','input'} or not {'operation','command_id','input'}<=set(body):raise ValueError('invalid_command')
        canonical_id(body['command_id']);operation=body['operation'];data=body['input'];revision=body.get('expected_version');key=body['command_id']
        if not isinstance(data,dict):raise ValueError('invalid_input')
        record=COMMANDS.get((path,operation))
        dynamic=re.fullmatch(r'/api/v1/orders/([0-9a-f-]{36})/commands',path)
        if record is None and dynamic:record=COMMANDS.get(('/api/v1/orders/:id/commands',operation));data=dict(data,order_id=canonical_id(dynamic.group(1)))
        if record is None:raise ValueError('not_found')
        required,allowed,permission=record
        if set(data)-allowed or not required<=set(data):raise ValueError('invalid_input')
        if not set(actor.get('roles',[]))&set(permission):raise PermissionError('forbidden')
        if revision is not None and (type(revision) is not int or revision<0):raise ValueError('invalid_version')
        actor=self.store.enrich_actor(actor)
        self.store.authorize_command(actor)
        kw={'actor':actor,'command_id':key}
        if path=='/api/v1/cart/commands':
            if operation=='set_line':self.store.cart.set_line(actor['cart_id'],data['variant_id'],data['quantity'],require_revision(revision),**kw)
            elif operation=='remove_line':self.store.cart.remove_line(actor['cart_id'],data['variant_id'],require_revision(revision),**kw)
            else:self.store.cart.set_coupon(actor['cart_id'],data['coupon_code'],require_revision(revision),**kw)
            return self.store.cart_view(actor)
        if path=='/api/v1/checkout/commands':
            if operation=='begin':return self.store.checkout.begin(actor['cart_id'],data['cart_revision'],**kw)
            if operation=='address':return self.store.checkout.update_contact_and_address(data['checkout_id'],data['email'],data['address'],require_revision(revision),**kw)
            if operation=='delivery':return self.store.checkout.select_delivery(data['checkout_id'],data['offer_id'],require_revision(revision),**kw)
            if operation=='evaluate':return self.store.checkout.evaluate(data['checkout_id'],**kw)
            return self.store.checkout.accept_quote(data['checkout_id'],data['quote_id'],data['total_minor'],require_revision(revision),**kw)
        if path=='/api/v1/purchases/commands':
            if operation=='submit':return self.store.purchase.submit(data['checkout_id'],data['accepted_quote_id'],key,actor=actor,demo_instrument=data['demo_instrument'])
            return self.store.purchase.get_status(data['attempt_id'],actor=actor)
        if path=='/api/v1/merchant/catalog/commands':
            if operation=='create_product':return self.store.catalog.create_product(data['name'],data['description'],data['slug'],{'category':data['category'],'tax_code':'demo_physical_standard'},**kw)
            if operation=='update_product':return self.store.catalog.update_product(data['product_id'],data['changes'],require_revision(revision),**kw)
            if operation=='upsert_variant':return self.store.catalog.upsert_variant(data['product_id'],data.get('variant_id'),data['sku'],data['options'],data['weight_g'],data['dimensions_mm'],require_revision(revision),**kw)
            if operation=='select_media':return self.store.catalog.attach_media(data['product_id'],data['asset_id'],data['alt_text'],data.get('position',0),require_revision(revision),**kw)
            if operation=='publish':return self.store.catalog.publish(data['product_id'],require_revision(revision),None,**kw)
            return self.store.catalog.unpublish(data['product_id'],require_revision(revision),**kw)
        if path=='/api/v1/merchant/prices/commands':return self.store.pricing.set_price(data['variant_id'],data['unit_minor'],expected_revision=require_revision(revision,zero=True),**kw)
        if path=='/api/v1/merchant/promotions/commands':
            if operation=='create_promotion':return self.store.pricing.create_promotion(**data,**kw)
            return getattr(self.store.pricing,operation)(data['promotion_id'],require_revision(revision),**kw)
        if path=='/api/v1/merchant/stock/commands':return self.store.inventory.adjust_stock(data['variant_id'],data['delta'],data['reason'],require_revision(revision,zero=True),**kw)
        if path=='/api/v1/merchant/fulfillment/commands':return self.store.fulfillment.command(operation,data,revision,key,actor=actor)
        if path=='/api/v1/merchant/returns/commands' or path=='/api/v1/returns/commands':return self.store.return_coordinator.command(operation,data,revision,key,actor=actor)
        if path=='/api/v1/merchant/refunds/commands':return self.store.refunds.request_plan(data['return_id'],key,actor=actor)
        if dynamic:
            if operation=='cancel_units':return self.store.fulfillment.cancel_units(data['order_id'],data['unit_ids'],data['reason'],revision,key,actor=actor)
            return self.store.return_coordinator.command('request_return',data,revision,key,actor=actor)
        raise ValueError('not_found')

    def read(self,path,query,actor):
        if path=='/api/v1/catalog':return self.store.catalog_view(filters=read_filters(query),cursor=single(query,'cursor'))
        matched=re.fullmatch('/api/v1/catalog/([a-z0-9-]{1,200})',path)
        if matched:return self.store.catalog_view(slug=matched.group(1))
        if not actor or actor.get('kind') not in ('guest','customer','staff'):raise PermissionError('unauthenticated')
        if path=='/api/v1/cart':return self.store.cart_view(actor)
        for pattern,method in [(r'/api/v1/checkout/([0-9a-f-]{36})',self.store.checkout.get_checkout),(r'/api/v1/purchases/([0-9a-f-]{36})',self.store.purchase.get_status),(r'/api/v1/orders/([0-9a-f-]{36})',self.store.orders.get_order),(r'/api/v1/returns/([0-9a-f-]{36})',self.store.returns.get_return)]:
            match=re.fullmatch(pattern,path)
            if match:
                ident=canonical_id(match.group(1))
                if '/returns/' in path and set(actor['roles'])&FINANCE_ROLES and not set(actor['roles'])&(BUYERS|SUPPORT_ROLES):
                    proposal=self.store.returns.get_refund_plan(ident,actor=actor)
                    return {'id':ident,'order_id':proposal['order_id'],'state':proposal['return_state'],'refund_plan':self.store.orders.get_refund_plan(proposal['order_plan_id'],actor=actor),'unit_records':proposal.get('unit_allocations',[]),'access':'approved_financial_plan'}
                result=method(ident,actor=actor)
                if '/orders/' in path:
                    result=order_browser_view(result)
                    result['shipments']=self.store.shipping.list_order_shipments(result['id'],actor=actor)['items']
                    if set(actor['roles'])&(BUYERS|SUPPORT_ROLES|FINANCE_ROLES):
                        linked=self.store.returns.list_order_returns(result['id'],single(query,'returns_cursor'),actor=actor)
                        result['returns']=linked['items'];result['returns_next_cursor']=linked.get('next_cursor')
                return result
        match=re.fullmatch(r'/api/v1/operations/([0-9a-f-]{36})',path)
        if match:
            return self.store.operation_status(canonical_id(match.group(1)), actor)
        label=re.fullmatch(r'/api/v1/shipments/([0-9a-f-]{36})/label',path)
        if label:return self.store.shipping.get_label(canonical_id(label.group(1)),actor=actor)
        if path=='/api/v1/orders':
            result=self.store.orders.list_customer_orders(actor['id'],actor=actor,cursor=single(query,'cursor'));return {'orders':result['items'],'next_cursor':result.get('next_cursor')}
        permission=READ_PERMISSIONS.get(path)
        if permission is None:raise ValueError('not_found')
        if not set(actor['roles'])&permission:raise PermissionError('forbidden')
        if path=='/api/v1/merchant/catalog':
            result=self.store.catalog.list_merchant_products(read_filters(query),single(query,'cursor'),actor=actor)
            prices=self.store.pricing.get_prices([v['id'] for p in result['items'] for v in p.get('variants',[])],actor=actor)
            products=[dict(p,category=p.get('attributes',{}).get('category'),variants=[dict(v,unit_minor=prices.get(v['id'],{}).get('unit_minor'),price_revision=prices.get(v['id'],{}).get('version',0)) for v in p.get('variants',[])]) for p in result['items']]
            return {'products':products,'next_cursor':result.get('next_cursor'),'assets':self.store.config.get('assets',[])}
        if path in ('/api/v1/merchant/prices','/api/v1/merchant/pricing'):return self.read('/api/v1/merchant/catalog',query,actor)
        if path=='/api/v1/merchant/promotions':return self.store.pricing.list_promotions(actor=actor,cursor=single(query,'cursor'))
        if path=='/api/v1/merchant/movements':return self.store.inventory.list_movements(single(query,'variant_id'),single(query,'cursor'),actor=actor)
        if path=='/api/v1/merchant/stock':
            result=self.store.catalog.list_merchant_products({},single(query,'cursor'),actor=actor);variants={v['id']:(p,v) for p in result['items'] for v in p.get('variants',[])};stock=self.store.inventory.get_availability(list(variants),actor=actor)
            return {'stock':[dict(stock.get(id,{'on_hand':0,'reserved':0,'allocated':0,'available':0,'version':0,'state':'not_initialized'}),variant_id=id,product_name=p['name'],sku=v['sku']) for id,(p,v) in variants.items()],'next_cursor':result.get('next_cursor')}
        if path in ('/api/v1/merchant/orders','/api/v1/merchant/overview'):
            result=self.store.orders.list_merchant_orders(actor=actor,cursor=single(query,'cursor'));summary=self.store.orders.merchant_summary(actor=actor)
            return {'orders':[order_browser_view(o) for o in result['items']],'next_cursor':result.get('next_cursor'),**summary,'readiness':self.store.readiness()}
        if path=='/api/v1/merchant/returns':
            result=self.store.returns.list_returns(actor=actor);return {'returns':result['items'],'next_cursor':result.get('next_cursor')}
        if path in ('/api/v1/merchant/finance','/api/v1/merchant/investigation'):
            return {'items':self.store.recovery.list_items(actor=actor),'simulation':True,'readiness':self.store.readiness()}
        if path=='/api/v1/merchant/settings':return {'profile':self.store.profile,'policy':POLICY,'assets':self.store.config.get('assets',[]),'simulated':True,'original_production_status':'BLOCKED'}
        raise ValueError('not_found')

def order_browser_view(order):
    snapshot=order.get('snapshot',{});quote=snapshot.get('quote',{})
    view=dict(order)
    for key in ('subtotal_minor','discount_minor','shipping_minor','tax_minor'):view[key]=quote.get(key)
    view['address']=snapshot.get('contact',{}).get('address')
    view['policy_revision']=snapshot.get('policy',{}).get('revision')
    view['lines']=[dict(line,shipped_quantity=order.get('shipped',{}).get(line['variant_id'],0),returned_quantity=order.get('returned',{}).get(line['variant_id'],0)) for line in order.get('lines',[])]
    return view

def read_filters(query):
    allowed={'query','category','sort','cursor','limit','min_price','max_price','in_stock','state'}
    if set(query)-allowed:raise ValueError('invalid_query')
    filters={}
    for key in ('query','category','sort','state'):
        value=single(query,key)
        if value is not None:filters[key]=value
    for key in ('limit','min_price','max_price'):
        value=single(query,key)
        if value is not None:
            if not value.isascii() or not value.isdigit():raise ValueError('invalid_query')
            filters[key]=int(value)
    value=single(query,'in_stock')
    if value is not None:
        if value not in ('true','false'):raise ValueError('invalid_query')
        filters['in_stock']=value=='true'
    return filters

def register_commands():
    def add(path,op,required,roles,optional=()):COMMANDS[(path,op)]=(frozenset(required.split()),frozenset(required.split())|frozenset(optional),roles)
    base='/api/v1/'
    for op,fields in [('set_line','variant_id quantity'),('remove_line','variant_id'),('set_coupon','coupon_code')]:add(base+'cart/commands',op,fields,BUYERS)
    for op,fields in [('begin','cart_revision'),('address','checkout_id email address'),('delivery','checkout_id offer_id'),('evaluate','checkout_id'),('accept_quote','checkout_id quote_id total_minor')]:add(base+'checkout/commands',op,fields,BUYERS)
    add(base+'purchases/commands','submit','checkout_id accepted_quote_id demo_instrument',BUYERS);add(base+'purchases/commands','reconcile','attempt_id',BUYERS)
    for op,fields,optional in [('create_product','name description slug category',()),('update_product','product_id changes',()),('upsert_variant','product_id sku options weight_g dimensions_mm',('variant_id',)),('select_media','product_id asset_id alt_text',('position',)),('publish','product_id',()),('unpublish','product_id',())]:add(base+'merchant/catalog/commands',op,fields,CATALOG_ROLES,optional)
    add(base+'merchant/prices/commands','set_price','variant_id unit_minor',CATALOG_ROLES)
    add(base+'merchant/promotions/commands','create_promotion','kind value eligible_variants min_subtotal start_at end_at max_redemptions per_customer_limit coupon_code',CATALOG_ROLES)
    for op in ('activate_promotion','end_promotion'):add(base+'merchant/promotions/commands',op,'promotion_id',CATALOG_ROLES)
    add(base+'merchant/stock/commands','adjust_stock','variant_id delta reason',STOCK_ROLES)
    for op,fields in [('quote_parcel','order_id unit_ids'),('plan_shipment','order_id unit_ids offer_id'),('purchase_label','shipment_id'),('reserve_handover','shipment_id'),('confirm_handover','shipment_id'),('simulate_tracking','shipment_id status')]:add(base+'merchant/fulfillment/commands',op,fields,STOCK_ROLES)
    for op,fields in [('approve_return','return_id decision reason'),('record_received','return_id unit_ids'),('record_inspection','return_id dispositions'),('create_exchange_link','return_id replacement_variant_id'),('close_return','return_id')]:add(base+'merchant/returns/commands',op,fields,SUPPORT_ROLES)
    add(base+'returns/commands','request_return','order_id unit_ids reason requested_resolution',BUYERS)
    add(base+'merchant/refunds/commands','request_refund','return_id',FINANCE_ROLES)
    add(base+'orders/:id/commands','cancel_units','order_id unit_ids reason',BUYERS|SUPPORT_ROLES)
    add(base+'orders/:id/commands','request_return','order_id unit_ids reason requested_resolution',BUYERS|SUPPORT_ROLES)

def single(query,key):
    value=query.get(key)
    if value is None:return None
    if isinstance(value,list):
        if len(value)!=1:raise ValueError('duplicate_query')
        value=value[0]
    if not isinstance(value,str) or len(value)>400:raise ValueError('invalid_query')
    return value

register_commands()
