"""HTTP ingress, authentication publication and exact response contract regressions."""
import io
import json
from types import SimpleNamespace
from datetime import datetime,timezone
from decimal import Decimal
import uuid
import pytest
from app.controllers import http as ops

def profile():
    return {'format':'sme.portfolio-profile/1','profile':'demo-test','providers':{'mode':'deterministic','outbound_network':False},'environment_id':str(uuid.uuid4()),'merchant_id':str(uuid.uuid4()),'machine_principal_id':str(uuid.uuid4()),'release_id':'controlled-test','database_ref':'file:/not-mounted/runtime','simulator_callback_key_ref':'file:/not-mounted/callback','workload_credential_ref':'file:/not-mounted/workload','origin':'https://store.test:8443','proxy_peer':'127.0.0.1','identity_receipt_key_ref':'file:/not-mounted/identity','totp_root':'/not-mounted/totp','renderer_socket':'/not-mounted/renderer.sock'}


@pytest.mark.parametrize('value',[float('nan'),1.5,Decimal('0.1'),9007199254740992,datetime(2026,1,1),object(),{1:'value'}])
def test_wire_rejects_lossy_or_unselected_types(value):
    with pytest.raises(ValueError):ops.encode_wire(value)


def test_wire_serializes_only_explicit_exact_types():
    value={'amount_minor':Decimal('123'),'at':datetime(2026,1,1,tzinfo=timezone.utc),'id':uuid.UUID(int=1)}
    assert json.loads(ops.encode_wire(value))=={'amount_minor':123,'at':'2026-01-01T00:00:00.000000Z','id':'00000000-0000-0000-0000-000000000001'}


class Identity:
    def public_dto(self,_):raise ValueError('SELECT password FROM secret_table')
    def bootstrap_preauth(self):raise AssertionError('unexpected recovery')


class App:
    profile='demo-test';release_id='controlled-test';closed=False
    def __init__(self):self.identity=Identity()
    def actor(self,_):return {'kind':'customer','csrf':'current'}


def call(binding,path='/api/v1/session',method='GET',body=b'',**extra):
    environ={'REQUEST_METHOD':method,'PATH_INFO':path,'HTTP_HOST':'store.test:8443','REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_PROTO':'https','HTTP_X_FORWARDED_HOST':'store.test:8443','HTTP_COOKIE':'__Host-sme_demo=current','wsgi.input':io.BytesIO(body),**extra};response={}
    output=b''.join(binding(environ,lambda status,headers:response.update(status=status,headers=headers)));return response,json.loads(output)


def test_unknown_exception_text_never_reaches_wire():
    response,body=call(ops.PortfolioDemoWSGI(App(),profile()))
    assert response['status'].startswith('503')
    assert body['meta']['operation_id'] is None
    assert body['error']['code']=='service_unavailable';assert b'SELECT' not in ops.encode_wire(body)


@pytest.mark.parametrize('path',['/api/v1/sandbox/staff','/api/v1/logout','/callbacks/stripe','/api/v1/inject_fault','/api/v1/reset'])
def test_removed_routes_deny_before_identity_or_domain(path):
    app=App();app.actor=lambda _:pytest.fail('identity reached by forbidden route')
    response,body=call(ops.PortfolioDemoWSGI(app,profile()),path)
    assert response['status'].startswith('404')


def test_auth_completion_precedes_cookie_publication():
    app=App();calls=[]
    result=SimpleNamespace(credential='new-native-credential',public={'actor':{'kind':'customer'}},clear_auth=False,clear_challenge=False,clear_preauth=True,preauth_credential=None,challenge_credential=None)
    app.identity.bootstrap_guest=lambda **_:result
    app.complete_authentication=lambda credential:calls.append(('native_cart_association',credential))
    binding=ops.PortfolioDemoWSGI(app,profile());original=binding._identity_cookies
    def cookies(value,headers):calls.append(('cookies',value.credential));original(value,headers)
    binding._identity_cookies=cookies
    response,body=call(binding,'/api/v1/auth/guest','POST',b'{}',HTTP_ORIGIN='https://store.test:8443',CONTENT_TYPE='application/json',CONTENT_LENGTH='2',HTTP_X_CSRF_TOKEN='current')
    assert response['status'].startswith('200');assert calls==[('native_cart_association','new-native-credential'),('cookies','new-native-credential')]


