from __future__ import annotations



class MonetaryEffectPolicy:
    @staticmethod
    def reserve_refund(amount_minor, captured_minor, refunded_minor, pending_minor):
        values = (amount_minor, captured_minor, refunded_minor, pending_minor)
        if any(type(value) is not int for value in values) or amount_minor <= 0 or min(captured_minor, refunded_minor, pending_minor) < 0:
            raise ValueError('invalid_amount')
        if amount_minor + pending_minor + refunded_minor > captured_minor:
            raise ValueError('refund_exceeds_remaining')
        return captured_minor - refunded_minor - pending_minor - amount_minor
