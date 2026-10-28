"""Private loopback SMTP capture and authenticated local inbox; no external delivery."""
from __future__ import annotations
import argparse
import base64
from datetime import datetime,timezone
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import html
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socketserver
import stat
import threading
import time
from app.config import read_secret,strict_object
from app.models.notifications.mail_config import validate_mail_config

LIMIT=65536


class CaptureStore:
    def __init__(self,config,password):
        self.config,self.password=config,password
        directory=Path(config['capture']['directory'])
        if directory.is_symlink() or directory.resolve()!=directory:raise ValueError('private_mail_capture_directory_required')
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if stat.S_IMODE(directory.stat().st_mode)!=0o700 or directory.stat().st_uid!=os.getuid():raise ValueError('private_mail_capture_directory_required')
        self.directory=directory/'messages';self.directory.mkdir(mode=0o700,exist_ok=True)
        if self.directory.is_symlink() or stat.S_IMODE(self.directory.stat().st_mode)!=0o700:raise ValueError('private_mail_capture_directory_required')
        self.lock=threading.Lock()

    def authenticated(self,user,password,*,smtp=False):
        if not isinstance(user,str) or not isinstance(password,str):return False
        return hmac.compare_digest(user.encode('utf-8'),('local-capture' if smtp else 'operator').encode('ascii')) and hmac.compare_digest(password.encode('utf-8'),self.password.encode('utf-8'))

    def messages(self):
        result=[]
        for path in sorted(self.directory.glob('*.json'),key=lambda item:item.lstat().st_mtime_ns,reverse=True)[:100]:
            if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode)!=0o600 or path.stat().st_size>262144:raise ValueError('private_mail_capture_record_required')
            result.append(json.loads(path.read_text(),object_pairs_hook=strict_object))
        return sorted(result,key=lambda item:(item['received_at'],item['id']),reverse=True)[:100]

    def store(self,sender,recipients,raw):
        if len(raw)>LIMIT:raise ValueError('message_too_large')
        message=BytesParser(policy=policy.default).parsebytes(raw)
        identity=str(message.get('Message-ID',''))
        if not identity or len(identity)>254 or any(c in identity for c in '\r\n'):raise ValueError('message_id_required')
        key=hashlib.sha256(identity.encode()).hexdigest();digest=hashlib.sha256(raw).hexdigest()
        text=message.get_body(preferencelist=('plain',)) if message.is_multipart() else message
        rich=message.get_body(preferencelist=('html',)) if message.is_multipart() else None
        value={'id':key,'message_id':identity,'envelope_from':sender,'envelope_to':recipients,'subject':str(message.get('Subject','')),'received_at':datetime.now(timezone.utc).isoformat(),'content_sha256':digest,'text':text.get_content() if text is not None else '', 'html':rich.get_content() if rich is not None else ''}
        path=self.directory/(key+'.json')
        with self.lock:
            if path.exists():
                if path.is_symlink() or stat.S_IMODE(path.stat().st_mode)!=0o600:raise ValueError('private_mail_capture_record_required')
                previous=json.loads(path.read_text(),object_pairs_hook=strict_object)
                if previous['content_sha256']!=digest or previous['envelope_from']!=sender or previous['envelope_to']!=recipients:raise ValueError('message_id_conflict')
                return key
            if sum(1 for _ in self.directory.glob('*.json'))>=1000:raise ValueError('mail_capture_full')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        return key


class SMTPHandler(socketserver.StreamRequestHandler):
    timeout=5
    def reply(self,code,text):self.wfile.write((str(code)+' '+text+'\r\n').encode('ascii'));self.wfile.flush()

    def handle(self):
        self.request.settimeout(5);self.reply(220,'Local account mail capture')
        authenticated=False;sender=None;recipients=[];greeted=False;deadline=time.monotonic()+10
        try:
            for _ in range(200):
                self.request.settimeout(max(.001,deadline-time.monotonic()))
                if time.monotonic()>=deadline:return
                line=self.rfile.readline(513)
                if not line:return
                if len(line)>512 or not line.endswith(b'\r\n'):self.reply(500,'Invalid command');return
                try:command=line[:-2].decode('ascii')
                except UnicodeError:self.reply(500,'ASCII commands required');return
                verb,_,value=command.partition(' ');verb=verb.upper()
                if verb in ('EHLO','HELO'):
                    greeted=True;sender=None;recipients=[]
                    self.wfile.write(b'250-localhost\r\n250-SIZE 65536\r\n250 AUTH PLAIN\r\n');self.wfile.flush()
                elif verb=='QUIT':self.reply(221,'Closing connection');return
                elif verb=='NOOP':self.reply(250,'OK')
                elif verb=='RSET':sender=None;recipients=[];self.reply(250,'OK')
                elif verb=='AUTH' and greeted:
                    mechanism,_,encoded=value.partition(' ')
                    if mechanism.upper()!='PLAIN':self.reply(504,'Unsupported authentication');continue
                    if not encoded:
                        self.reply(334,'');encoded=self.rfile.readline(1025).strip().decode('ascii')
                    try:
                        parts=base64.b64decode(encoded,validate=True).decode('utf-8').split('\x00');user,password=parts[-2:]
                        authenticated=len(parts)==3 and not parts[0] and self.server.store.authenticated(user,password,smtp=True)
                    except (ValueError,UnicodeError):authenticated=False
                    self.reply(235 if authenticated else 535,'Authenticated' if authenticated else 'Authentication required')
                elif not authenticated:self.reply(530,'Authentication required')
                elif verb=='MAIL':
                    match=re.fullmatch(r'FROM:<([^<>\r\n]+)>(?: SIZE=[0-9]{1,8})?',value,re.I)
                    if not match or match.group(1).casefold()!=self.server.store.config['from_address'].casefold():self.reply(550,'Sender rejected');continue
                    sender=match.group(1);recipients=[];self.reply(250,'OK')
                elif verb=='RCPT':
                    match=re.fullmatch(r"TO:<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+)>",value,re.I)
                    if not sender or not match or len(recipients)>=1:self.reply(550,'Recipient rejected');continue
                    recipients.append(match.group(1));self.reply(250,'OK')
                elif verb=='DATA' and sender and recipients:
                    self.reply(354,'End data with dot');chunks=[];size=0
                    while True:
                        self.request.settimeout(max(.001,deadline-time.monotonic()))
                        if time.monotonic()>=deadline:return
                        part=self.rfile.readline(1001)
                        if not part or len(part)>1000 or not part.endswith(b'\r\n'):self.reply(552,'Message framing rejected');return
                        if part==b'.\r\n':break
                        if part.startswith(b'..'):part=part[1:]
                        size+=len(part)
                        if size>LIMIT:self.reply(552,'Message too large');return
                        chunks.append(part)
                    try:key=self.server.store.store(sender,list(recipients),b''.join(chunks));self.reply(250,'LOCAL_CAPTURED '+key)
                    except (ValueError,OSError):self.reply(550,'Capture rejected')
                    sender=None;recipients=[]
                else:self.reply(502,'Unsupported command')
        except (TimeoutError,OSError):return