@pytest.mark.parametrize('slug',['seed','debug','reset','provision','migrate','simulator'])
def test_maintenance_words_remain_valid_published_product_pages_and_existing_catalog_reads(slug):
    class Published(App):
        def catalog_view(self, **kwargs):
            product={'id':'published','slug':slug,'name':'Synthetic object','variants':[]}
            return product if kwargs.get('slug') else {'products':[product],'categories':[]}
    class Renderer:
        def render(self,path,initial):assert path=='/products/'+slug;return '<main>Synthetic published object</main>'
    binding=ops.PortfolioDemoWSGI(Published(),profile(),Renderer());response={}
    environ={'REQUEST_METHOD':'GET','PATH_INFO':'/products/'+slug,'HTTP_HOST':'store.test:8443','REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_PROTO':'https','HTTP_X_FORWARDED_HOST':'store.test:8443','wsgi.input':io.BytesIO()}
    body=b''.join(binding(environ,lambda status,headers:response.update(status=status,headers=headers)))
    assert response['status'].startswith('200') and body==b'<main>Synthetic published object</main>'
    response,value=call(binding,'/api/v1/catalog/'+slug)
    assert response['status'].startswith('200') and value['data']['slug']==slug
    response,body=call(binding,'/api/v1/catalog/'+slug,'POST',b'{}')
    assert response['status'].startswith('404')


@pytest.mark.parametrize('path',['/api/v1/cart/debug','/api/v1/seed','/api/v1/catalog/seed/commands'])
def test_maintenance_api_word_filter_does_not_admit_any_new_route(path):
    app=App();app.actor=lambda _:pytest.fail('maintenance route reached identity')
    response,body=call(ops.PortfolioDemoWSGI(app,profile()),path)
    assert response['status'].startswith('404')




