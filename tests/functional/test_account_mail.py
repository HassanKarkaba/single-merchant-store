"""Private mail configuration, real loopback SMTP and bounded transport tests."""
import base64
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
import uuid
import pytest
from app.controllers.mail_capture import start_capture
from app.controllers.setup import configure_mail,private_file
from app.models.notifications.mail_config import validate_mail_config
from app.models.notifications.smtp_delivery import MailDeliveryError,send_account_mail
from app.models.notifications.templates import render_account_mail


def available_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def mail_configuration(tmp_path):
    tmp_path.chmod(0o700);profile=tmp_path/'profile.json';private_file(profile,{'origin':'https://localhost:8443'})
    configure_mail(profile);config=json.loads(profile.read_text())['mail']
    config['port']=config['capture']['smtp_port']=available_port();config['capture']['http_port']=available_port()
    return config


def payload():
    now=datetime.now(timezone.utc)
    return {'message_id':str(uuid.uuid4()),'purpose':'email_verification','recipient':'customer@review.invalid','token':'A'*43,'expires_at':(now+timedelta(minutes=30)).isoformat(),'full_name':'<Example Customer>','created_at':now.isoformat()}


def test_closed_mail_configuration_rejects_external_plaintext_and_auth_mismatch(tmp_path):
    config=mail_configuration(tmp_path);validate_mail_config(config)
    config['host']='mail.example.com'
    with pytest.raises(ValueError,match='private_loopback_capture_required'):validate_mail_config(config)
    config['mode']='smtp';config.pop('capture')
    with pytest.raises(ValueError,match='verified_smtp_tls_required'):validate_mail_config(config)
    config['security']='starttls';validate_mail_config(config)
    config['password_ref']=None
    with pytest.raises(ValueError,match='mail_authentication_required'):validate_mail_config(config)


def test_mail_configuration_is_idempotent_and_keeps_generated_secrets_private(tmp_path):
    config=mail_configuration(tmp_path);profile=tmp_path/'profile.json'
    before=profile.read_bytes();key=Path(config['queue_key_ref'][5:]);key_before=key.read_bytes()
    configure_mail(profile)
    assert profile.read_bytes()==before and key.read_bytes()==key_before and key.stat().st_mode&0o777==0o600


def test_real_smtp_capture_is_authenticated_private_and_message_idempotent(tmp_path):
    config=mail_configuration(tmp_path);smtp,inbox=start_capture(config)
    try:
        original=payload();first=render_account_mail(original,config);second=render_account_mail(original,config)
        assert first.as_bytes()==second.as_bytes()
        assert send_account_mail(first,config)=='LOCAL_CAPTURED'
        assert send_account_mail(second,config)=='LOCAL_CAPTURED'
        base='http://127.0.0.1:'+str(config['capture']['http_port'])
        with pytest.raises(urllib.error.HTTPError) as unauthorized:urllib.request.urlopen(base+'/messages')
        assert unauthorized.value.code==401
        password=Path(config['capture']['password_ref'][5:]).read_text();authorization='Basic '+base64.b64encode(('operator:'+password).encode()).decode()
        request=urllib.request.Request(base+'/messages',headers={'Authorization':authorization})
        with urllib.request.urlopen(request) as response:messages=json.loads(response.read())['messages']
        assert len(messages)==1 and messages[0]['envelope_to']==['customer@review.invalid']
        assert '#token=' in messages[0]['text'] and '&lt;Example Customer&gt;' in messages[0]['html']
        assert all(path.stat().st_mode&0o777==0o600 for path in (Path(config['capture']['directory'])/'messages').iterdir())
        foreign=urllib.request.Request(base+'/messages',headers={'Authorization':authorization,'Origin':'https://foreign.invalid'})
        with pytest.raises(urllib.error.HTTPError) as blocked:urllib.request.urlopen(foreign)
        assert blocked.value.code==403
    finally:
        for server in (smtp,inbox):server.shutdown();server.server_close()


def test_smtp_aggregate_deadline_bounds_multiple_slow_protocol_steps(tmp_path):
    config=mail_configuration(tmp_path);config['timeout_seconds']=1
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                time.sleep(.45);self.wfile.write(b'220 localhost\r\n');self.wfile.flush()
                while self.rfile.readline(1024):
                    time.sleep(.45);self.wfile.write(b'250-localhost\r\n250 AUTH PLAIN\r\n');self.wfile.flush()
            except OSError:pass
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    config['port']=server.server_address[1];threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        started=time.monotonic()
        with pytest.raises(MailDeliveryError):send_account_mail(render_account_mail(payload(),config),config)
        assert time.monotonic()-started<1.6
    finally:server.shutdown();server.server_close()