class SMTPServer(socketserver.ThreadingMixIn,socketserver.TCPServer):
    allow_reuse_address=True;daemon_threads=True
    def __init__(self,address,store):
        self.store=store;self.slots=threading.BoundedSemaphore(8);super().__init__(address,SMTPHandler)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):request.close();return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()
    def handle_error(self,request,client_address):pass


class InboxHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(5)
    def log_message(self,*_):pass
    def do_GET(self):
        expected='127.0.0.1:'+str(self.server.server_port)
        if self.headers.get('Host')!=expected or self.headers.get('Origin') not in (None,'http://'+expected):return self.respond(403,{'error':'forbidden'})
        if self.path=='/health':return self.respond(200,{'ready':True})
        authorized=False
        try:
            scheme,_,encoded=self.headers.get('Authorization','').partition(' ')
            user,password=base64.b64decode(encoded,validate=True).decode('utf-8').split(':',1)
            authorized=scheme=='Basic' and self.server.store.authenticated(user,password)
        except (ValueError,UnicodeError):pass
        if not authorized:return self.respond(401,{'error':'authentication_required'},authenticate=True)
        if self.path not in ('/','/messages'):return self.respond(404,{'error':'not_found'})
        try:messages=self.server.store.messages()
        except (ValueError,OSError):return self.respond(503,{'error':'inbox_unavailable'})
        if self.path=='/messages':return self.respond(200,{'messages':messages})
        body='<!doctype html><html><head><meta charset="utf-8"><title>Private local mail</title></head><body><h1>Private local mail capture</h1><p>These messages were captured locally. External delivery has not occurred.</p>'
        for message in messages:
            body+='<article><h2>'+html.escape(message['subject'])+'</h2><p>To: '+html.escape(', '.join(message['envelope_to']))+'</p><pre>'+html.escape(message['text'])+'</pre>'
            for link in re.findall(r'https://[^\s<>]+',message['text']):body+='<p><a rel="noreferrer" href="'+html.escape(link,quote=True)+'">Open account link</a></p>'
            body+='</article>'
        body+='</body></html>';self.respond(200,body,content_type='text/html; charset=utf-8')
    def respond(self,status,value,*,authenticate=False,content_type='application/json'):
        raw=(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)).encode('utf-8');self.send_response(status)
        for key,item in {'Content-Type':content_type,'Content-Length':str(len(raw)),'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"}.items():self.send_header(key,item)
        if authenticate:self.send_header('WWW-Authenticate','Basic realm="Private local mail", charset="UTF-8"')
        self.end_headers();self.wfile.write(raw)


class InboxServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store):self.store=store;self.slots=threading.BoundedSemaphore(8);super().__init__(address,InboxHandler)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):request.close();return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()
    def handle_error(self,request,client_address):pass


def start_capture(config):
    validate_mail_config(config)
    if config['mode']!='local_capture':raise ValueError('local_mail_capture_configuration_required')
    store=CaptureStore(config,read_secret(config['capture']['password_ref']))
    smtp=SMTPServer(('127.0.0.1',config['capture']['smtp_port']),store)
    try:inbox=InboxServer(('127.0.0.1',config['capture']['http_port']),store)
    except BaseException:smtp.server_close();raise
    for server in (smtp,inbox):threading.Thread(target=server.serve_forever,daemon=True).start()
    return smtp,inbox


def main(argv=None):
    parser=argparse.ArgumentParser(description='Private loopback account email capture.')
    parser.add_argument('--config',required=True);args=parser.parse_args(argv)
    value=json.loads(Path(args.config).read_text(),object_pairs_hook=strict_object)
    config=value.get('mail')
    def expand(item):
        if type(item) is dict:return {k:expand(v) for k,v in item.items()}
        if isinstance(item,str) and re.fullmatch(r'\$\{SME_[A-Z0-9_]+\}',item):
            if item[2:-1] not in os.environ:raise ValueError('mail_runtime_binding_missing')
            return os.environ[item[2:-1]]
        return item
    smtp,inbox=start_capture(expand(config));stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    try:stop.wait()
    finally:
        for server in (smtp,inbox):server.shutdown();server.server_close()
    return 0


if __name__=='__main__':raise SystemExit(main())
