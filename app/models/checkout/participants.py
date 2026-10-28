from __future__ import annotations
import hashlib
import json


class CheckoutParticipantPlan:
    @staticmethod
    def freeze_quote(quote, *, profile='local-sandbox'):
        if profile not in ('local-sandbox','demo-test','portfolio-demo'):raise ValueError('profile_denied')
        required=('subtotal_minor','discount_minor','shipping_minor','tax_minor','total_minor')
        if quote.get('currency')!='USD' or any(type(quote.get(key)) is not int or quote[key]<0 for key in required):raise ValueError('invalid_quote')
        if quote['subtotal_minor']-quote['discount_minor']+quote['shipping_minor']+quote['tax_minor']!=quote['total_minor'] or quote['total_minor']>99999999:raise ValueError('invalid_quote')
        if profile!='local-sandbox' and 0<quote['total_minor']<50:raise ValueError('invalid_quote_total')
        frozen=json.loads(json.dumps(quote,sort_keys=True,separators=(',',':')))
        frozen['digest']=hashlib.sha256(json.dumps(frozen,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return frozen
