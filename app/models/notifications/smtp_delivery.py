"""Verified SMTP with one aggregate deadline and safe status-only failures."""
from __future__ import annotations
import socket
import smtplib
import ssl
import threading
import time
from app.config import read_secret

DNS_SLOTS=threading.BoundedSemaphore(2)


class MailDeliveryError(ValueError):
    def __init__(self,code,retryable=True):self.code,self.retryable=code,retryable;super().__init__(code)


def remaining(deadline):
    value=deadline-time.monotonic()
    if value<=0:raise MailDeliveryError('smtp_timeout')
    return value


def resolve_address(host,port,deadline):
    try:
        import ipaddress
        ipaddress.ip_address(host)
        return [host]
    except ValueError:pass
    if not DNS_SLOTS.acquire(blocking=False):raise MailDeliveryError('smtp_dns_capacity')
    event=threading.Event();result=[]
    def resolve():
        try:result.append(socket.getaddrinfo(host,port,type=socket.SOCK_STREAM))
        except OSError:result.append(None)
        finally:DNS_SLOTS.release();event.set()
    threading.Thread(target=resolve,daemon=True).start()
    if not event.wait(remaining(deadline)):raise MailDeliveryError('smtp_timeout')
    if not result or not result[0]:raise MailDeliveryError('smtp_dns_unavailable')
    return list(dict.fromkeys(item[4][0] for item in result[0]))[:8]


class DeadlineSMTP(smtplib.SMTP):
    def __init__(self,host,port,*,deadline,address,tls=False,local_hostname='localhost'):
        self.deadline,self.address,self.implicit_tls=deadline,address,tls
        super().__init__(host,port,local_hostname=local_hostname,timeout=remaining(deadline))

    def _get_socket(self,host,port,timeout):
        sock=None;last_error=None
        for address in self.address:
            try:sock=socket.create_connection((address,port),timeout=remaining(self.deadline));break
            except OSError as error:last_error=error
        if sock is None:raise last_error or OSError('smtp_connect_failed')
        if self.implicit_tls:
            try:
                sock.settimeout(remaining(self.deadline))
                sock=ssl.create_default_context().wrap_socket(sock,server_hostname=host)
            except BaseException:sock.close();raise
        return sock

    def send(self,value):
        if self.sock:self.sock.settimeout(remaining(self.deadline))
        return super().send(value)

    def getreply(self):
        if self.sock:self.sock.settimeout(remaining(self.deadline))
        return super().getreply()

    def starttls(self,*,context=None):
        self.ehlo_or_helo_if_needed()
        if not self.has_extn('starttls'):raise smtplib.SMTPNotSupportedError('starttls_required')
        code,response=self.docmd('STARTTLS')
        if code!=220:raise smtplib.SMTPResponseException(code,response)
        self.sock.settimeout(remaining(self.deadline))
        self.sock=(context or ssl.create_default_context()).wrap_socket(self.sock,server_hostname=self._host)
        self.file=None;self.helo_resp=None;self.ehlo_resp=None;self.esmtp_features={};self.does_esmtp=False
        return code,response


def send_account_mail(message,config,*,deadline=None):
    deadline=min(deadline,time.monotonic()+config['timeout_seconds']) if deadline is not None else time.monotonic()+config['timeout_seconds'];client=None
    try:
        address=resolve_address(config['host'],config['port'],deadline)
        client=DeadlineSMTP(config['host'],config['port'],deadline=deadline,address=address,tls=config['security']=='tls',local_hostname=config['from_address'].rsplit('@',1)[1])
        client.ehlo_or_helo_if_needed()
        if config['security']=='starttls':client.starttls(context=ssl.create_default_context());client.ehlo()
        if config.get('username'):
            client.login(config['username'],read_secret(config['password_ref']))
        code,_=client.mail(config['from_address'])
        if code!=250:raise MailDeliveryError('smtp_sender_rejected',400<=code<500)
        code,_=client.rcpt(str(message['To']))
        if code not in (250,251):raise MailDeliveryError('smtp_recipient_rejected',400<=code<500)
        code,response=client.data(message.as_bytes())
        if code!=250:raise MailDeliveryError('smtp_message_rejected',400<=code<500)
        return 'LOCAL_CAPTURED' if config['mode']=='local_capture' and response.startswith(b'LOCAL_CAPTURED ') else 'SMTP_ACCEPTED'
    except MailDeliveryError:raise
    except smtplib.SMTPResponseException as error:raise MailDeliveryError('smtp_rejected',400<=error.smtp_code<500) from None
    except ssl.SSLError:raise MailDeliveryError('smtp_tls_failed',False) from None
    except smtplib.SMTPNotSupportedError:raise MailDeliveryError('smtp_tls_or_auth_unavailable',False) from None
    except (ValueError,UnicodeError):raise MailDeliveryError('smtp_configuration_failed',False) from None
    except (TimeoutError,OSError,smtplib.SMTPException):raise MailDeliveryError('smtp_unavailable') from None
    finally:
        if client is not None:client.close()
