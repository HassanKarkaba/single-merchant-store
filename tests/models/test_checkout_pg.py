"""Actual owner integration, bounded to the Checkout participant contracts.

Uses real Cart/Catalog/Pricing/Shipping/Tax/Inventory over independently attested Pg.
The test capability is deliberately not evidence of full application/AAC permission parity.
"""
from pathlib import Path
import json,os,sys,uuid
import pytest

pytestmark = pytest.mark.native

from app.models import persistence, catalog as catalog_model, pricing, cart, shipping, tax, inventory, checkout as checkout_model, orders as orders_model, integrations, payments
from app.models import disposable as g

def allow(*args):return True
@pytest.fixture(scope='module')
def owners():
 dsn=os.environ.get('SME_TEST_DSN');assert dsn,'explicit attested disposable target required'
 g.DisposableResourceGuard.from_environment().attest_dsn(dsn)
 db=persistence.PostgresPersistence(dsn);assert db.attest_disposable(g.DisposableResourceGuard.from_environment())['state']=='DISPOSABLE_VERIFIED';db.migrate_core()
 names={'catalog':('product-catalog-merchandising','CatalogService'),'pricing':('pricing-promotions','PricingService'),'cart':('shopping-cart-management','CartService'),'shipping':('shipping-fulfillment','ShippingService'),'tax':('tax-calculation-allocation','TaxService'),'inventory':('inventory-availability','InventoryService')}
 modules={'catalog':catalog_model,'pricing':pricing,'cart':cart,'shipping':shipping,'tax':tax,'inventory':inventory}
 services={k:getattr(modules[k],cls)(db,authorize=allow,profile='demo-test') for k,(_,cls) in names.items()}
 for service in services.values():service.migrate();service.initialize()
 checkout=checkout_model.CheckoutService(db,services['cart'],services['catalog'],services['pricing'],services['tax'],services['shipping'],authorize=allow,profile='demo-test');checkout.migrate();checkout.initialize()
 checkout.policy_snapshot={'revision':'pf-policy-1','return_days':30};services['checkout']=checkout
 catalog=services['catalog'];catalog.pricing_port=services['pricing'].get_prices;catalog.inventory_port=services['inventory'].get_availability
 services['pricing'].variant_port=catalog.get_variants
 public=lambda ids:catalog.get_variants(ids,published_only=True)
 for key in ('cart','pricing','shipping'):services[key].catalog_port=public
 services['inventory'].variant_port=lambda id,**kw:catalog.get_variants([id],**kw).get(id)
 services['inventory'].checkout_port=checkout.inspect_reservation_input
 checkout.reservation_port=services['inventory'].inspect_reservation
 db.migrate('pf_checkout_test',1,'CREATE TABLE pf_checkout_admissions(id text PRIMARY KEY,receipt jsonb NOT NULL)')
 def admission(id,*,actor):
  with db.transaction('pf_checkout_test') as tx:
   r=tx.fetchone('SELECT receipt FROM pf_checkout_admissions WHERE id=%s',(id,))
   if r is None:raise ValueError('not_found')
   return r['receipt']
 checkout.admission_port=admission
 staff={'id':str(uuid.uuid4()),'merchant_id':str(uuid.uuid4()),'kind':'staff','roles':['merchant_admin']}
 asset={'id':'pf-test-curated','sha256':'b'*64,'mime':'image/svg+xml','width':80,'height':80,'url':'/assets/pf-test-curated.svg'}
 catalog.media_port=lambda id:asset if id==asset['id'] else None
 p=catalog.create_product('Synthetic participant sample','Synthetic product','pf-checkout-'+uuid.uuid4().hex,{'category':'test','tax_code':'demo_physical_standard','synthetic':True},actor=staff)
 v=catalog.upsert_variant(p['id'],sku='PF-'+uuid.uuid4().hex,options={'size':'One'},weight_g=150,dimensions_mm=[10,20,30],expected_revision=p['version'],actor=staff)
 m=catalog.attach_media(p['id'],asset['id'],'Synthetic sample',0,v['version'],actor=staff)
 services['pricing'].set_price(v['id'],101,expected_revision=0,actor=staff)
 catalog.publish(p['id'],m['version'],actor=staff)
 services['inventory'].adjust_stock(v['id'],100,'synthetic test fixture',0,actor=staff)
 # Actual Order/payment provider owners extend the participant regression below.
 orders=orders_model.OrderService(db,authorize=allow,profile='demo-test');orders.resource_predicate=allow;orders.migrate();orders.initialize();services['orders']=orders
 provider=integrations.ExternalIntegrationService(db,authorize=allow,profile='demo-test');provider.migrate();provider.initialize();provider.bind_dispatch(allow);provider.bind_inspection(allow);services['provider']=provider
 payment=payments.PaymentService(db,provider,authorize=allow,profile='demo-test');payment.migrate();payment.initialize();services['payment']=payment
 payment.admission_port=admission;payment.order_port=orders.get_receipt;payment.inventory_port=services['inventory'].inspect_receipt
 services['inventory'].order_port=orders.get_order;services['inventory'].resource_predicate=allow
 return db,services,v['id'],staff

