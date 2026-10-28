"""Retained real-owner lifecycle and delivery/audit tests on a guarded private fixture.

Uses actual Identity password/TOTP sessions, native Pg authority, coordinator permits,
owner ledgers and the independent provider simulator. Does not provision or reset.
"""
import json
import os
from pathlib import Path
import time
import uuid

import pyotp
import pytest

from test_application_journey import instance, command, prepared_checkout, wait_purchase

pytestmark = pytest.mark.native


@pytest.fixture(scope='module')
def merchant(instance):
    app,_,_=instance
    identity=app.identity
    identity_module=__import__(type(identity).__module__,fromlist=['read_protected_file'])
    accounts=json.loads(identity_module.read_protected_file(app.config['test_accounts_ref'][5:]))
    record=accounts['merchant']
    cache=Path(app.config['test_accounts_ref'][5:]).parent/'merchant.session'
    if cache.is_file():
        try:return app.actor(identity_module.read_protected_file(str(cache)).strip())
        except (PermissionError,ValueError):pass
    pre=identity.bootstrap_preauth()
    result=identity.authenticate(record['username'],record['password'],preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key='retained-lifecycle-merchant')
    assert result.challenge_credential, 'Actual staff MFA challenge required'
    # A fresh counter is necessary when a previous retained run used this factor.
    time.sleep(31-time.time()%30)
    result=identity.verify_staff_totp(result.public['challenge']['id'],pyotp.TOTP(record['totp_secret']).now(),challenge_credential=result.challenge_credential,preauth_credential=result.preauth_credential,csrf=result.public['csrf_token'])
    app.complete_authentication(result.credential)
    return app.actor(result.credential)


@pytest.fixture(scope='module')
def finance(instance):
    app,_,_=instance
    identity_module=__import__(type(app.identity).__module__,fromlist=['read_protected_file'])
    private=Path(app.config['test_accounts_ref'][5:]).parent
    file=private/'native-finance-fixture.json'
    if file.exists():record=json.loads(identity_module.read_protected_file(str(file)))
    else:
        import secrets
        record={'id':str(uuid.uuid4()),'username':'finance-native@review.invalid','password':secrets.token_urlsafe(24),'totp_secret':pyotp.random_base32()}
        file.write_text(json.dumps(record));file.chmod(0o600)
        factor=Path(app.config['totp_root'])/'native-finance.totp';factor.write_text(record['totp_secret']);factor.chmod(0o600)
    # Explicit private fixture maintenance follows the instance's independent guard.
    control=type(app)(app._instance_dsn,profile='demo-test',config=app.config,maintenance=True)
    try:control.identity.provision_account(principal_id=record['id'],username=record['username'],roles=['finance'],password=record['password'],factor_ref='file:'+str(Path(app.config['totp_root'])/'native-finance.totp'),factor_version=1)
    except identity_module.IdentityError as exc:
        if exc.code!='identity_version_conflict':raise
    cache=private/'native-finance.session'
    if cache.exists():
        try:return app.actor(identity_module.read_protected_file(str(cache)).strip())
        except (ValueError,PermissionError):pass
    pre=app.identity.bootstrap_preauth()
    result=app.identity.authenticate(record['username'],record['password'],preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key='retained-native-finance')
    assert result.challenge_credential
    time.sleep(31-time.time()%30)
    result=app.identity.verify_staff_totp(result.public['challenge']['id'],pyotp.TOTP(record['totp_secret']).now(),challenge_credential=result.challenge_credential,preauth_credential=result.preauth_credential,csrf=result.public['csrf_token'])
    app.complete_authentication(result.credential);cache.write_text(result.credential);cache.chmod(0o600)
    actor=app.actor(result.credential);assert actor['kind']=='staff' and actor['roles']==['finance']
    return actor


def staff_command(app,actor,op,data,revision=None,family='fulfillment'):
    return command(app,actor,'merchant/'+family,op,data,revision)


