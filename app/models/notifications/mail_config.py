"""Closed mail transport configuration and private local defaults."""
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit


def validate_mail_config(value,*,bootstrap=False):
    fields={'mode','public_origin','from_address','from_name','host','port','security','username','password_ref','timeout_seconds','max_attempts','queue_key_ref'}
    if type(value) is not dict or value.get('mode') not in ('local_capture','smtp') or set(value)!=(fields|({'capture'} if value.get('mode')=='local_capture' else set())):raise ValueError('closed_mail_configuration_required')
    def declared(item):return bootstrap and isinstance(item,str) and re.fullmatch(r'\$\{SME_[A-Z0-9_]+\}',item)
    def reference(item):return declared(item) or isinstance(item,str) and item.startswith('file:/') and '..' not in Path(item[5:]).parts and '\x00' not in item
    if not reference(value['queue_key_ref']) or value['password_ref'] is not None and not reference(value['password_ref']):raise ValueError('protected_mail_secret_reference_required')
    if not declared(value['from_address']) and (not isinstance(value['from_address'],str) or len(value['from_address'])>254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+",value['from_address'])):raise ValueError('mail_sender_required')
    if not isinstance(value['from_name'],str) or not 1<=len(value['from_name'])<=120 or any(ord(c)<32 or ord(c)==127 for c in value['from_name']):raise ValueError('mail_sender_required')
    if type(value['port']) is not int or not 1<=value['port']<=65535 or type(value['timeout_seconds']) is not int or not 1<=value['timeout_seconds']<=3 or type(value['max_attempts']) is not int or not 1<=value['max_attempts']<=8:raise ValueError('bounded_mail_transport_required')
    if not isinstance(value['host'],str) or not value['host'] or len(value['host'])>253 or any(c.isspace() for c in value['host']):raise ValueError('mail_host_required')
    if value['username'] is not None and (not isinstance(value['username'],str) or not value['username'] or len(value['username'])>200 or any(ord(c)<32 or ord(c)==127 for c in value['username'])):raise ValueError('mail_authentication_required')
    if bool(value['username'])!=bool(value['password_ref']):raise ValueError('mail_authentication_required')
    if not declared(value['public_origin']):
        origin=urlsplit(value['public_origin'])
        try:origin.port
        except ValueError:raise ValueError('mail_public_https_origin_required') from None
        if origin.scheme!='https' or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:raise ValueError('mail_public_https_origin_required')
    if value['mode']=='smtp':
        if value['security'] not in ('tls','starttls'):raise ValueError('verified_smtp_tls_required')
    else:
        capture=value['capture']
        if type(capture) is not dict or set(capture)!={'directory','smtp_port','http_port','password_ref'}:raise ValueError('closed_mail_capture_configuration_required')
        if value['host']!='127.0.0.1' or value['security']!='none' or value['username']!='local-capture' or capture['password_ref']!=value['password_ref'] or capture['smtp_port']!=value['port']:raise ValueError('private_loopback_capture_required')
        if type(capture['http_port']) is not int or not 1<=capture['http_port']<=65535 or capture['http_port']==capture['smtp_port']:raise ValueError('bounded_mail_capture_port_required')
        if not declared(capture['directory']) and (not isinstance(capture['directory'],str) or not Path(capture['directory']).is_absolute() or '..' in Path(capture['directory']).parts):raise ValueError('private_mail_capture_directory_required')
    return value
