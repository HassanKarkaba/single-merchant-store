"""HTTPS-proxy ingress, authentication requests and detached view rendering."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from decimal import Decimal
from http.cookies import SimpleCookie
import uuid
import os
from pathlib import Path
import re
import secrets
import socket
import threading
from urllib.parse import parse_qs, urlsplit
from app.config import ROOT, strict_object
from .commerce import CommerceController
from .accounts import ACCOUNT_SCHEMAS, AccountAccessController
AUTH_SCHEMAS={'/api/v1/auth/login':{'username','password'},'/api/v1/auth/mfa':{'challenge_id','code'},'/api/v1/auth/guest':set(),'/api/v1/auth/logout':set(),'/api/v1/auth/touch':{'activity_id'}}
PAGE_ROUTES=__import__('re').compile(r'/(?:|search|cart|about|policies|register|verify-email|forgot-password|reset-password|account(?:/(?:orders|privacy))?|merchant(?:/(?:catalog|pricing|promotions|stock|orders|returns|finance|settings|investigation))?|categories/[A-Za-z0-9_-]+|products/[a-z0-9-]+|(?:checkout|purchases|orders|returns|operations)/[0-9a-f-]{36})')
class HttpProblem(ValueError):
    def __init__(self,code,status):self.code,self.status=code,status;super().__init__(code)

class PortfolioDemoWSGI:
    """Closed PF HTTP adapter; Caddy is the sole public HTTPS listener."""
    AUTH='__Host-sme_demo';PRE='__Host-sme_demo_pre';CHALLENGE='__Secure-sme_demo_challenge'
    def __call__(self,environ,start_response):
        if not self._slots.acquire(blocking=False):return self._respond(start_response,503,{'error':{'code':'overload','message':'Please try again shortly','details':{}}},[])
        import time
        self._timing.started=time.perf_counter()
        cookies=[]
        try:
            method=environ.get('REQUEST_METHOD');path=environ.get('PATH_INFO','');host=environ.get('HTTP_HOST','')
            if host!=self.authority or environ.get('REMOTE_ADDR')!=self.proxy_peer or environ.get('HTTP_X_FORWARDED_PROTO')!='https' or environ.get('HTTP_X_FORWARDED_HOST')!=self.authority or environ.get('HTTP_FORWARDED') or ',' in environ.get('HTTP_X_FORWARDED_FOR',''):raise HttpProblem('untrusted_ingress',403)
            if method not in ('GET','HEAD','POST'):raise HttpProblem('method_not_allowed',405)
            if not isinstance(path,str) or len(path)>4096 or '\\' in path:raise HttpProblem('not_found',404)
            forbidden={'/api/v1/sandbox/staff','/api/v1/logout','/callbacks/stripe','/callbacks/easypost','/callbacks/ses'}
            public_catalog_read=method in ('GET','HEAD') and (path=='/api/v1/catalog' or re.fullmatch(r'/api/v1/catalog/[a-z0-9-]+',path) is not None)
            if path in forbidden or (path.startswith('/api/') and not public_catalog_read and any(part in path.lower().split('/') for part in ('inject_fault','complete_action','debug','reset','seed','provision','migrate','simulator'))):raise HttpProblem('not_found',404)
            self._rate(environ,method)
            jar=SimpleCookie()
            try:jar.load(environ.get('HTTP_COOKIE',''))
            except Exception:raise HttpProblem('invalid_cookie',401)
            credential=lambda name:jar[name].value if name in jar else None
            if path=='/health/live' and method in ('GET','HEAD'):return self._respond(start_response,200,{'data':{'alive':not self.app.closed}},cookies,head=method=='HEAD')
            if path=='/health/ready' and method in ('GET','HEAD'):
                ready=self.app.readiness();ready['renderer_ready']=bool(self.renderer and self.renderer.ready());ready['ready']=ready['ready'] and ready['renderer_ready'];return self._respond(start_response,200 if ready['ready'] else 503,{'data':ready},cookies,head=method=='HEAD')
            if path=='/api/v1/session' and method in ('GET','HEAD'):
                context=self._session_context(credential,cookies)
                return self._respond(start_response,200,{'data':self.app.identity.public_dto(context)},cookies,head=method=='HEAD')
            if path=='/api/v1/auth/action-status' and method in ('GET','HEAD'):
                context=self._session_context(credential,cookies)
                query=parse_qs(environ.get('QUERY_STRING',''),keep_blank_values=True)
                if set(query)!={'operation_id'} or len(query['operation_id'])!=1:raise HttpProblem('invalid_operation_id',422)
                if context.get('challenge'):raise HttpProblem('mfa_in_progress',409)
                result=self.accounts.status(query['operation_id'][0],carrier=credential(self.AUTH) or credential(self.PRE),csrf=context['csrf'],source_key=environ.get('HTTP_X_FORWARDED_FOR') or self.proxy_peer)
                return self._respond(start_response,200,{'data':result},cookies,head=method=='HEAD')
            if method=='POST':
                if environ.get('HTTP_ORIGIN')!=self.origin:raise HttpProblem('origin_denied',403)
                if environ.get('CONTENT_TYPE','').split(';')[0].lower()!='application/json':raise HttpProblem('media_type_denied',415)
                length=environ.get('CONTENT_LENGTH','')
                if not length.isascii() or not length.isdigit() or not 0<int(length)<=1048576:raise HttpProblem('body_limit',413)
                if environ.get('HTTP_TRANSFER_ENCODING'):raise HttpProblem('framing_denied',400)
                context=self._session_context(credential,cookies)
                if not secrets.compare_digest(context['csrf'],environ.get('HTTP_X_CSRF_TOKEN','')):raise HttpProblem('csrf_denied',403)
                raw=environ['wsgi.input'].read(int(length))
                if len(raw)!=int(length):raise HttpProblem('body_incomplete',400)
                body=json.loads(raw.decode('utf-8'),object_pairs_hook=strict_object,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid_json')))
                if not isinstance(body,dict):raise HttpProblem('closed_object_required',422)
                if path in ACCOUNT_SCHEMAS:
                    if set(body)!=ACCOUNT_SCHEMAS[path]:raise HttpProblem('unknown_auth_fields',422)
                    if context.get('challenge'):
                        # A challenge CSRF protects MFA, not the distinct PRE
                        # carrier. Cancel that native challenge explicitly; the
                        # standard 401 path issues a fresh customer form carrier.
                        self.app.identity.logout(preauth_credential=credential(self.PRE),challenge_credential=credential(self.CHALLENGE))
                        raise HttpProblem('customer_preauth_required',401)
                    result=self.accounts.submit(path,body,carrier=credential(self.AUTH) or credential(self.PRE),csrf=context['csrf'],source_key=environ.get('HTTP_X_FORWARDED_FOR') or self.proxy_peer,idempotency_key=environ.get('HTTP_IDEMPOTENCY_KEY'))
                    if path.endswith('/reset-password') and result['state']=='password_reset' and credential(self.AUTH):
                        try:self.app.identity.resolve_session(credential(self.AUTH))
                        except PermissionError:
                            fresh=self.app.identity.bootstrap_preauth();self._identity_cookies(fresh,cookies);cookies.extend(self._clear_auth())
                    status=202 if result['state'] in ('verification_pending','reset_requested','reset_pending') else 200
                    return self._respond(start_response,status,{'data':result},cookies)
                if path in AUTH_SCHEMAS:
                    if set(body)!=AUTH_SCHEMAS[path]:raise HttpProblem('unknown_auth_fields',422)
                    identity=self.app.identity;carrier=credential(self.AUTH) or credential(self.PRE)
                    if path.endswith('/login'):
                        result=identity.authenticate(body['username'],body['password'],preauth_credential=carrier,csrf=context['csrf'],source_key=environ.get('HTTP_X_FORWARDED_FOR',self.proxy_peer),old_credential=credential(self.AUTH))
                    elif path.endswith('/mfa'):result=identity.verify_staff_totp(body['challenge_id'],body['code'],challenge_credential=credential(self.CHALLENGE),preauth_credential=credential(self.PRE),csrf=context['csrf'])
                    elif path.endswith('/guest'):result=identity.bootstrap_guest(preauth_credential=credential(self.PRE),csrf=context['csrf'])
                    elif path.endswith('/touch'):result=identity.touch(credential(self.AUTH),body['activity_id'])
                    else:result=identity.logout(carrier,preauth_credential=credential(self.PRE),challenge_credential=credential(self.CHALLENGE))
                    if result.credential:self.app.complete_authentication(result.credential)
                    self._identity_cookies(result,cookies)
                    return self._respond(start_response,200,{'data':result.public},cookies)
                if context['kind']=='preauth':raise HttpProblem('unauthenticated',401)
                actor=self.app.actor(credential(self.AUTH));result=self.controller.dispatch(path,body,actor)
                pending=result.get('state') in ('ADMISSION_PENDING','AUTHORIZATION_PENDING','PAYMENT_ACTION','CAPTURE_PENDING','PENDING','UNKNOWN','REVIEW','REQUESTED','SENDING','REFUND_PENDING')
                return self._respond(start_response,202 if pending else 200,{'data':result},cookies)
            if path.startswith('/api/'):
                if path=='/api/v1/catalog' or re.fullmatch(r'/api/v1/catalog/[a-z0-9-]+',path):actor=None
                else:
                    context=self._session_context(credential,cookies)
                    if context['kind']=='preauth':raise HttpProblem('unauthenticated',401)
                    actor=self.app.actor(credential(self.AUTH))
                return self._respond(start_response,200,{'data':self.controller.read(path,parse_qs(environ.get('QUERY_STRING',''),keep_blank_values=True),actor)},cookies,head=method=='HEAD')
            if path.startswith('/assets/'):
                base=(ROOT/'dist/client').resolve();file=(base/path.removeprefix('/assets/')).resolve()
                if base not in file.parents or not file.is_file() or file.is_symlink():raise HttpProblem('not_found',404)
                import mimetypes
                return self._respond(start_response,200,file.read_bytes(),cookies,mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream',head=method=='HEAD')
            if not PAGE_ROUTES.fullmatch(path):raise HttpProblem('not_found',404)
            if not self.renderer:raise HttpProblem('renderer_unavailable',503)
            nonce=secrets.token_urlsafe(24);html=self.renderer.render(path+('?' + environ['QUERY_STRING'] if environ.get('QUERY_STRING') else ''),{'catalog':self.app.catalog_view(),'profile':self.app.profile,'sandbox':True,'nonce':nonce})
            return self._respond(start_response,200,html.encode(),cookies,mime='text/html; charset=utf-8',head=method=='HEAD',nonce=nonce)
        except Exception as error:
            if isinstance(error,(json.JSONDecodeError,UnicodeDecodeError)):error=HttpProblem('invalid_json',400)
            status=getattr(error,'status',None);code=getattr(error,'code',str(error)).lower()
            known={'customer_preauth_required':401,'mfa_in_progress':409,'password_reset_in_progress':409,'identity_version_conflict':409,'invalid_json':400,'unknown_auth_fields':422,'invalid_operation_id':422,'invalid_full_name':422,'invalid_email':422,'password_policy':422,'password_confirmation_mismatch':422,'verification_token_invalid':422,'verification_token_expired':410,'verification_token_used':409,'password_reset_token_invalid':422,'password_reset_token_expired':410,'password_reset_token_used':409,'email_verification_required':403,'authentication_rate_limited':429,'authentication_overload':429,'public_registration_overload':429,'account_email_unavailable':503,'account_mail_not_configured':503,'preauth_required':401,'invalid_source':422,'account_action_not_found':404,'not_found':404,'forbidden':403,'unauthenticated':401,'session_expired':401,'authentication_failed':401,'invalid_code':422,'challenge_expired':401,'csrf_denied':403,'version_conflict':409,'idempotency_conflict':409,'expected_version_required':422,'quote_expired':410,'invalid_input':422,'unknown_asset':422,'unsupported_operation':422,'authority_gate_closed_or_expired':401,'session_context_mismatch':401,'current_native_policy_allow_required':403,'native_session_rejected':401,'workload_obligation_denied':403}
            status=status or known.get(code,503)
            if code not in known and not isinstance(error,HttpProblem):code='service_unavailable';status=503
            if status==503 and code not in ('account_email_unavailable','account_mail_not_configured'):code='service_unavailable'
            details={}
            fields=getattr(error,'field_errors',None)
            if type(fields) is dict:
                safe_fields={key:value for key,value in fields.items() if key in ('full_name','email','password','password_confirmation','token') and isinstance(value,str) and len(value)<=240}
                if safe_fields:details['field_errors']=safe_fields
            if status==401:
                fresh=self.app.identity.bootstrap_preauth();self._identity_cookies(fresh,cookies);cookies.extend(self._clear_auth());dto=fresh.public;details['preauth']={key:dto[key] for key in ('csrf_token','epoch','session_generation','absolute_expires_at')};details['preauth']['expires_at']=details['preauth'].pop('absolute_expires_at')
            certainty='unknown' if getattr(error,'certainty',None)=='UNKNOWN' or (environ.get('REQUEST_METHOD')=='POST' and status>=500) else 'none'
            if status>=500:log_server_failure(error)
            messages={'customer_preauth_required':'Merchant verification was canceled. Submit this customer account form again.','mfa_in_progress':'Complete merchant verification before checking this account request.','invalid_full_name':'Enter your full name using 2 to 120 characters.','invalid_email':'Enter a valid email address, up to 254 bytes.','password_policy':'Use 12 to 128 characters for your password.','password_confirmation_mismatch':'Passwords must match.','verification_token_invalid':'This verification link is invalid, expired, or already used. Request a new link or sign in.','password_reset_token_invalid':'This reset link is invalid, expired, or already used. Request a new password reset.','email_verification_required':'Verify your email before signing in. You can request another verification email.','account_email_unavailable':'Account email is temporarily unavailable. Please try again.','account_mail_not_configured':'Account email is temporarily unavailable. Please try again.','authentication_rate_limited':'Too many account requests. Please wait before trying again.','invalid_operation_id':'Please submit this form again.','unknown_auth_fields':'The submitted account form contains unsupported fields.'}
            return self._respond(start_response,status,{'error':{'code':code,'message':messages.get(code,code.replace('_',' ').capitalize()),'details':details},'meta':{'request_id':secrets.token_hex(16),'profile':self.app.profile,'release_id':self.app.release_id,'effect_certainty':certainty,'operation_id':None}},cookies,retry_after=900 if status==429 and environ.get('PATH_INFO') in ACCOUNT_SCHEMAS else 60)
        finally:self._slots.release()
    def __init__(self,app,config,renderer=None):
        self.app,self.config,self.renderer=app,config,renderer;self.controller=CommerceController(app);self.accounts=AccountAccessController(app.identity);self.origin=config['origin'];self.authority=urlsplit(self.origin).netloc;self.proxy_peer=config['proxy_peer'];self._slots=threading.BoundedSemaphore(24);self._limiter={};self._rate_lock=threading.Lock();self._timing=threading.local()
        if urlsplit(self.origin).scheme!='https' or not self.authority:raise ValueError('exact_https_origin_required')
    def _clear_auth(self):return [self._cookie(self.AUTH,'',0),self._cookie(self.CHALLENGE,'',0,path='/api/v1/')]
    def _cookie(self,name,value,max_age,path='/'):
        return ('Set-Cookie',f'{name}={value}; Path={path}; Max-Age={max_age}; HttpOnly; Secure; SameSite=Lax')
    def _identity_cookies(self,result,cookies):
        if result.clear_auth:cookies.append(self._cookie(self.AUTH,'',0))
        if result.clear_challenge:cookies.append(self._cookie(self.CHALLENGE,'',0,path='/api/v1/'))
        if result.clear_preauth:cookies.append(self._cookie(self.PRE,'',0))
        if result.credential:cookies.append(self._cookie(self.AUTH,result.credential,86400 if result.public.get('actor',{}).get('kind')=='guest' else 14400))
        if result.preauth_credential:cookies.append(self._cookie(self.PRE,result.preauth_credential,600))
        if result.challenge_credential:cookies.append(self._cookie(self.CHALLENGE,result.challenge_credential,300,path='/api/v1/'))
    def _rate(self,environ,method):
        import time
        profile=self.app.profile;rate=50 if method in ('GET','HEAD') and profile=='demo-test' else 20 if profile=='demo-test' else 5 if method in ('GET','HEAD') else 1
        key=(environ.get('HTTP_X_FORWARDED_FOR',self.proxy_peer),'read' if method in ('GET','HEAD') else 'write');now=time.monotonic()
        with self._rate_lock:
            tokens,at=self._limiter.get(key,(float(rate*4),now));tokens=min(rate*4,tokens+(now-at)*rate)
            if tokens<1:raise HttpProblem('rate_limited',429)
            self._limiter[key]=(tokens-1,now)
            if method=='POST' and environ.get('PATH_INFO')=='/api/v1/purchases/commands':
                capacity=120 if profile=='demo-test' else 2;checkout_key=(key[0],'checkout');remaining,at=self._limiter.get(checkout_key,(float(capacity),now));remaining=min(float(capacity),remaining+(now-at)*capacity/60)
                if remaining<1:raise HttpProblem('checkout_rate_limited',429)
                self._limiter[checkout_key]=(remaining-1,now)
            if method=='POST' and environ.get('PATH_INFO') in ACCOUNT_SCHEMAS:
                route=environ['PATH_INFO'];capacity=12 if route.endswith('/verify-email') else 6
                account_key=(key[0],route);remaining,at=self._limiter.get(account_key,(float(capacity),now));remaining=min(float(capacity),remaining+(now-at)*capacity/900)
                if remaining<1:raise HttpProblem('authentication_rate_limited',429)
                self._limiter[account_key]=(remaining-1,now)
            if len(self._limiter)>1000:self._limiter={k:v for k,v in self._limiter.items() if now-v[1]<900}
    def _respond(self,start_response,status,data,cookies,mime='application/json; charset=utf-8',head=False,nonce=None,retry_after=60):
        import http
        if isinstance(data,dict):
            data=dict(data);data.setdefault('meta',{'request_id':secrets.token_hex(16),'profile':self.app.profile,'release_id':self.app.release_id,'effect_certainty':data.get('data',{}).get('effect_certainty','none') if isinstance(data.get('data'),dict) else 'none','operation_id':data['data'].get('operation_id') if isinstance(data.get('data'),dict) else None});body=encode_wire(data)
        else:body=data
        policy="default-src 'none'; script-src 'self'; style-src 'self'"+(f" 'nonce-{nonce}'" if nonce else '')+"; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        headers=[('Content-Type',mime),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer'),('Content-Security-Policy',policy),*cookies]
        import time
        if hasattr(self._timing,'started'):headers.append(('Server-Timing','app;dur='+format((time.perf_counter()-self._timing.started)*1000,'.3f')))
        if status==429:headers.append(('Retry-After',str(retry_after)))
        if status==405:headers.append(('Allow','GET, HEAD, POST'))
        start_response(str(status)+' '+http.HTTPStatus(status).phrase,headers);return [b'' if head else body]
    def _session_context(self,credential,cookies):
        if credential(self.AUTH):return self.app.actor(credential(self.AUTH))
        if credential(self.CHALLENGE):return self.app.identity.resolve_challenge(credential(self.CHALLENGE))
        if credential(self.PRE):return self.app.identity.resolve_session(credential(self.PRE))
        result=self.app.identity.bootstrap_preauth();self._identity_cookies(result,cookies)
        return {'kind':'preauth','csrf':result.public['csrf_token'],'epoch':result.public['epoch'],'session_generation':result.public['session_generation'],'idle_expires_at':result.public['idle_expires_at'],'absolute_expires_at':result.public['absolute_expires_at']}

class RendererClient:
    def __init__(self,socket_path):self.socket_path=socket_path
    def _request(self,path,data):
        import socket,http.client
        class UnixHTTP(http.client.HTTPConnection):
            def connect(this):this.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);this.sock.settimeout(5);this.sock.connect(self.socket_path)
        connection=UnixHTTP('localhost',timeout=5)
        try:
            body=json.dumps(data) if data else None;connection.request('POST' if data else 'GET',path,body=body,headers={'Content-Type':'application/json'});response=connection.getresponse();raw=response.read(2097153)
            if response.status!=200 or len(raw)>2097152:raise RuntimeError('renderer_contract_failure')
            return json.loads(raw)
        finally:connection.close()
    def ready(self):
        try:return self._request('/health',None)['ready'] is True
        except Exception:return False
    def render(self,path,initial):return self._request('/render',{'path':path,'initialData':initial})['html']

def encode_wire(value):return json.dumps(wire_value(value),ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')

def wire_value(value):
    if value is None or type(value) in (str,bool):return value
    if type(value) is int:
        if abs(value)>9007199254740991:raise ValueError('wire_integer_out_of_range')
        return value
    if isinstance(value,uuid.UUID):return str(value)
    if isinstance(value,datetime):
        if value.tzinfo is None or value.utcoffset() is None:raise ValueError('wire_aware_timestamp_required')
        return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    if isinstance(value,Decimal):
        if not value.is_finite() or value!=value.to_integral_value():raise ValueError('wire_exact_integer_required')
        return wire_value(int(value))
    if isinstance(value,(list,tuple)):return [wire_value(v) for v in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):raise ValueError('wire_string_keys_required')
        return {key:wire_value(v) for key,v in value.items()}
    raise ValueError('wire_unsupported_value')



def log_server_failure(error):
    """Keep operational diagnostics useful without request or credential data."""
    import sys,traceback
    frames=traceback.extract_tb(error.__traceback__)
    location=frames[-1] if frames else None
    value={'event':'http_failure','error_type':type(error).__name__}
    if location:value['tracepoint']={'file':Path(location.filename).name,'function':location.name,'line':location.lineno}
    sqlstate=getattr(error,'sqlstate',None)
    if isinstance(sqlstate,str) and re.fullmatch('[A-Z0-9]{5}',sqlstate):value['sqlstate']=sqlstate
    sys.stderr.write(json.dumps(value,separators=(',',':'))+'\n')