def wait_flow(handler,id,actor,seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=handler.get_status(id,actor=actor)
        if result['state'] in ('COMPLETE','SUCCEEDED','CANCELED_REFUND_PENDING'):return result
        handler.run(id)
        time.sleep(.1)
    pytest.fail('Native flow did not converge: '+repr(result))


def test_partial_parcel_cancel_return_original_refunds(instance,merchant):
    app,buyer,_=instance
    catalog=app.catalog.list_merchant_products({},actor=merchant)
    assert all(product['variants'] and product['media'] for product in catalog['items'])
    variant=next(v['id'] for p in app.catalog_view()['products'] for v in p['variants'] if v['available']>=2 and v['unit_minor']>=50)
    checkout,quote=prepared_checkout(app,buyer,variant,2)
    submitted=command(app,buyer,'purchases','submit',{'checkout_id':checkout['id'],'accepted_quote_id':quote['id'],'demo_instrument':'demo_success'})
    paid=wait_purchase(app,submitted['id'],buyer)
    order=app.orders.get_order(paid['order_id'],actor=merchant)
    units=order['unit_records'];unit_ids=[u['unit_id'] for u in units]
    offered=staff_command(app,merchant,'quote_parcel',{'order_id':order['id'],'unit_ids':[unit_ids[0]]})
    assert len(offered['offers'])==2
    planned=staff_command(app,merchant,'plan_shipment',{'order_id':order['id'],'unit_ids':[unit_ids[0]],'offer_id':offered['offers'][0]['id']},order['version'])
    assert planned['state']=='CREATED'
    shipment_id=planned['shipment_id']
    for operation in ('purchase_label','reserve_handover','confirm_handover'):
        shipment=app.shipping.get_shipment(shipment_id,actor=merchant)
        receipt=staff_command(app,merchant,operation,{'shipment_id':shipment_id},shipment['version'])
        assert wait_flow(app.fulfillment,receipt['id'],merchant)['state']=='COMPLETE' if receipt['state']!='COMPLETE' else True
    shipment=app.shipping.get_shipment(shipment_id,actor=merchant)
    delivered=staff_command(app,merchant,'simulate_tracking',{'shipment_id':shipment_id,'status':'DELIVERED'},shipment['version'])
    assert delivered['state']=='COMPLETE'
    order=app.orders.get_order(order['id'],actor=buyer)
    assert order['shipped_unit_ids']==[unit_ids[0]] and order['delivered_unit_times'][unit_ids[0]]
    cancelled=command(app,buyer,'orders/'+order['id'],'cancel_units',{'unit_ids':[unit_ids[1]],'reason':'changed_mind'},order['version'])
    assert cancelled['state']=='CANCELED_REFUND_PENDING'
    assert wait_flow(app.refunds,cancelled['refund_operation_id'],merchant)['state']=='SUCCEEDED'
    requested=command(app,buyer,'returns','request_return',{'order_id':order['id'],'unit_ids':[unit_ids[0]],'reason':'damaged','requested_resolution':'refund'})
    assert requested['state']=='COMPLETE'
    return_id=requested['return_id']
    for operation,data in (
        ('approve_return',{'decision':'approved','reason':'damaged'}),
        ('record_received',{'unit_ids':[unit_ids[0]]}),
        ('record_inspection',{'dispositions':[{'unit_id':unit_ids[0],'accepted':True,'disposition':'sellable'}]}),
    ):
        returned=app.returns.get_return(return_id,actor=merchant)
        receipt=staff_command(app,merchant,operation,{'return_id':return_id,**data},returned['version'],family='returns')
        assert receipt['state']=='COMPLETE'
    refund=staff_command(app,merchant,'request_refund',{'return_id':return_id},family='refunds')
    assert wait_flow(app.refunds,refund['id'],merchant)['state']=='SUCCEEDED'
    order=app.orders.get_order(order['id'],actor=buyer)
    expected=sum(u['net_minor']+u['tax_minor'] for line in order['lines'] for u in line['units'])
    assert order['refunded_minor']==expected
    assert order['canceled_unit_ids']==[unit_ids[1]] and order['returned_unit_ids']==[unit_ids[0]]
    assert app.returns.get_return(return_id,actor=buyer)['state']=='closed'
    # Independent fixture oracle; production owners always use supported receipt ports.
    with app.db.transaction('application') as tx:
        provider=tx.fetchone('SELECT provider_id FROM payments.attempts WHERE id=%s',(submitted['id'],))['provider_id']
        capture=tx.fetchone("SELECT count(*) n,sum(amount_minor) amount FROM integrations.money_ledger WHERE object_id=%s AND kind='capture'",(provider,))
        refunds=tx.fetchone("SELECT count(*) n,sum(amount_minor) amount FROM integrations.money_ledger WHERE object_id=%s AND kind='refund'",(provider,))
        movement=tx.fetchone('SELECT count(*) n FROM inventory.shipments WHERE id=%s',(shipment_id,))['n']
        restock=tx.fetchone("SELECT count(*) n FROM inventory.movements WHERE reason='inspected_return' AND reference=%s",(return_id,))['n']
    assert capture['n']==1 and capture['amount']==quote['total_minor']
    assert refunds['n']==2 and refunds['amount']==expected
    assert movement==restock==1


def test_all_family_archive_converges_without_recursive_publication(instance):
    app,_,_=instance
    maintenance=type(app)(app._instance_dsn,profile='demo-test',config=app.config,maintenance=True)
    maintenance.initialize();maintenance.recovery.provision_control()
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        result=app.recovery.run_once(100)
        assert not result['failures']
        if result['state']=='ANOTHER_WORKER':
            time.sleep(.2);continue
        if result['processed']==result['delivery_discovered']==0:
            current=app.recovery.unresolved_snapshot()
            if current['complete'] and all(v==0 for k,v in current['counts'].items() if k.endswith('.delivery')):break
            time.sleep(.1)
    else:pytest.fail('Actual owner delivery failed to quiesce within bounded turn window')
    snapshot=app.recovery.unresolved_snapshot()
    assert snapshot['complete'] is True and snapshot['failures']==[]
    assert set(snapshot['coverage'])=={'application','purchases','payments','refunds','cancellations','fulfillment','returns','tax','notifications','events','audit'}
    assert all(v==0 for k,v in snapshot['counts'].items() if k.endswith('.delivery'))
    with app.db.transaction('application') as tx:
        assert tx.fetchone('SELECT count(*) n FROM events.audit_archive')['n']>0
        assert tx.fetchone("SELECT count(*) n FROM commerce_audit_staging WHERE state<>'RECORDED'")['n']==0
        assert tx.fetchone("SELECT count(*) n FROM commerce_outbox WHERE owner='events' AND topic IN ('deliver','claim','ack','defer')")['n']==0


@pytest.mark.parametrize('refund_first',[False,True])
def test_independent_replacement_keeps_original_refund(instance,merchant,refund_first):
    app,buyer,_=instance
    variant=next(v['id'] for p in app.catalog_view()['products'] for v in p['variants'] if v['available']>=2 and v['unit_minor']>=50)
    original_checkout,original_quote=prepared_checkout(app,buyer,variant)
    submitted=command(app,buyer,'purchases','submit',{'checkout_id':original_checkout['id'],'accepted_quote_id':original_quote['id'],'demo_instrument':'demo_success'})
    paid=wait_purchase(app,submitted['id'],buyer);order=app.orders.get_order(paid['order_id'],actor=merchant);unit=order['unit_records'][0]['unit_id']
    offered=staff_command(app,merchant,'quote_parcel',{'order_id':order['id'],'unit_ids':[unit]})
    planned=staff_command(app,merchant,'plan_shipment',{'order_id':order['id'],'unit_ids':[unit],'offer_id':offered['offers'][0]['id']},order['version'])
    assert planned['state']=='CREATED';shipment_id=planned['shipment_id']
    for operation in ('purchase_label','reserve_handover','confirm_handover','simulate_tracking'):
        shipment=app.shipping.get_shipment(shipment_id,actor=merchant);data={'shipment_id':shipment_id}
        if operation=='simulate_tracking':data['status']='DELIVERED'
        receipt=staff_command(app,merchant,operation,data,shipment['version']);assert receipt['state']=='COMPLETE'
    requested=command(app,buyer,'returns','request_return',{'order_id':order['id'],'unit_ids':[unit],'reason':'damaged','requested_resolution':'exchange'})
    assert requested['state']=='COMPLETE';return_id=requested['return_id']
    for operation,data in [('approve_return',{'decision':'approved','reason':'damaged'}),('record_received',{'unit_ids':[unit]}),('record_inspection',{'dispositions':[{'unit_id':unit,'accepted':True,'disposition':'sellable'}]})]:
        returned=app.returns.get_return(return_id,actor=merchant);receipt=staff_command(app,merchant,operation,{'return_id':return_id,**data},returned['version'],family='returns');assert receipt['state']=='COMPLETE'
    if refund_first:
        original_refund=staff_command(app,merchant,'request_refund',{'return_id':return_id},family='refunds')
        assert wait_flow(app.refunds,original_refund['id'],merchant)['state']=='SUCCEEDED'
        assert app.returns.get_return(return_id,actor=buyer)['state']=='closed'
        assert app.orders.get_order(order['id'],actor=buyer)['refunded_minor']==original_quote['total_minor']
    returned=app.returns.get_return(return_id,actor=merchant)
    linked=staff_command(app,merchant,'create_exchange_link',{'return_id':return_id,'replacement_variant_id':variant},returned['version'],family='returns')
    assert linked['state']=='COMPLETE';checkout_id=linked['replacement_checkout_id'];assert checkout_id
    if not refund_first:
        original_refund=staff_command(app,merchant,'request_refund',{'return_id':return_id},family='refunds')
        assert wait_flow(app.refunds,original_refund['id'],merchant)['state']=='SUCCEEDED'
    refunded=app.orders.get_order(order['id'],actor=buyer)['refunded_minor'];assert refunded==original_quote['total_minor']
    checkout=app.checkout.get_checkout(checkout_id,actor=buyer)
    assert checkout['replacement_return_id']==return_id and checkout['owner_id']==buyer['id']
    checkout=command(app,buyer,'checkout','address',{'checkout_id':checkout_id,'email':'replacement@review.invalid','address':{'recipient':'Demo Customer','line1':'100 Example Street','city':'Demo City','region':'MI','postal_code':'48104','country':'US'}},checkout['version'])
    checkout=command(app,buyer,'checkout','evaluate',{'checkout_id':checkout_id})
    checkout=command(app,buyer,'checkout','delivery',{'checkout_id':checkout_id,'offer_id':checkout['delivery_offers'][0]['id']},checkout['version'])
    checkout=command(app,buyer,'checkout','evaluate',{'checkout_id':checkout_id});quote=checkout['quote']
    checkout=command(app,buyer,'checkout','accept_quote',{'checkout_id':checkout_id,'quote_id':quote['id'],'total_minor':quote['total_minor']},checkout['version'])
    replacement=command(app,buyer,'purchases','submit',{'checkout_id':checkout_id,'accepted_quote_id':quote['id'],'demo_instrument':'demo_success'})
    replacement_paid=wait_purchase(app,replacement['id'],buyer)
    assert replacement_paid['state']=='PAID' and replacement_paid['order_id']!=order['id']
    returned=app.returns.get_return(return_id,actor=buyer)
    assert returned['replacement_state']=='completed' and returned['state']=='closed'
    assert app.orders.get_order(order['id'],actor=buyer)['refunded_minor']==refunded
    with app.db.transaction('application') as tx:
        assert tx.fetchone('SELECT count(*) n FROM application.purchase_workflows WHERE checkout_id=%s',(checkout_id,))['n']==1
        assert tx.fetchone("SELECT count(*) n FROM integrations.money_ledger WHERE kind='capture' AND object_id=(SELECT provider_id FROM payments.attempts WHERE id=%s)",(replacement['id'],))['n']==1


def test_odd_cent_discount_and_tax_against_integer_oracle(instance,merchant):
    app,buyer,_=instance
    variants=sorted(v['id'] for p in app.catalog_view()['products'] for v in p['variants'] if v['unit_minor']>=50)[:2]
    now=int(time.time()*1000)
    promotion=app.pricing.create_promotion('fixed',3,variants,start_at=now-1000,end_at=now+600000,coupon_code='PF'+uuid.uuid4().hex[:24],actor=merchant,command_id=str(uuid.uuid4()))
    app.pricing.activate_promotion(promotion['id'],promotion['version'],actor=merchant,command_id=str(uuid.uuid4()))
    with app.db.transaction('application') as tx:code=tx.fetchone('SELECT code FROM pricing.promotions WHERE id=%s',(promotion['id'],))['code']
    price=app.pricing.quote([{'variant_id':variants[1],'quantity':3},{'variant_id':variants[0],'quantity':2}],coupon_code=code,customer_id=buyer['id'],actor=buyer,command_id=str(uuid.uuid4()))
    ordered=sorted(price['lines'],key=lambda l:l['variant_id']);weights=[l['gross_minor'] for l in ordered];den=sum(weights)
    floors=[3*w//den for w in weights];remaining=3-sum(floors)
    rank=sorted(range(len(weights)),key=lambda i:(-(3*weights[i]%den),ordered[i]['variant_id']))
    for i in rank[:remaining]:floors[i]+=1
    assert [l['discount_minor'] for l in ordered]==floors
    for line,discount in zip(ordered,floors):
        q,r=divmod(discount,line['quantity']);expected=[q+(i<r) for i in range(line['quantity'])]
        assert [u['discount_minor'] for u in line['units']]==expected
        assert sum(u['net_minor'] for u in line['units'])==line['net_minor']==line['gross_minor']-discount
    address={'recipient':'Demo Customer','line1':'100 Example Street','city':'Demo City','region':'CA','postal_code':'94105','country':'US'}
    tax=app.tax.calculate(str(uuid.uuid4()),address,price['lines'],501,actor=buyer,command_id=str(uuid.uuid4()))
    for actual in tax['lines']:
        expected=(actual['net_minor']*725+5000)//10000
        assert actual['tax_minor']==sum(u['tax_minor'] for u in actual['units'])==expected
        unit_weights=[u['net_minor'] for u in actual['units']];total=sum(unit_weights)
        allocation=[expected*w//total for w in unit_weights];left=expected-sum(allocation)
        ranking=sorted(range(len(unit_weights)),key=lambda i:(-(expected*unit_weights[i]%total),i))
        for i in ranking[:left]:allocation[i]+=1
        assert [u['tax_minor'] for u in actual['units']]==allocation
    assert tax['shipping_tax_minor']==(501*725+5000)//10000
    assert tax['tax_minor']==sum(l['tax_minor'] for l in tax['lines'])+tax['shipping_tax_minor']


def test_category_route_and_order_return_reader_owning_scope(instance):
    app,buyer,foreign=instance
    lower=app.catalog.list_public_products({'category':'home'})
    original=app.catalog.list_public_products({'category':'Home'})
    assert lower['items'] and {p['id'] for p in lower['items']}=={p['id'] for p in original['items']}
    orders=app.orders.list_customer_orders(buyer['id'],actor=buyer)['items']
    actual=None
    for order in orders:
        returns=app.returns.list_order_returns(order['id'],actor=buyer)
        if returns['items']:
            assert all(row['owner_id']==buyer['id'] and row['order_id']==order['id'] and row['unit_ids'] for row in returns['items'])
            actual=order['id'];break
    assert actual, 'Earlier native lifecycle must have committed an actual return'
    with pytest.raises((ValueError,PermissionError)):app.returns.list_order_returns(actual,actor=foreign)


@pytest.mark.parametrize('accepted',[True,False])
def test_zero_refund_or_rejected_all_never_dispatches_money(instance,merchant,accepted,finance):
    app,buyer,_=instance
    suffix=uuid.uuid4().hex
    product=command(app,merchant,'merchant/catalog','create_product',{'name':'Zero-priced physical fixture','description':'Synthetic zero-item-value inspection fixture.','slug':'zero-fixture-'+suffix,'category':'Home'})
    variant=command(app,merchant,'merchant/catalog','upsert_variant',{'product_id':product['id'],'sku':'ZERO-'+suffix,'options':{'Color':'Test'},'weight_g':250,'dimensions_mm':[100,100,50]},product['version'])
    vid=variant.get('variant_id',variant['id'])
    command(app,merchant,'merchant/prices','set_price',{'variant_id':vid,'unit_minor':0},0)
    command(app,merchant,'merchant/stock','adjust_stock',{'variant_id':vid,'delta':2,'reason':'Attested synthetic zero-refund fixture'},0)
    product=app.catalog.get_merchant_product(product['id'],actor=merchant)
    media=command(app,merchant,'merchant/catalog','select_media',{'product_id':product['id'],'asset_id':app.config['assets'][0]['id'],'alt_text':'Synthetic zero item fixture'},product['version'])
    product=app.catalog.get_merchant_product(product['id'],actor=merchant)
    command(app,merchant,'merchant/catalog','publish',{'product_id':product['id']},product['version'])
    checkout,quote=prepared_checkout(app,buyer,vid)
    assert quote['subtotal_minor']==quote['discount_minor']==0 and quote['total_minor']>=50
    purchase=command(app,buyer,'purchases','submit',{'checkout_id':checkout['id'],'accepted_quote_id':quote['id'],'demo_instrument':'demo_success'})
    paid=wait_purchase(app,purchase['id'],buyer);order=app.orders.get_order(paid['order_id'],actor=merchant);unit=order['unit_records'][0]['unit_id']
    offered=staff_command(app,merchant,'quote_parcel',{'order_id':order['id'],'unit_ids':[unit]})
    planned=staff_command(app,merchant,'plan_shipment',{'order_id':order['id'],'unit_ids':[unit],'offer_id':offered['offers'][0]['id']},order['version']);assert planned['state']=='CREATED'
    shipment_id=planned['shipment_id']
    for operation in ('purchase_label','reserve_handover','confirm_handover','simulate_tracking'):
        shipment=app.shipping.get_shipment(shipment_id,actor=merchant);data={'shipment_id':shipment_id}
        if operation=='simulate_tracking':data['status']='DELIVERED'
        assert staff_command(app,merchant,operation,data,shipment['version'])['state']=='COMPLETE'
    returned=command(app,buyer,'returns','request_return',{'order_id':order['id'],'unit_ids':[unit],'reason':'changed_mind','requested_resolution':'refund'})
    assert returned['state']=='COMPLETE';return_id=returned['return_id']
    for operation,data in [('approve_return',{'decision':'approved','reason':'changed_mind'}),('record_received',{'unit_ids':[unit]}),('record_inspection',{'dispositions':[{'unit_id':unit,'accepted':accepted,'disposition':'sellable' if accepted else 'rejected'}]})]:
        current=app.returns.get_return(return_id,actor=merchant)
        assert staff_command(app,merchant,operation,{'return_id':return_id,**data},current['version'],family='returns')['state']=='COMPLETE'
    if accepted:
        proposal=app.returns.get_refund_plan(return_id,actor=finance);assert proposal['order_plan_id']
        with pytest.raises((ValueError,PermissionError)):app.returns.get_return(return_id,actor=finance)
        refund=staff_command(app,finance,'request_refund',{'return_id':return_id},family='refunds');assert wait_flow(app.refunds,refund['id'],finance)['state']=='SUCCEEDED'
        current=app.returns.get_return(return_id,actor=buyer);plan=app.orders.get_refund_plan(current['refund_plan']['order_plan_id'],actor=merchant)
        assert plan['amount_minor']==0 and plan['state']=='succeeded'
        assert current['state']=='closed'
    else:
        current=app.returns.get_return(return_id,actor=buyer)
        assert current['state']=='closed_rejected' and not current.get('refund_plan')
    with app.db.transaction('application') as tx:
        provider=tx.fetchone('SELECT provider_id FROM payments.attempts WHERE id=%s',(purchase['id'],))['provider_id']
        assert tx.fetchone("SELECT count(*) n FROM integrations.money_ledger WHERE object_id=%s AND kind='refund'",(provider,))['n']==0
        assert tx.fetchone("SELECT count(*) n FROM payments.effects WHERE kind='refund' AND attempt_id=%s",(purchase['id'],))['n']==0
    assert app.orders.get_order(order['id'],actor=buyer)['refunded_minor']==0