# Exercise the real HTTP boundary independently of native owner tests. These
# doubles deliberately cannot mint sessions or provision privileged accounts.
def account_call(binding, body=None, path='/api/v1/auth/register', **headers):
    body={'full_name':'Ada Shopper','email':'ada@example.test','password':'correct horse battery','password_confirmation':'correct horse battery'} if body is None else body
    raw=json.dumps(body).encode()
    return call(binding,path,'POST',raw,**{'HTTP_ORIGIN':'https://store.test:8443','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),'HTTP_X_CSRF_TOKEN':'current','HTTP_IDEMPOTENCY_KEY':str(uuid.uuid4()),**headers})


def pending_result():
    return {'state':'verification_pending','message':'Check your email.','operation_id':str(uuid.uuid4()),'delivery_status':'REQUEST_ACCEPTED'}


def test_registration_response_is_pending_and_cannot_publish_authenticated_cookies():
    app=App();seen=[]
    app.identity.register_customer=lambda **kwargs:seen.append(kwargs) or pending_result()
    app.complete_authentication=lambda _:pytest.fail('account action minted a session')
    response,body=account_call(ops.PortfolioDemoWSGI(app,profile()))
    assert response['status'].startswith('202')
    assert body['data']['state']=='verification_pending' and 'actor' not in body['data']
    assert not any(name=='Set-Cookie' for name,_ in response['headers'])
    assert seen[0]['preauth_credential']=='current' and seen[0]['csrf']=='current'
    assert not {'principal_id','roles','merchant_id','active'} & set(seen[0])


@pytest.mark.parametrize('field',['roles','kind','principal_id','merchant_id','account_id','active','email_verified_at','verified'])
def test_public_signup_rejects_all_privilege_and_identity_injection(field):
    app=App();app.identity.register_customer=lambda **_:pytest.fail('injected request reached model')
    body={'full_name':'Ada Shopper','email':'ada@example.test','password':'correct horse battery','password_confirmation':'correct horse battery',field:'untrusted'}
    response,value=account_call(ops.PortfolioDemoWSGI(app,profile()),body)
    assert response['status'].startswith('422') and value['error']['code']=='unknown_auth_fields'


@pytest.mark.parametrize('headers,code',[({'HTTP_ORIGIN':'https://evil.test'},'origin_denied'),({'HTTP_X_CSRF_TOKEN':'wrong'},'csrf_denied'),({'HTTP_IDEMPOTENCY_KEY':''},'invalid_operation_id')])
def test_account_actions_retain_origin_csrf_and_idempotency_guards(headers,code):
    app=App();app.identity.register_customer=lambda **_:pytest.fail('unprotected request reached model')
    response,value=account_call(ops.PortfolioDemoWSGI(app,profile()),**headers)
    assert value['error']['code']==code and response['status'].startswith('422' if code=='invalid_operation_id' else '403')


def test_account_errors_expose_only_selected_field_errors():
    from app.models.identity.errors import IdentityError
    app=App()
    def invalid(**_):raise IdentityError('password_confirmation_mismatch',422,{'password_confirmation':'Passwords must match.','internal':'private context'})
    app.identity.register_customer=invalid
    response,value=account_call(ops.PortfolioDemoWSGI(app,profile()))
    assert response['status'].startswith('422')
    assert value['error']['details']=={'field_errors':{'password_confirmation':'Passwords must match.'}}


@pytest.mark.parametrize('leak',[{'token':'sensitive'},{'password':'sensitive'},{'credential':'sensitive'},{'actor':{'kind':'customer'}}])
def test_account_owner_cannot_leak_credentials_or_a_session_through_response(leak):
    app=App();app.identity.register_customer=lambda **_:dict(pending_result(),**leak)
    response,value=account_call(ops.PortfolioDemoWSGI(app,profile()))
    assert response['status'].startswith('503') and value['error']['code']=='service_unavailable'
    assert b'sensitive' not in ops.encode_wire(value)


def test_verification_response_requires_explicit_sign_in_without_session_publication():
    app=App();app.identity.verify_email=lambda **_:{'state':'email_verified','message':'Email verified. Sign in to continue.'}
    app.complete_authentication=lambda _:pytest.fail('verification minted a session')
    response,value=account_call(ops.PortfolioDemoWSGI(app,profile()),{'token':'x'*43},'/api/v1/auth/verify-email')
    assert response['status'].startswith('200') and value['data']['state']=='email_verified'
    assert not any(name=='Set-Cookie' for name,_ in response['headers'])


def test_mail_status_passes_only_current_carrier_and_requires_exact_query():
    app=App();seen=[];operation=str(uuid.uuid4())
    app.identity.get_account_action_status=lambda value,**kwargs:seen.append((value,kwargs)) or pending_result()
    binding=ops.PortfolioDemoWSGI(app,profile())
    response,value=call(binding,'/api/v1/auth/action-status',QUERY_STRING='operation_id='+operation)
    assert response['status'].startswith('200') and seen[0][0]==operation
    assert seen[0][1]['preauth_credential']=='current' and seen[0][1]['csrf']=='current'
    response,value=call(binding,'/api/v1/auth/action-status',QUERY_STRING='operation_id='+operation+'&carrier_id=other')
    assert response['status'].startswith('422') and len(seen)==1


@pytest.mark.parametrize('path',['/register','/verify-email','/forgot-password','/reset-password'])
def test_account_pages_support_direct_navigation_and_refresh(path):
    class Published(App):
        def catalog_view(self):return {'products':[],'categories':[]}
    class Renderer:
        def render(self,rendered_path,initial):assert rendered_path==path;return '<main>Account access</main>'
    binding=ops.PortfolioDemoWSGI(Published(),profile(),Renderer())
    for _ in range(2):
        response={}
        environ={'REQUEST_METHOD':'GET','PATH_INFO':path,'HTTP_HOST':'store.test:8443','REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_PROTO':'https','HTTP_X_FORWARDED_HOST':'store.test:8443','wsgi.input':io.BytesIO()}
        body=b''.join(binding(environ,lambda status,headers:response.update(status=status,headers=headers)))
        assert response['status'].startswith('200') and body==b'<main>Account access</main>'


@pytest.mark.parametrize('path',list(ops.ACCOUNT_SCHEMAS))
def test_every_public_account_mutation_has_a_separate_route_rate_limit(path):
    binding=ops.PortfolioDemoWSGI(App(),profile())
    request={'PATH_INFO':path,'HTTP_X_FORWARDED_FOR':'192.0.2.7'}
    capacity=12 if path.endswith('/verify-email') else 6
    for _ in range(capacity):binding._rate(request,'POST')
    with pytest.raises(ops.HttpProblem) as rejected:binding._rate(request,'POST')
    assert rejected.value.code=='authentication_rate_limited' and rejected.value.status==429



def test_public_account_backoff_header_covers_the_durable_rate_window():
    app=App();app.identity.register_customer=lambda **_:pending_result()
    binding=ops.PortfolioDemoWSGI(app,profile())
    for _ in range(6):account_call(binding)
    response,value=account_call(binding)
    assert response['status'].startswith('429')
    assert ('Retry-After','900') in response['headers']



def mfa_app():
    app=App();calls=[]
    app.identity.resolve_challenge=lambda _:{'kind':'preauth','csrf':'current','challenge':{'id':'pending-mfa'}}
    app.identity.logout=lambda **kwargs:calls.append(kwargs)
    fresh=SimpleNamespace(public={'csrf_token':'fresh-csrf','epoch':0,'session_generation':str(uuid.uuid4()),'absolute_expires_at':'2026-12-01T00:00:00Z'},credential=None,preauth_credential='fresh-preauth',challenge_credential=None,clear_auth=False,clear_challenge=True,clear_preauth=False)
    app.identity.bootstrap_preauth=lambda:fresh
    return app,calls


def test_public_customer_form_explicitly_cancels_pending_mfa_and_returns_fresh_carrier():
    app,calls=mfa_app();app.identity.register_customer=lambda **_:pytest.fail('MFA csrf was passed to customer carrier')
    response,value=account_call(ops.PortfolioDemoWSGI(app,profile()),HTTP_COOKIE='__Host-sme_demo_pre=old-preauth; __Secure-sme_demo_challenge=pending-cookie')
    assert response['status'].startswith('401') and value['error']['code']=='customer_preauth_required'
    assert calls==[{'preauth_credential':'old-preauth','challenge_credential':'pending-cookie'}]
    assert value['error']['details']['preauth']['csrf_token']=='fresh-csrf'
    cookies=[value for name,value in response['headers'] if name=='Set-Cookie']
    assert any('__Host-sme_demo_pre=fresh-preauth;' in value and 'HttpOnly; Secure' in value for value in cookies)
    assert any('__Secure-sme_demo_challenge=;' in value and 'Max-Age=0' in value for value in cookies)


def test_background_account_status_never_cancels_merchant_mfa():
    app,calls=mfa_app();app.identity.get_account_action_status=lambda *_args,**_kwargs:pytest.fail('challenge read entered customer carrier')
    response,value=call(ops.PortfolioDemoWSGI(app,profile()),'/api/v1/auth/action-status',HTTP_COOKIE='__Host-sme_demo_pre=old-preauth; __Secure-sme_demo_challenge=pending-cookie',QUERY_STRING='operation_id='+str(uuid.uuid4()))
    assert response['status'].startswith('409') and value['error']['code']=='mfa_in_progress' and not calls
    assert not any(name=='Set-Cookie' for name,_ in response['headers'])



def test_merchant_returns_route_uses_the_native_supported_owner_signature():
    app=App();seen=[]
    def list_returns(*,actor):seen.append(actor['id']);return {'items':[],'next_cursor':None}
    unrelated=lambda *_args,**_kwargs:pytest.fail('unrelated owner reached')
    app.checkout=SimpleNamespace(get_checkout=unrelated);app.purchase=SimpleNamespace(get_status=unrelated);app.orders=SimpleNamespace(get_order=unrelated)
    app.returns=SimpleNamespace(list_returns=list_returns,get_return=unrelated)
    actor={'id':str(uuid.uuid4()),'kind':'staff','roles':['merchant_admin']}
    result=ops.CommerceController(app).read('/api/v1/merchant/returns',{},actor)
    assert result=={'returns':[],'next_cursor':None} and seen==[actor['id']]



def test_server_failure_diagnostics_never_include_exception_arguments(capsys):
    response,value=call(ops.PortfolioDemoWSGI(App(),profile()))
    diagnostic=capsys.readouterr().err
    assert response['status'].startswith('503')
    assert 'SELECT' not in diagnostic and 'password' not in diagnostic and 'secret_table' not in diagnostic
    event=json.loads(diagnostic)
    assert event['event']=='http_failure' and event['error_type']=='ValueError'
    assert set(event['tracepoint'])=={'file','function','line'}
