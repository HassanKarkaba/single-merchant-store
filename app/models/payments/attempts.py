from __future__ import annotations



class PaymentAttemptPolicy:
    @staticmethod
    def validate_authorization(amount_minor, currency):
        if type(amount_minor) is not int or not 0 <= amount_minor <= 99999999 or 0 < amount_minor < 50 or currency != 'USD':
            raise ValueError('invalid_amount')
        return amount_minor
    @staticmethod
    def validate_provider_result(result, amount_minor, object_id=None):
        if not isinstance(result, dict) or result.get('currency') != 'USD' or type(result.get('amount_minor')) is not int or result['amount_minor'] != amount_minor:
            raise ValueError('provider_contract_error')
        if object_id is not None and result.get('id') != object_id:
            raise ValueError('provider_object_mismatch')
        return result
