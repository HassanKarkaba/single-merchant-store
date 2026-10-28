"""Actual portfolio composition tests. Requires an independently attested demo-test instance.

No native/provider qualifications, public faults, permission stubs, or destructive resets.
Credentials stay in the private operator fixture; these tests do not capture authentication.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid

import pytest

from app.config import ROOT
from app.models import identity, disposable as guard_module
from app.models.store import Store
from app.controllers.commerce import CommerceController

pytestmark = pytest.mark.native

@pytest.fixture(scope='module')
def instance():
    file=os.environ.get('SME_TEST_CONFIG')
    if not file:pytest.fail('BLOCKED: explicit protected demo-test config required')
    config=json.loads(Path(file).read_text())
    if config.get('profile')!='demo-test':pytest.fail('BLOCKED: destructive demo target denied')
    dsn=identity.read_protected_file(config['database_ref'][5:]).strip()
    guard_module.DisposableResourceGuard(config['disposable_proof_file'],run_id=os.environ['SME_TEST_RUN_ID'],environment_id=config['environment_id'],profile='demo-test').attest_dsn(dsn)
    app=Store(dsn,profile='demo-test',config=config)
    accounts=json.loads(identity.read_protected_file(config['test_accounts_ref'][5:]))
    if accounts['profile']!='demo-test' or accounts['run_id']!=os.environ['SME_TEST_RUN_ID']:pytest.fail('BLOCKED: account fixture identity mismatch')
    def login(key):
        account=accounts[key];pre=app.identity.bootstrap_preauth()
        result=app.identity.authenticate(account['username'],account['password'],preauth_credential=pre.preauth_credential,csrf=pre.public['csrf_token'],source_key='retained-pf-test-'+key)
        app.complete_authentication(result.credential)
        return app.actor(result.credential)
    return app,login('customerA'),login('customerB')


def command(app,actor,path,operation,data,revision=None,key=None):
    body={'operation':operation,'input':data,'command_id':key or str(uuid.uuid4())}
    if revision is not None:body['expected_version']=revision
    return CommerceController(app).dispatch('/api/v1/'+path+'/commands',body,actor)


def prepared_checkout(app,actor,variant,quantity=1):
    cart=app.cart_view(actor)
    for line in cart['lines']:
        cart=command(app,actor,'cart','remove_line',{'variant_id':line['variant_id']},cart['version'])
    cart=command(app,actor,'cart','set_line',{'variant_id':variant,'quantity':quantity},cart['version'])
    checkout=command(app,actor,'checkout','begin',{'cart_revision':cart['version']})
    checkout=command(app,actor,'checkout','address',{'checkout_id':checkout['id'],'email':'journey@review.invalid','address':{'recipient':'Demo Customer','line1':'100 Example Street','city':'Demo City','region':'MI','postal_code':'48104','country':'US'}},checkout['version'])
    checkout=command(app,actor,'checkout','evaluate',{'checkout_id':checkout['id']})
    assert len(checkout['delivery_offers'])==2
    checkout=command(app,actor,'checkout','delivery',{'checkout_id':checkout['id'],'offer_id':checkout['delivery_offers'][0]['id']},checkout['version'])
    checkout=command(app,actor,'checkout','evaluate',{'checkout_id':checkout['id']});quote=checkout['quote']
    checkout=command(app,actor,'checkout','accept_quote',{'checkout_id':checkout['id'],'quote_id':quote['id'],'total_minor':quote['total_minor']},checkout['version'])
    return checkout,quote


def wait_purchase(app,id,actor,seconds=25):
    deadline=time.monotonic()+seconds;outcome={};status={}
    while time.monotonic()<deadline:
        status=app.purchase.get_status(id,actor=actor)
        if status['state']=='PAID' and not status['pending_followups']:return status
        if status['state'] in ('DECLINED','CANCELED','EXPIRED','FAILED'):return status
        # Native coordinator acquires the same durable lease used by the worker.
        outcome=app.purchase.run(id)
        time.sleep(.1)
    pytest.fail('Native purchase failed to converge: '+repr({'outcome':outcome,'state':status.get('state'),'steps':status.get('progress')}))


def test_pf_actual_paid_purchase_replay_and_foreign_ownership(instance):
    app,buyer,foreign=instance
    variant=next(v['id'] for p in app.catalog_view()['products'] for v in p['variants'] if v['available']>=2)
    checkout,quote=prepared_checkout(app,buyer,variant,2)
    key=str(uuid.uuid4());data={'checkout_id':checkout['id'],'accepted_quote_id':quote['id'],'demo_instrument':'demo_success'}
    first=command(app,buyer,'purchases','submit',data,key=key)
    replay=command(app,buyer,'purchases','submit',data,key=key)
    assert replay['id']==first['id']
    paid=wait_purchase(app,first['id'],buyer)
    assert paid['state']=='PAID' and paid['order']['total_minor']==quote['total_minor']
    order=app.orders.get_order(paid['order_id'],actor=buyer)
    assert sum(u['net_minor']+u['tax_minor'] for l in order['lines'] for u in l['units'])+quote['shipping_minor']+quote['shipping_tax_minor']==order['total_minor']
    with pytest.raises((PermissionError,ValueError)):app.orders.get_order(order['id'],actor=foreign)
    with pytest.raises((PermissionError,ValueError)):app.purchase.get_status(first['id'],actor=foreign)
    with pytest.raises((PermissionError,ValueError)):command(app,buyer,'purchases','submit',{**data,'demo_instrument':'demo_decline'},key=key)
    # Only this private test oracle reads simulator ledger; production owners use supported ports.
    with app.db.transaction('application') as tx:
        attempts=tx.execute('SELECT count(*) n FROM application.purchase_workflows WHERE checkout_id=%s',(checkout['id'],)).fetchone()['n']
        captures=tx.execute("SELECT count(*) n FROM integrations.money_ledger WHERE kind='capture' AND object_id=(SELECT provider_id FROM payments.attempts WHERE id=%s)",(first['id'],)).fetchone()['n']
    assert attempts==1 and captures==1


def test_pf_unknown_command_and_get_do_not_create_guest_or_cart(instance):
    app,buyer,_=instance
    with app.db.transaction('application') as tx:
        before=tx.execute('SELECT count(*) n FROM application.cart_associations').fetchone()['n']
    assert CommerceController(app).read('/api/v1/catalog',{},None)['products']
    with pytest.raises((PermissionError,ValueError)):CommerceController(app).dispatch('/api/v1/merchant/reset/commands',{'operation':'reset','command_id':str(uuid.uuid4()),'input':{}},buyer)
    with app.db.transaction('application') as tx:
        after=tx.execute('SELECT count(*) n FROM application.cart_associations').fetchone()['n']
    assert before==after


def test_pf_forged_workload_has_no_business_admission(instance):
    app,buyer,_=instance
    fake=app.workload_actor(str(uuid.uuid4()),lease_token=str(uuid.uuid4()),dispatch_permit_id=str(uuid.uuid4()))
    with pytest.raises((PermissionError,ValueError)):
        with app.db.transaction('inventory',authority_guard=lambda tx:app.authorize(tx,fake,'inventory.adjust_stock',str(uuid.uuid4()))):pass
    with pytest.raises((PermissionError,ValueError)):
        with app.db.transaction('application',authority_guard=lambda tx:app.authorize(tx,{**buyer,'roles':['merchant_admin']},'application.seed',buyer['id'])):pass

@pytest.mark.parametrize('instrument,fault',[('demo_success','lost_reply'),('demo_pending',None)])
def test_pf_adverse_original_capture_reconciliation(instance,instrument,fault):
    app,buyer,_=instance
    variant=next(v['id'] for p in app.catalog_view()['products'] for v in p['variants'] if v['available']>=1)
    checkout,quote=prepared_checkout(app,buyer,variant)
    key=str(uuid.uuid4());primary=sys.modules[type(app).__module__]
    attempt=primary.step_id(buyer['id']+':'+key,'purchase')
    if fault:
        # A separate explicit maintenance object may install a run/case/effect-scoped
        # plan only after its real disposable guard re-attests this same target.
        control=type(app)(app._instance_dsn,profile='demo-test',config=app.config,maintenance=True)
        control.initialize()
        plan=control.inject_test_fault('stripe','capture',fault,run_id=os.environ['SME_TEST_RUN_ID'],case_id='PAC22-lost-capture',effect_key=app.payments.effect_id(attempt,'capture'))
        assert plan['remaining']==1
    body={'checkout_id':checkout['id'],'accepted_quote_id':quote['id'],'demo_instrument':instrument}
    submitted=command(app,buyer,'purchases','submit',body,key=key)
    assert submitted['id']==attempt
    paid=wait_purchase(app,attempt,buyer)
    assert paid['state']=='PAID' and not paid['pending_followups']
    assert command(app,buyer,'purchases','submit',body,key=key)['id']==attempt
    with app.db.transaction('application') as tx:
        captured=tx.execute("SELECT count(*) n,sum(amount_minor) amount FROM integrations.money_ledger WHERE kind='capture' AND object_id=(SELECT provider_id FROM payments.attempts WHERE id=%s)",(attempt,)).fetchone()
        effects=tx.execute("SELECT count(*) n FROM integrations.effects WHERE provider='stripe' AND effect_key=%s",(app.payments.effect_id(attempt,'capture'),)).fetchone()['n']
        if fault:
            remaining=tx.execute('SELECT remaining FROM integrations.fault_plans WHERE id=%s',(plan['id'],)).fetchone()['remaining']
            assert remaining==0
    assert captured['n']==1 and captured['amount']==quote['total_minor'] and effects==1
