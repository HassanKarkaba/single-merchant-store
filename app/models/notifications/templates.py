"""Closed transactional account-email templates; no arbitrary content port."""
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr,format_datetime
import hashlib
import html
from urllib.parse import quote


def render_account_mail(payload,config):
    purpose=payload['purpose']
    if purpose=='email_verification':
        subject='Verify your email address';action='Verify email address';route='/verify-email'
        explanation='Confirm your email address to finish creating your account.'
    elif purpose=='password_reset':
        subject='Reset your password';action='Reset password';route='/reset-password'
        explanation='Use this link to choose a new password. If you did not request this, you can ignore this email.'
    else:raise ValueError('unsupported_account_mail_purpose')
    url=config['public_origin']+route+'#token='+quote(payload['token'],safe='')
    greeting='Hello '+payload['full_name']+',' if payload['full_name'] else 'Hello,'
    expiry=payload['expires_at']
    message=EmailMessage(policy=SMTP)
    message['From']=formataddr((config['from_name'],config['from_address']))
    message['To']=payload['recipient'];message['Subject']=subject
    message['Message-ID']='<'+hashlib.sha256(payload['message_id'].encode()).hexdigest()+'@account-mail.store.invalid>'
    message['Date']=format_datetime(datetime.fromisoformat(payload['created_at']))
    message.set_content(greeting+'\n\n'+explanation+'\n\n'+url+'\n\nThis one-use link expires at '+expiry+'.\n')
    message.add_alternative('<!doctype html><html><body><p>'+html.escape(greeting)+'</p><p>'+html.escape(explanation)+'</p><p><a href="'+html.escape(url,quote=True)+'">'+action+'</a></p><p>This one-use link expires at '+html.escape(expiry)+'.</p></body></html>',subtype='html')
    message.set_boundary('account-mail-'+hashlib.sha256(payload['message_id'].encode()).hexdigest()[:32])
    return message
