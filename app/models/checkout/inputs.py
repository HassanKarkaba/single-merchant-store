from __future__ import annotations
import re


class CheckoutInputPolicy:
    @staticmethod
    def validate_address(address):
        if not isinstance(address,dict):raise ValueError('invalid_address')
        required=('line1','city','region','postal_code','country')
        if any(not isinstance(address.get(key),str) or not 0 < len(address[key].strip()) <= 200 for key in required):raise ValueError('invalid_address')
        states={'AL','AZ','AR','CA','CO','CT','DE','FL','GA','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','DC'}
        if address['country']!='US' or address['region'] not in states or not re.fullmatch(r'\d{5}(-\d{4})?',address['postal_code']):raise ValueError('unsupported_destination')
        if 'line2' in address and (not isinstance(address['line2'],str) or len(address['line2'])>200):raise ValueError('invalid_address')
        return dict(address)
    @staticmethod
    def validate_demo_contact(email,address):
        CheckoutInputPolicy.validate_email(email)
        if not email.endswith('.invalid'):raise ValueError('synthetic_email_required')
        fields={'recipient','name','line1','line2','city','region','postal_code','country'}
        if not isinstance(address,dict) or set(address)-fields or address.get('recipient',address.get('name'))!='Demo Customer' or ('name'in address and address['name']!='Demo Customer') or address.get('line1')!='100 Example Street' or address.get('city')!='Demo City' or address.get('country')!='US' or {'MI':'48104','CA':'94105','TX':'78701'}.get(address.get('region'))!=address.get('postal_code'):raise ValueError('synthetic_fixture_address_required')
        if 'line2'in address and (not isinstance(address['line2'],str) or not re.fullmatch(r'Unit (?:[1-9]|[1-9][0-9])',address['line2'])):raise ValueError('synthetic_unit_required')
        result={k:v for k,v in address.items() if k!='name'};result['recipient']='Demo Customer'
        return email,result
    @staticmethod
    def validate_email(email):
        if not isinstance(email,str) or len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):raise ValueError('invalid_email')
        return email