ADDRESS={'recipient':'Demo Customer','line1':'100 Example Street','city':'Demo City','region':'MI','postal_code':'48104','country':'US'}
def draft(owners):
 db,s,variant,staff=owners;actor={'id':str(uuid.uuid4()),'merchant_id':staff['merchant_id'],'kind':'customer','roles':['customer']}
 cart=s['cart'].create_guest_cart(actor['id'],actor=actor);cart=s['cart'].set_line(cart['id'],variant,3,cart['version'],actor=actor)
 row=s['checkout'].begin(cart['id'],cart['version'],actor=actor,command_id=str(uuid.uuid4()))
 row=s['checkout'].update_contact_and_address(row['id'],'buyer@shop.invalid',ADDRESS,row['version'],actor=actor,command_id=str(uuid.uuid4()))
 return actor,row

def quoted(owners):
 db,s,_,_=owners;actor,row=draft(owners);checkout=s['checkout'];offers=checkout.evaluate(row['id'],actor=actor,command_id=str(uuid.uuid4()));assert offers['state']=='DELIVERY_REQUIRED' and offers['quote'] is None
 chosen=checkout.select_delivery(row['id'],offers['delivery_offers'][0]['id'],offers['version'],actor=actor,command_id=str(uuid.uuid4()))
 key=str(uuid.uuid4());row=checkout.evaluate(row['id'],actor=actor,command_id=key)
 assert checkout.evaluate(row['id'],actor=actor,command_id=key)==row
 return actor,row

def test_explicit_delivery_exact_allocations_replay_and_admission_claim(owners):
 db,s,_,_=owners;actor,row=quoted(owners);checkout=s['checkout'];q=row['quote']
 assert q['subtotal_minor']==303 and q['shipping_minor']==500 and q['tax_minor']==48 and q['shipping_tax_minor']==30 and q['total_minor']==851
 assert sum(u['tax_minor'] for line in q['lines'] for u in line['units'])==18
 assert sum(u['net_minor']+u['tax_minor'] for line in q['lines'] for u in line['units'])+q['shipping_minor']+q['shipping_tax_minor']==q['total_minor']
 row=checkout.accept_quote(row['id'],q['id'],q['total_minor'],row['version'],actor=actor,command_id=str(uuid.uuid4()))
 attempt=str(uuid.uuid4());admission={'attempt_id':attempt,'checkout_id':row['id'],'accepted_quote_id':q['id'],'quote_digest':q['digest'],'total_minor':q['total_minor'],'owner_id':actor['id']}
 with db.transaction('pf_checkout_test') as tx:tx.execute('INSERT INTO pf_checkout_admissions VALUES(%s,%s::jsonb)',(attempt,json.dumps(admission)))
 row=checkout.claim_purchase(row['id'],q['id'],attempt,actor=actor,command_id=str(uuid.uuid4()))
 assert row['claimed_attempt_id']==attempt and row['state']=='PURCHASING'
 reservation=s['inventory'].reserve(row['id'],row['lines'],actor=actor)
 bound=checkout.bind_reservation(row['id'],attempt,reservation,actor=actor,command_id=str(uuid.uuid4()))
 assert checkout.inspect_purchase_claim(attempt,actor=actor)['frozen_quote_until']==reservation['expires_at']
 assert checkout.bind_reservation(row['id'],attempt,reservation,actor=actor,command_id=str(uuid.uuid4()))['frozen_quote_until']==bound['frozen_quote_until']
 # Provisional Orders has no payment binding until actual confirmation. Capture
 # must accept ONLY this pending native state, with original owner/checkout/total.
 machine={'id':str(uuid.uuid4()),'kind':'recovery','merchant_id':actor['merchant_id'],'obligation_id':attempt}
 order=s['orders'].create_provisional(row['id'],q,{'actor_id':actor['id'],'email':'buyer@shop.invalid','address':ADDRESS},{'offer_id':q['shipping_quote_id']},q['policy'],actor=machine)
 assert s['orders'].get_receipt({'id':order['id']},actor=machine).get('payment_id') is None and order['payment_state']=='pending'
 allocation=s['inventory'].allocate(reservation['id'],order['id'],actor=machine)
 admission.update(currency='USD',demo_instrument='demo_success',obligation_id=attempt,frozen_quote_until=reservation['expires_at'])
 with db.transaction('pf_checkout_test') as tx:tx.execute('UPDATE pf_checkout_admissions SET receipt=%s::jsonb WHERE id=%s',(json.dumps(admission),attempt))
 payment=s['payment'];assert payment.create_authorization(attempt,q['digest'],q['total_minor'],'USD','buyer@shop.invalid',actor=machine,demo_instrument='demo_success')['state']=='AUTHORIZED'
 receipt=payment.capture(attempt,{'id':order['id']},allocation,actor=machine);assert receipt['state']=='CAPTURED'
 with pytest.raises(ValueError,match='checkout_already_submitted'):checkout.update_contact_and_address(row['id'],'buyer@shop.invalid',ADDRESS,row['version'],actor=actor)

