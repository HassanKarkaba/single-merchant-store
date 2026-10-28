"""Public customer account actions have closed schemas and never imply a session."""
from __future__ import annotations
import uuid

ACCOUNT_SCHEMAS = {
    '/api/v1/auth/register': {'full_name','email','password','password_confirmation'},
    '/api/v1/auth/verify-email': {'token'},
    '/api/v1/auth/resend-verification': {'email'},
    '/api/v1/auth/forgot-password': {'email'},
    '/api/v1/auth/reset-password': {'token','password','password_confirmation'},
}
ACCOUNT_STATES = {'verification_pending','reset_requested','email_verified','password_reset','reset_pending'}
DELIVERY_STATES = {'queued','REQUEST_ACCEPTED','LOCAL_CAPTURED','SMTP_ACCEPTED','DELIVERY_FAILED','NOT_APPLICABLE','CANCELED'}


def account_result(value):
    allowed={'state','message','operation_id','delivery_status','retry_after','retryable'}
    if type(value) is not dict or set(value)-allowed or not {'state','message'}<=set(value) or value['state'] not in ACCOUNT_STATES or not isinstance(value['message'],str) or len(value['message'])>500:
        raise ValueError('account_response_unavailable')
    if 'operation_id' in value and (not isinstance(value['operation_id'],str) or str(uuid.UUID(value['operation_id']))!=value['operation_id']):raise ValueError('account_response_unavailable')
    if 'delivery_status' in value and value['delivery_status'] not in DELIVERY_STATES:raise ValueError('account_response_unavailable')
    if 'retry_after' in value and (type(value['retry_after']) is not int or not 0<=value['retry_after']<=86400):raise ValueError('account_response_unavailable')
    if 'retryable' in value and type(value['retryable']) is not bool:raise ValueError('account_response_unavailable')
    return value


class AccountAccessController:
    def __init__(self, identity):self.identity=identity

    def submit(self,path,body,*,carrier,csrf,source_key,idempotency_key):
        if path not in ACCOUNT_SCHEMAS or type(body) is not dict or set(body)!=ACCOUNT_SCHEMAS[path]:raise ValueError('unknown_auth_fields')
        try:
            if not isinstance(idempotency_key,str) or str(uuid.UUID(idempotency_key))!=idempotency_key:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise ValueError('invalid_operation_id') from None
        fields={'preauth_credential':carrier,'csrf':csrf,'source_key':source_key,'operation_id':idempotency_key}
        operation = {
            '/api/v1/auth/register':'register_customer',
            '/api/v1/auth/verify-email':'verify_email',
            '/api/v1/auth/resend-verification':'resend_verification',
            '/api/v1/auth/forgot-password':'request_password_reset',
            '/api/v1/auth/reset-password':'reset_password',
        }[path]
        return account_result(getattr(self.identity,operation)(**body,**fields))

    def status(self,operation_id,*,carrier,csrf,source_key):
        try:
            if not isinstance(operation_id,str) or str(uuid.UUID(operation_id))!=operation_id:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise ValueError('invalid_operation_id') from None
        return account_result(self.identity.get_account_action_status(operation_id,preauth_credential=carrier,csrf=csrf,source_key=source_key))
