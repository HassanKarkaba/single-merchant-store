from __future__ import annotations
import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta
from .inputs import CheckoutInputPolicy
from .participants import CheckoutParticipantPlan

DDL='''CREATE SCHEMA IF NOT EXISTS checkout;
CREATE TABLE IF NOT EXISTS checkout.sessions(id text PRIMARY KEY,owner_id text NOT NULL,cart_id text NOT NULL,cart_revision bigint NOT NULL,lines jsonb NOT NULL,email text,address jsonb,quote jsonb,accepted_quote text,state text NOT NULL DEFAULT 'DRAFT',version bigint NOT NULL DEFAULT 1,created_at timestamptz DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS checkout.acceptances(checkout_id text NOT NULL,quote_id text NOT NULL,total_minor bigint NOT NULL,accepted_at timestamptz DEFAULT clock_timestamp(),PRIMARY KEY(checkout_id,quote_id));'''

DDL2='ALTER TABLE checkout.sessions ADD COLUMN IF NOT EXISTS coupon_code text'

class BaseCheckoutService:
    def __init__(self,db,cart,catalog,pricing,tax,shipping,*,authorize):
        self.db,self.cart,self.catalog,self.pricing,self.tax,self.shipping,self.authorize=db,cart,catalog,pricing,tax,shipping,authorize
        self.input_policy=CheckoutInputPolicy
        self.participant_plan=CheckoutParticipantPlan

    def _get(self,tx,id,actor,lock=False):
        self.authorize(tx,actor,'checkout.read',id)
        row=tx.execute('SELECT * FROM checkout.sessions WHERE id=%s'+(' FOR UPDATE' if lock else ''),(id,)).fetchone()
        if not row or (actor['kind'] not in ('service','staff') and row['owner_id']!=actor['id']):raise ValueError('not_found')
        return row

    def accept_quote(self,checkout_id,quote_id,total_minor,expected_revision,*,actor,command_id=None):
        with self.db.transaction('checkout') as tx:
            row=self._get(tx,checkout_id,actor,True);q=row['quote']
            if row['state']=='PURCHASING':raise ValueError('checkout_already_submitted')
            if row['version']!=expected_revision:raise ValueError('version_conflict')
            now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if not q or q['id']!=quote_id or q['total_minor']!=total_minor:raise ValueError('quote_mismatch')
            if datetime.fromisoformat(q['expires_at'])<=now:raise ValueError('quote_expired')
            tx.execute('INSERT INTO checkout.acceptances(checkout_id,quote_id,total_minor) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(checkout_id,quote_id,total_minor))
            tx.execute("UPDATE checkout.sessions SET accepted_quote=%s,state='QUOTED',version=version+1 WHERE id=%s",(quote_id,checkout_id))
            return self._get(tx,checkout_id,actor)

    def begin(self,cart_id,cart_revision,*,actor,grant=None,command_id=None):
        cart=self.cart.get_cart(cart_id,grant=grant,actor=actor)
        if cart['version']!=cart_revision:raise ValueError('version_conflict')
        if not cart['lines']:raise ValueError('cart_empty')
        if sum(x['quantity'] for x in cart['lines'])>200:raise ValueError('unit_limit')
        lines=self.revalidate_sellability(cart['lines'])
        with self.db.transaction('checkout',command_id=command_id,request_digest=hashlib.sha256(json.dumps([actor['id'],cart_id,cart_revision]).encode()).hexdigest() if command_id else None,authority_guard=lambda tx:self.authorize(tx,actor,'checkout.begin',cart_id)) as tx:
            replay=tx.command_lookup() if command_id else None
            if replay is not None:return replay
            id=str(uuid.uuid4())
            tx.execute('INSERT INTO checkout.sessions(id,owner_id,cart_id,cart_revision,lines,coupon_code) VALUES(%s,%s,%s,%s,%s::jsonb,%s)',(id,actor['id'],cart_id,cart_revision,json.dumps(lines),cart.get('coupon')))
            result=self._get(tx,id,actor)
            if command_id:tx.command_complete(safe(result))
            return result

    def evaluate(self,checkout_id,*,actor,command_id=None):
        row=self.get_checkout(checkout_id,actor=actor)
        if row['state']=='PURCHASING':raise ValueError('checkout_already_submitted')
        if not row['address'] or not row['email']:raise ValueError('address_required')
        lines=self.revalidate_sellability(row['lines'])
        price=self.pricing.quote(lines,coupon_code=row.get('coupon_code'),customer_id=actor['id'] if actor['kind']=='customer' else None,actor=actor)
        offer=self.shipping.quote(row['address'],None,lines,actor=actor)
        # Quotation is synthetic until native packing/media and real providers qualify.
        shipping_minor=offer.get('amount_minor',offer.get('price_minor',offer.get('shipping_minor',795)))
        if price.get('shipping_discount_rule')=='free':shipping_minor=0
        discounted=price.get('lines',lines)
        tax=self.tax.calculate(checkout_id,row['address'],discounted,shipping_minor,actor=actor)
        tax_lines={line['variant_id']:line for line in tax['lines']}
        quoted_lines=[]
        for line in discounted:
            tax_line=tax_lines[line['variant_id']]
            unit_tax={unit['unit_id']:unit['tax_minor'] for unit in tax_line['units']}
            quoted_lines.append({**line,'tax_minor':tax_line['tax_minor'],'units':[{**unit,'tax_minor':unit_tax[unit['unit_id']]} for unit in line['units']]})
        subtotal=price.get('subtotal_minor',sum(x.get('unit_minor',0)*x['quantity'] for x in discounted))
        discount=price.get('discount_minor',0);tax_minor=tax.get('tax_minor',tax.get('total_minor',0))
        q={'id':str(uuid.uuid4()),'pricing_quote_id':price['id'],'shipping_quote_id':offer['id'],'tax_calculation_id':tax['id'],'lines':quoted_lines,'subtotal_minor':subtotal,'discount_minor':discount,'shipping_minor':shipping_minor,'shipping_tax_minor':tax.get('shipping_tax_minor',0),'tax_minor':tax_minor,'total_minor':subtotal-discount+shipping_minor+tax_minor,'currency':'USD'}
        if any(type(q[x]) is not int or q[x]<0 for x in ('subtotal_minor','discount_minor','shipping_minor','tax_minor','total_minor')) or q['total_minor']>99999999:raise ValueError('invalid_quote')
        with self.db.transaction('checkout') as tx:
            current=self._get(tx,checkout_id,actor,True)
            if current['state']=='PURCHASING':raise ValueError('checkout_already_submitted')
            if current['version']!=row['version']:raise ValueError('version_conflict')
            now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            bounds=[now+timedelta(minutes=10)]
            for participant in (price,offer,tax):
                expiry=participant.get('expires_at')
                if type(expiry) is int:bounds.append(datetime.fromtimestamp(expiry/1000,timezone.utc))
                elif isinstance(expiry,str):bounds.append(datetime.fromisoformat(expiry))
            q['expires_at']=min(bounds).isoformat()
            if min(bounds)<=now:raise ValueError('quote_expired')
            q=self.participant_plan.freeze_quote(q)
            tx.execute("UPDATE checkout.sessions SET quote=%s::jsonb,accepted_quote=NULL,state='QUOTED',version=version+1 WHERE id=%s",(json.dumps(q),checkout_id))
            return self._get(tx,checkout_id,actor)

    def freeze_for_purchase(self,checkout_id,accepted_quote_id,*,actor):
        candidate=self.get_checkout(checkout_id,actor=actor)
        if candidate['state']!='PURCHASING':self.revalidate_sellability(candidate['lines'])
        with self.db.transaction('checkout') as tx:
            row=self._get(tx,checkout_id,actor,True);quote=row['quote']
            if not quote or row['accepted_quote']!=accepted_quote_id or quote['id']!=accepted_quote_id:raise ValueError('quote_not_accepted')
            if row['state']=='PURCHASING':return row
            now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if datetime.fromisoformat(quote['expires_at'])<=now:raise ValueError('quote_expired')
            tx.execute("UPDATE checkout.sessions SET state='PURCHASING',version=version+1 WHERE id=%s",(checkout_id,))
            return self._get(tx,checkout_id,actor)

    def get_checkout(self,checkout_id,*,actor):
        with self.db.transaction('checkout') as tx:return self._get(tx,checkout_id,actor)

    def initialize(self):
        self.db.migrate('checkout',1,DDL)
        self.db.migrate('checkout',2,DDL2)

    def revalidate_sellability(self, lines):
        products=[];cursor=None
        while True:
            page=self.catalog.list_public_products({'limit':50},cursor)
            products.extend(page['items']);cursor=page.get('next_cursor')
            if not cursor:break
        variants={v['id']:(p,v) for p in products for v in p.get('variants',[])}
        enriched=[]
        for line in lines:
            found=variants.get(line['variant_id'])
            if found is None:raise ValueError('product_not_sellable')
            product,variant=found
            enriched.append({**line,'product_id':product['id'],'name':product['name'],'sku':variant['sku'],'options':variant['options'],'weight_g':variant['weight_g'],'dimensions_mm':variant['dimensions_mm'],'catalog_revision':product['version'],'tax_code':product.get('attributes',{}).get('tax_code')})
        return enriched

    def update_contact_and_address(self,checkout_id,email,address,expected_revision,*,actor,command_id=None):
        self.input_policy.validate_email(email)
        address=self.input_policy.validate_address(address)
        with self.db.transaction('checkout') as tx:
            row=self._get(tx,checkout_id,actor,True)
            if row['state']=='PURCHASING':raise ValueError('checkout_already_submitted')
            if row['version']!=expected_revision:raise ValueError('version_conflict')
            tx.execute("UPDATE checkout.sessions SET email=%s,address=%s::jsonb,quote=NULL,accepted_quote=NULL,state='DRAFT',version=version+1 WHERE id=%s",(email,json.dumps(address),checkout_id))
            return self._get(tx,checkout_id,actor)

class CheckoutService(BaseCheckoutService):
    PF_DDL='''ALTER TABLE checkout.sessions ADD COLUMN IF NOT EXISTS delivery_offers jsonb;
    ALTER TABLE checkout.sessions ADD COLUMN IF NOT EXISTS selected_offer jsonb;
    ALTER TABLE checkout.sessions ADD COLUMN IF NOT EXISTS claimed_attempt_id text;
    ALTER TABLE checkout.sessions ADD COLUMN IF NOT EXISTS reservation_snapshot jsonb;
    ALTER TABLE checkout.sessions ADD COLUMN IF NOT EXISTS frozen_quote_until timestamptz;
    CREATE UNIQUE INDEX IF NOT EXISTS checkout_one_attempt ON checkout.sessions(claimed_attempt_id) WHERE claimed_attempt_id IS NOT NULL;'''

    PF_LINK_DDL='ALTER TABLE checkout.sessions ADD COLUMN IF NOT EXISTS replacement_return_id text;'

    def __init__(self,db,cart,catalog,pricing,tax,shipping,*,authorize,profile='local-sandbox'):
        super().__init__(db,cart,catalog,pricing,tax,shipping,authorize=authorize)
        if profile not in ('local-sandbox','demo-test','portfolio-demo'):raise ValueError('profile_denied')
        self.owner='checkout';self.profile=profile;self.resource_predicate=None;self.admission_port=None;self.reservation_port=None
        self.policy_snapshot=None;self.purchase_state_verifier=None

    def _get(self,tx,id,actor,lock=False):
        if self.profile=='local-sandbox':return super()._get(tx,id,actor,lock)
        row=tx.execute('SELECT * FROM checkout.sessions WHERE id=%s'+(' FOR UPDATE' if lock else ''),(id,)).fetchone()
        if row is None or (actor.get('kind') not in ('staff','recovery') and row['owner_id']!=actor.get('id')):raise ValueError('not_found')
        return row

    def _guard(self,actor,operation,resource,*,checkout=True):
        def admit(tx):
            if self.authorize(tx,actor,'checkout.'+operation,resource) is not True:raise PermissionError('forbidden')
            if self.resource_predicate is not None and self.resource_predicate(tx,actor,'checkout.'+operation,resource) is not True:raise PermissionError('forbidden')
            if checkout:
                row=tx.execute('SELECT owner_id FROM checkout.sessions WHERE id=%s FOR SHARE',(resource,)).fetchone()
                if row is None or (actor.get('kind') not in ('staff','recovery') and row['owner_id']!=actor.get('id')):raise ValueError('not_found')
            return True
        return admit

    def _prior(self,operation,resource,payload,command_id,actor,*,checkout=True):
        if command_id is None:return None
        if not isinstance(command_id,str) or str(uuid.UUID(command_id))!=command_id:raise ValueError('invalid_command_id')
        key=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([actor['merchant_id'],actor['id'],actor.get('obligation_id'),'checkout',operation,resource,str(command_id)],separators=(',',':'))))
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        with self.db.transaction('checkout',authority_guard=self._guard(actor,operation,resource,checkout=checkout)) as tx:
            row=tx.command_record(key)
            if row is None:return None
            if row['request_digest']!=digest:raise ValueError('idempotency_conflict')
            if row['state']!='COMPLETED':raise ValueError('command_outcome_unknown')
            return row['result']

    def _run(self,operation,resource,payload,command_id,actor,fn,*,checkout=True):
        client=str(command_id or uuid.uuid4())
        if str(uuid.UUID(client))!=client:raise ValueError('invalid_command_id')
        key=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([actor['merchant_id'],actor['id'],actor.get('obligation_id'),'checkout',operation,resource,client],separators=(',',':'))))
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        with self.db.transaction('checkout',command_id=key,request_digest=digest,authority_guard=self._guard(actor,operation,resource,checkout=checkout)) as tx:
            prior=tx.command_lookup()
            if prior is not None:return prior
            result=safe(fn(tx));tx.command_complete(result)
            tx.stage_outbox('checkout.'+operation,key,{'id':result.get('id',resource),'state':result.get('state'),'version':result.get('version',1),'receipt_id':key})
            tx.stage_audit(key,{'operation':operation,'resource':resource,'state':result.get('state')})
            return result

    def _writable(self,row,expected):
        if row['claimed_attempt_id'] is not None:raise ValueError('checkout_already_submitted')
        if type(expected) is not int or expected<1 or row['version']!=expected:raise ValueError('version_conflict')

    def accept_quote(self,checkout_id,quote_id,total_minor,expected_revision,*,actor,command_id=None):
        if self.profile=='local-sandbox':return super().accept_quote(checkout_id,quote_id,total_minor,expected_revision,actor=actor,command_id=command_id)
        cart=self.cart.get_cart(self.get_checkout(checkout_id,actor=actor)['cart_id'],actor=actor)
        def work(tx):
            row=self._get(tx,checkout_id,actor,True);self._writable(row,expected_revision);q=row['quote'];now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if cart['version']!=row['cart_revision'] or not q or q['id']!=quote_id or type(total_minor)is not int or q['total_minor']!=total_minor:raise ValueError('quote_mismatch')
            if datetime.fromisoformat(q['expires_at'])<=now:raise ValueError('quote_expired')
            tx.execute('INSERT INTO checkout.acceptances(checkout_id,quote_id,total_minor) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(checkout_id,quote_id,total_minor))
            tx.execute("UPDATE checkout.sessions SET accepted_quote=%s,state='ACCEPTED',version=version+1 WHERE id=%s",(quote_id,checkout_id))
            return self._get(tx,checkout_id,actor)
        return self._run('accept_quote',checkout_id,[quote_id,total_minor,expected_revision],command_id,actor,work)

    @staticmethod
    def attempt_total(value):
        if type(value)is not int or value<0 or value>99999999 or 0<value<50:raise ValueError('invalid_quote_total')

    def begin(self,cart_id,cart_revision,*,actor,grant=None,command_id=None):
        if self.profile=='local-sandbox':return super().begin(cart_id,cart_revision,actor=actor,grant=grant,command_id=command_id)
        cart=self.cart.get_cart(cart_id,actor=actor)
        prior=self._prior('begin',cart_id,[cart_id,cart_revision],command_id,actor,checkout=False)
        if prior is not None:return prior
        if type(cart_revision)is not int or cart['version']!=cart_revision:raise ValueError('version_conflict')
        if not cart['lines'] or len(cart['lines'])>100 or sum(l['quantity'] for l in cart['lines'])>200:raise ValueError('cart_empty_or_limit')
        lines=self.revalidate_sellability(cart['lines'])
        purpose=cart.get('purpose','');replacement_return_id=purpose.split(':',1)[1] if isinstance(purpose,str) and purpose.startswith('replacement:') else None
        if replacement_return_id is not None and str(uuid.UUID(replacement_return_id))!=replacement_return_id:raise ValueError('invalid_native_replacement_cart')
        def work(tx):
            id=str(uuid.uuid4())
            tx.execute('INSERT INTO checkout.sessions(id,owner_id,cart_id,cart_revision,lines,coupon_code,replacement_return_id) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s)',(id,cart['owner_id'],cart_id,cart_revision,json.dumps(lines),cart.get('coupon'),replacement_return_id))
            return self._get(tx,id,actor)
        return self._run('begin',cart_id,[cart_id,cart_revision],command_id,actor,work,checkout=False)

    def bind_reservation(self,checkout_id,attempt_id,reservation_receipt,*,actor,command_id=None):
        if self.reservation_port is None:raise ValueError('actual_inventory_reservation_inspector_required')
        reservation=self.reservation_port(reservation_receipt['id'],actor=actor)
        expiry=reservation['expires_at']
        if type(expiry)is not int:raise ValueError('reservation_expiry_contract_error')
        frozen=datetime.fromtimestamp(expiry/1000,timezone.utc)
        def work(tx):
            row=self._get(tx,checkout_id,actor,True)
            if row['claimed_attempt_id']!=attempt_id or reservation['checkout_id']!=checkout_id or reservation['state']!='reserved':raise ValueError('reservation_claim_mismatch')
            if datetime.fromtimestamp((expiry-900000)/1000,timezone.utc)>=datetime.fromisoformat(row['quote']['expires_at']):raise ValueError('quote_expired_before_reservation')
            if row['frozen_quote_until'] is not None:
                if row['reservation_snapshot']['id']!=reservation['id'] or row['frozen_quote_until']!=frozen:raise ValueError('reservation_renewal_forbidden')
                return self._get(tx,checkout_id,actor)
            tx.execute('UPDATE checkout.sessions SET reservation_snapshot=%s::jsonb,frozen_quote_until=%s,version=version+1 WHERE id=%s',(json.dumps(reservation),frozen,checkout_id))
            return self._get(tx,checkout_id,actor)
        return self._run('bind_reservation',checkout_id,[attempt_id,reservation['id'],expiry],command_id,actor,work)

    def claim_purchase(self,checkout_id,quote_id,attempt_id,*,actor,command_id=None):
        if self.admission_port is None:raise ValueError('actual_application_admission_required')
        admitted=self.admission_port(attempt_id,actor=actor)
        if admitted.get('checkout_id')!=checkout_id or admitted.get('accepted_quote_id')!=quote_id:raise ValueError('admission_checkout_mismatch')
        cart=self.cart.get_cart(self.get_checkout(checkout_id,actor=actor)['cart_id'],actor=actor)
        def work(tx):
            row=self._get(tx,checkout_id,actor,True)
            if row['claimed_attempt_id'] is not None:
                if row['claimed_attempt_id']!=attempt_id:raise ValueError('checkout_bound_to_other_attempt')
                return self._get(tx,checkout_id,actor)
            q=row['quote'];now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if cart['version']!=row['cart_revision'] or row['accepted_quote']!=quote_id or not q or q['id']!=quote_id or admitted.get('quote_digest')!=q['digest']:raise ValueError('quote_not_accepted')
            if datetime.fromisoformat(q['expires_at'])<=now:raise ValueError('quote_expired')
            tx.execute("UPDATE checkout.sessions SET claimed_attempt_id=%s,state='PURCHASING',version=version+1 WHERE id=%s",(attempt_id,checkout_id))
            return self._get(tx,checkout_id,actor)
        return self._run('claim_purchase',checkout_id,[quote_id,attempt_id],command_id,actor,work)

    def evaluate(self,checkout_id,*,actor,command_id=None):
        if self.profile=='local-sandbox':return super().evaluate(checkout_id,actor=actor,command_id=command_id)
        prior=self._prior('evaluate',checkout_id,[checkout_id],command_id,actor)
        if prior is not None:return prior
        row=self.get_checkout(checkout_id,actor=actor)
        if row['claimed_attempt_id'] is not None:raise ValueError('checkout_already_submitted')
        if not row['address'] or not row['email']:raise ValueError('address_required')
        cart=self.cart.get_cart(row['cart_id'],actor=actor)
        if cart['version']!=row['cart_revision']:raise ValueError('cart_changed_create_new_checkout')
        lines=self.revalidate_sellability(row['lines'])
        # First evaluation obtains finite offers only; no implicit service choice.
        if not row['selected_offer']:
            quoted=self.shipping.quote(row['address'],None,lines,actor=actor,command_id=command_id)
            def offers(tx):
                current=self._get(tx,checkout_id,actor,True)
                if current['version']!=row['version']:raise ValueError('version_conflict')
                tx.execute("UPDATE checkout.sessions SET delivery_offers=%s::jsonb,quote=NULL,accepted_quote=NULL,state='DELIVERY_REQUIRED',version=version+1 WHERE id=%s",(json.dumps(quoted['offers']),checkout_id))
                return self._get(tx,checkout_id,actor)
            return self._run('evaluate',checkout_id,[checkout_id],command_id,actor,offers)
        offer=self.shipping.get_offer(row['selected_offer']['id'],actor=actor)
        if offer['destination']!=row['address'] or type(offer.get('amount_minor')) is not int:raise ValueError('delivery_input_mismatch')
        shape=lambda l:{k:l[k] for k in ('variant_id','quantity','weight_g','dimensions_mm')}
        if sorted(map(shape,lines),key=lambda l:l['variant_id'])!=sorted(offer['lines'],key=lambda l:l['variant_id']):raise ValueError('delivery_input_changed')
        child=lambda label:str(uuid.uuid5(uuid.NAMESPACE_URL,'checkout:'+checkout_id+':'+str(command_id)+':'+label)) if command_id else None
        price=self.pricing.quote(lines,coupon_code=row['coupon_code'],customer_id=row['owner_id'] if actor['kind']=='customer' else None,actor=actor,command_id=child('pricing'))
        shipping=0 if price.get('shipping_discount_rule')=='free' else offer['amount_minor']
        tax=self.tax.calculate(checkout_id,row['address'],price['lines'],shipping,actor=actor,command_id=child('tax'))
        taxed={l['variant_id']:l for l in tax['lines']};result_lines=[]
        for l in price['lines']:
            detail=taxed[l['variant_id']];units={u['unit_id']:u for u in detail['units']}
            result_lines.append({**l,'tax_minor':detail['tax_minor'],'units':[{**u,'tax_minor':units[u['unit_id']]['tax_minor']} for u in l['units']]})
        total=price['item_net_minor']+shipping+tax['tax_minor'];self.attempt_total(total)
        def work(tx):
            current=self._get(tx,checkout_id,actor,True)
            if current['version']!=row['version']:raise ValueError('version_conflict')
            now=tx.execute('SELECT clock_timestamp() now').fetchone()['now'];expiry=min(now+timedelta(minutes=10),*[datetime.fromtimestamp(p['expires_at']/1000,timezone.utc) for p in (offer,price,tax)])
            if expiry<=now:raise ValueError('participant_quote_expired')
            q={'id':str(uuid.uuid4()),'currency':'USD','lines':result_lines,'subtotal_minor':price['item_gross_minor'],'discount_minor':price['discount_minor'],'shipping_minor':shipping,'shipping_tax_minor':tax['shipping_tax_minor'],'tax_minor':tax['tax_minor'],'total_minor':total,'shipping_quote_id':offer['id'],'selected_offer':offer,'pricing_quote_id':price['id'],'tax_calculation_id':tax['id'],'expires_at':expiry.isoformat(),'cart_revision':row['cart_revision'],'policy':self.policy_snapshot,'fixture_revisions':['PF-ADDR1','PF-TAX1','PF-SHIP1']}
            if not isinstance(self.policy_snapshot,dict) or not self.policy_snapshot.get('revision'):raise ValueError('versioned_demo_policy_required')
            q=self.participant_plan.freeze_quote(q,profile=self.profile)
            tx.execute("UPDATE checkout.sessions SET quote=%s::jsonb,accepted_quote=NULL,state='QUOTED',version=version+1 WHERE id=%s",(json.dumps(q),checkout_id))
            return self._get(tx,checkout_id,actor)
        return self._run('evaluate',checkout_id,[checkout_id],command_id,actor,work)

    def freeze_for_purchase(self,*args,**kwargs):
        if self.profile!='local-sandbox':raise ValueError('durable_admission_claim_required')
        return super().freeze_for_purchase(*args,**kwargs)

    def get_checkout(self,checkout_id,*,actor):
        if self.profile=='local-sandbox':return super().get_checkout(checkout_id,actor=actor)
        with self.db.transaction('checkout',authority_guard=self._guard(actor,'get_checkout',checkout_id)) as tx:return self._get(tx,checkout_id,actor)

    def initialize(self):
        self.db.verify_migration('checkout',1,DDL,allow_newer=True);self.db.verify_migration('checkout',2,DDL2,allow_newer=self.profile!='local-sandbox')
        if self.profile!='local-sandbox':
            self.db.verify_migration('checkout',3,self.PF_DDL,allow_newer=True);self.db.verify_migration('checkout',4,self.PF_LINK_DDL)
        return {'owner':'checkout','ready':True,'profile':self.profile}

    def inspect_purchase_claim(self,attempt_id,*,actor):
        with self.db.transaction('checkout',authority_guard=self._guard(actor,'inspect_purchase_claim',attempt_id,checkout=False)) as tx:
            row=tx.execute('SELECT * FROM checkout.sessions WHERE claimed_attempt_id=%s',(attempt_id,)).fetchone()
            if row is None:raise ValueError('not_found')
            row=self._get(tx,row['id'],actor)
            result=safe(row)
            result['attempt_id']=attempt_id
            result['frozen_quote_until']=int(row['frozen_quote_until'].timestamp()*1000) if row['frozen_quote_until'] else None
            return result

    def inspect_receipt(self,evidence,*,actor):
        id=evidence['id'] if isinstance(evidence,dict) else evidence
        with self.db.transaction('checkout',authority_guard=self._guard(actor,'inspect_receipt',id)) as tx:
            row=self._get(tx,id,actor);result=safe(row)
            if row.get('claimed_attempt_id'):
                if not callable(self.purchase_state_verifier):raise ValueError('actual_purchase_state_verifier_required')
                actual=self.purchase_state_verifier(tx,id,row['claimed_attempt_id'])
                if not isinstance(actual,dict) or actual.get('checkout_id')!=id or actual.get('attempt_id')!=row['claimed_attempt_id'] or not actual.get('admission_receipt'):raise ValueError('actual_checkout_claim_unverified')
                result['purchase_state']=actual.get('purchase_state',actual['state'])
                result['purchase_confirmation']=actual.get('confirmation')
            return result

    def inspect_reservation_input(self,checkout_id,*,actor):
        row=self.get_checkout(checkout_id,actor=actor)
        with self.db.transaction('checkout',authority_guard=self._guard(actor,'inspect_reservation_input',checkout_id)) as tx:
            now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if not row['claimed_attempt_id'] or not row['quote'] or datetime.fromisoformat(row['quote']['expires_at'])<=now:raise ValueError('financial_admission_quote_expired')
        return row

    def migrate(self):
        self.db.migrate('checkout',1,DDL);self.db.migrate('checkout',2,DDL2)
        if self.profile!='local-sandbox':
            self.db.migrate('checkout',3,self.PF_DDL);self.db.migrate('checkout',4,self.PF_LINK_DDL)

    def safety_snapshot(self, *, actor):
        with self.db.transaction('checkout',authority_guard=self._guard(actor,'safety_snapshot','checkout',checkout=False)) as tx:
            rows=tx.fetchall('SELECT id,claimed_attempt_id FROM checkout.sessions WHERE claimed_attempt_id IS NOT NULL ORDER BY id LIMIT 10001 FOR SHARE')
            failures=[];unresolved=0
            if len(rows)>10000:failures.append('claimed_checkout_scan_bound_exceeded')
            for row in rows[:10000]:
                if not callable(self.purchase_state_verifier):
                    failures.append('actual_purchase_state_verifier_required');unresolved+=1;continue
                actual=self.purchase_state_verifier(tx,row['id'],row['claimed_attempt_id'])
                if not isinstance(actual,dict) or actual.get('checkout_id')!=row['id'] or actual.get('attempt_id')!=row['claimed_attempt_id'] or not actual.get('admission_receipt'):
                    failures.append('actual_checkout_claim_unverified');unresolved+=1;continue
                if actual.get('state')!='COMPLETED':unresolved+=1
            return {'owner':'checkout','complete':not failures,'unresolved':unresolved,'claimed':len(rows),'failures':sorted(set(failures))}

    def select_delivery(self,checkout_id,offer_id,expected_revision,*,actor,command_id=None):
        offer=self.shipping.get_offer(offer_id,actor=actor)
        def work(tx):
            row=self._get(tx,checkout_id,actor,True);self._writable(row,expected_revision);now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if not row['delivery_offers'] or offer_id not in [o['id'] for o in row['delivery_offers']] or offer['destination']!=row['address'] or datetime.fromtimestamp(offer['expires_at']/1000,timezone.utc)<=now:raise ValueError('invalid_or_expired_delivery_offer')
            tx.execute("UPDATE checkout.sessions SET selected_offer=%s::jsonb,quote=NULL,accepted_quote=NULL,state='DRAFT',version=version+1 WHERE id=%s",(json.dumps(offer),checkout_id))
            return self._get(tx,checkout_id,actor)
        return self._run('select_delivery',checkout_id,[offer_id,expected_revision],command_id,actor,work)

    def update_contact_and_address(self,checkout_id,email,address,expected_revision,*,actor,command_id=None):
        if self.profile=='local-sandbox':return super().update_contact_and_address(checkout_id,email,address,expected_revision,actor=actor,command_id=command_id)
        email,address=self.input_policy.validate_demo_contact(email,address)
        def work(tx):
            row=self._get(tx,checkout_id,actor,True);self._writable(row,expected_revision)
            tx.execute("UPDATE checkout.sessions SET email=%s,address=%s::jsonb,selected_offer=NULL,delivery_offers=NULL,quote=NULL,accepted_quote=NULL,state='DRAFT',version=version+1 WHERE id=%s",(email,json.dumps(address),checkout_id))
            return self._get(tx,checkout_id,actor)
        return self._run('update_contact_and_address',checkout_id,[email,address,expected_revision],command_id,actor,work)

def safe(value):
    return json.loads(json.dumps(value,default=lambda v:v.isoformat() if isinstance(v,datetime) else str(v)))
