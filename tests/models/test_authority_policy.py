"""Finite role policy table checks; current native identity/owner guards are separate."""
from pathlib import Path
from app.models import authorization as a
ROLES=('guest','customer','catalog_editor','stock_operator','support','finance','merchant_admin')
def snapshots(role):
 data={'roles':[role],'account_epoch':1,'auth_strength':'password+totp' if role in a.PortfolioDemoPolicy.STAFF_ROLES else 'password'}
 return {'policy':{'data':{'policy_revision':'pf-policy-1'}},'principal':{'data':dict(data)},'session':{'data':dict(data)}}
def allowed(role,owner,op,resource=True):return a.PortfolioDemoPolicy.evaluate(snapshots(role),owner,op,resource_allowed=resource)
def test_exact_application_workflow_admission_matrix():
 operations={'application.fulfillment.submit':{'stock_operator','merchant_admin'},'application.cancellation.submit':{'guest','customer','support','merchant_admin'},'application.return.submit':{'guest','customer','support','merchant_admin'},'application.return.manage':{'support','merchant_admin'},'application.refund.submit':{'finance','merchant_admin'}}
 for op,roles in operations.items():
  for role in ROLES:assert allowed(role,'application',op) is (role in roles)
 assert allowed('merchant_admin','checkout','checkout.begin') is False

def test_parcel_quotes_add_only_stock_operator_to_buyers():
 for role in ROLES:assert allowed(role,'shipping','shipping.quote') is (role in {'guest','customer','stock_operator','merchant_admin'})

def test_unknown_owner_operation_current_policy_and_literal_resource_deny():
 assert allowed('merchant_admin','orders','application.refund.submit') is False
 assert allowed('merchant_admin','application','application.arbitrary') is False
 assert allowed('finance','application','application.refund.submit',1) is False
 for mutation in ('policy','roles','mfa'):
  state=snapshots('finance')
  if mutation=='policy':state['policy']['data']['policy_revision']='other'
  elif mutation=='roles':state['session']['data']['roles']=['support']
  else:state['session']['data']['auth_strength']='password'
  assert a.PortfolioDemoPolicy.evaluate(state,'application','application.refund.submit',resource_allowed=True) is False

def test_native_order_reader_role_matrix_keeps_catalog_and_stock_boundaries():
 expected={'shipping.list_order_shipments':{'guest','customer','support','stock_operator','finance','merchant_admin'},'returns.list_order_returns':{'guest','customer','support','finance','merchant_admin'}}
 for operation,roles in expected.items():
  for role in ROLES:assert allowed(role,operation.split('.')[0],operation) is (role in roles)