def test_foreign_current_owner_and_revocation_deny_cached_results(owners):
 _,s,_,_=owners;actor,row=draft(owners);key=str(uuid.uuid4());first=s['checkout'].evaluate(row['id'],actor=actor,command_id=key)
 foreign={**actor,'id':str(uuid.uuid4())}
 with pytest.raises(ValueError,match='not_found'):s['checkout'].evaluate(row['id'],actor=foreign,command_id=key)
 old=s['checkout'].authorize;s['checkout'].authorize=lambda *args:False
 try:
  with pytest.raises(PermissionError):s['checkout'].evaluate(row['id'],actor=actor,command_id=key)
 finally:s['checkout'].authorize=old
 assert s['checkout'].evaluate(row['id'],actor=actor,command_id=key)==first

def test_invalid_destination_and_stale_delivery_revision_have_no_selection(owners):
 _,s,_,_=owners;actor,row=draft(owners);checkout=s['checkout']
 with pytest.raises(ValueError):checkout.update_contact_and_address(row['id'],'buyer@example.com',ADDRESS,row['version'],actor=actor)
 with pytest.raises(ValueError):checkout.update_contact_and_address(row['id'],'buyer@shop.invalid',{**ADDRESS,'postal_code':'48201'},row['version'],actor=actor)
 offers=checkout.evaluate(row['id'],actor=actor)
 with pytest.raises(ValueError,match='version_conflict'):checkout.select_delivery(row['id'],offers['delivery_offers'][0]['id'],row['version'],actor=actor)
 assert checkout.get_checkout(row['id'],actor=actor)['selected_offer'] is None

def test_claim_safety_snapshot_requires_actual_application_state_not_boolean(owners):
 db,s,_,staff=owners;checkout=s['checkout'];actor={'id':str(uuid.uuid4()),'kind':'recovery','merchant_id':staff['merchant_id'],'obligation_id':str(uuid.uuid4())}
 checkout.purchase_state_verifier=None
 assert checkout.safety_snapshot(actor=actor)['complete'] is False
 def inspect(tx,checkout_id,attempt_id):
  row=tx.fetchone('SELECT receipt FROM pf_checkout_admissions WHERE id=%s',(attempt_id,))
  actual=row['receipt'] if row else {}
  return {'checkout_id':actual.get('checkout_id'),'attempt_id':actual.get('attempt_id'),'state':actual.get('state','ADMITTED'),'admission_receipt':actual.get('quote_digest')}
 checkout.purchase_state_verifier=inspect
 actual=checkout.safety_snapshot(actor=actor);assert actual['complete'] is True and actual['unresolved']>=1
 checkout.purchase_state_verifier=lambda *args:True
 assert checkout.safety_snapshot(actor=actor)['complete'] is False
 checkout.purchase_state_verifier=inspect
