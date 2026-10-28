"""Store model composition, authorization, persistence and fixture initialization."""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
import uuid
from .common import CURRENT_WORKLOAD_ACTOR, CURRENT_WORKER_LEASE, POLICY, STAFF, BUYERS, CATALOG_ROLES, STOCK_ROLES, SUPPORT_ROLES, FINANCE_ROLES, PF_WORK_OPS, ROOT, canonical_id, permission_for, require_revision, safe, step_id
from .persistence import PostgresPersistence
from .authorization import HostBoundPgAuthority, PortfolioDemoPolicy
from .identity import PortfolioDemoIdentity, SecretFileProvider, read_protected_file
from .disposable import DisposableResourceGuard
from .schema import DDL, PF_DDL, PF_COORD_DDL
from .workflows import PurchaseCoordinator, FulfillmentCoordinator, RecoveryCoordinator, RefundCoordinator, ReturnCoordinator
from .catalog import CatalogService
from .pricing import PricingService
from .inventory import InventoryService
from .cart import CartService
from .orders import OrderService
from .tax import TaxService
from .shipping import ShippingService
from .returns import ReturnsService
from .notifications import NotificationService, IdentityMailService
from .events import EventService
from .integrations import ExternalIntegrationService
from .payments import PaymentService
from .checkout import CheckoutService

class Store:
    """Compose the commerce models and their protected, transactional owner ports."""
    def __init__(self,dsn,*,profile='local-sandbox',config=None,maintenance=False):
        if profile not in ('local-sandbox','demo-test','portfolio-demo'):raise ValueError('profile_denied')
        if not isinstance(config,dict):raise ValueError('explicit_instance_config_required')
        self.config=dict(config);self._instance_dsn=dsn;self.profile=self.mode=profile;self.closed=False
        self.merchant_id=canonical_id(config['merchant_id']);self.environment_id=canonical_id(config['environment_id']);self.release_id=config['release_id']
        self.workload_principal_id=canonical_id(config['machine_principal_id']);self.workload_credential_ref=config['workload_credential_ref'];self._maintenance=maintenance;self._test_control_token=object()
        self.db=PostgresPersistence(dsn)
        self.identity=PortfolioDemoIdentity(self.db,merchant_id=self.merchant_id,profile=profile,secret_provider=SecretFileProvider(config['totp_root']) if config.get('totp_root') else None,transition_capture=self.capture_guest_handoff,receipt_key_provider=lambda:self.protected_key('identity_receipt_key_ref'))
        self.policy=PortfolioDemoPolicy
        self.authority=HostBoundPgAuthority(self.db,publication_verifier=self.identity.verify_publication_receipt,workload_secret_provider=self.workload_secret,obligation_verifier=self.verify_workload_obligation)
        self.identity.authority=self.authority
        mapping = {'catalog': CatalogService, 'pricing': PricingService, 'inventory': InventoryService, 'cart': CartService, 'orders': OrderService, 'tax': TaxService, 'shipping': ShippingService, 'returns': ReturnsService, 'notifications': NotificationService, 'events': EventService, 'integrations': ExternalIntegrationService}
        self.owners = []
        for name, service_type in mapping.items():
            service = service_type(self.db, authorize=self.authorize, profile=profile)
            service.resource_predicate = self.resource_predicate
            service.release_id = self.release_id
            setattr(self, name, service)
            self.owners.append(service)
        mail_config = config.get('mail')
        self.account_mail = IdentityMailService(self.db, mail_config, key_provider=self.mail_queue_key,
            intent_guard=self.identity.verify_mail_intent, worker_guard=self.mail_worker_guard)
        if mail_config:self.identity.mail_port = self.account_mail
        self.payments=PaymentService(self.db,self.integrations,authorize=self.authorize,profile=profile);self.payments.resource_predicate=self.resource_predicate
        self.checkout=CheckoutService(self.db,self.cart,self.catalog,self.pricing,self.tax,self.shipping,authorize=self.authorize,profile=profile);self.checkout.resource_predicate=self.resource_predicate
        self.inventory.checkout_port=self.checkout.inspect_reservation_input;self.checkout.reservation_port=self.inventory.inspect_reservation;self.checkout.policy_snapshot=POLICY;self.checkout.purchase_state_verifier=self.inspect_checkout_claim
        self.catalog.pricing_port=self.pricing.get_prices;self.catalog.inventory_port=self.inventory.get_availability;self.catalog.media_port=self.asset
        self.cart.catalog_port=lambda ids:self.catalog.get_variants(ids,published_only=True)
        self.pricing.catalog_port=lambda ids:self.catalog.get_variants(ids,published_only=True)
        self.pricing.variant_port=lambda ids,actor:self.catalog.get_variants(ids,actor=actor)
        self.inventory.variant_port=lambda vid,actor:self.catalog.get_variants([vid],actor=actor).get(vid)
        self.inventory.order_port=self.orders.get_order;self.shipping.order_port=self.orders.get_order;self.shipping.provider=self.integrations
        self.shipping.catalog_port=lambda ids:self.catalog.get_variants(ids,published_only=True)
        self.returns.order_port=self.orders.get_order;self.notifications.provider=self.integrations
        self.integrations.test_guard=self.test_control_guard
        self.integrations.callback_secret=lambda:self.protected_key('simulator_callback_key_ref');self.integrations.bind_dispatch(self.provider_dispatch);self.integrations.bind_inspection(self.provider_inspection)
        inspectors={'orders':self.orders.get_receipt,'inventory':self.inventory.inspect_receipt,'payments':lambda evidence,actor=None:self.payments.inspect_receipt(evidence,actor=actor),'shipping':self.shipping.inspect_receipt,'returns':self.returns.get_receipt,'checkout':lambda evidence,actor=None:self.checkout.inspect_receipt(evidence,actor=actor)}
        for owner in self.owners+[self.payments,self.checkout]:owner.receipt_inspectors=dict(inspectors)
        self.notifications.source_port=self.notification_source;self.events.source_port=self.owner_delivery
        for owner in self.owners:
            if owner is not self.events:self.events.bind_publications(owner.owner,owner)
        self.purchase=PurchaseCoordinator(self);self.payments.admission_port=self.purchase.inspect_admission;self.checkout.admission_port=self.purchase.inspect_admission;self.payments.order_port=self.orders.get_receipt;self.payments.inventory_port=self.inventory.inspect_receipt;self.payments.approved_plan_port=self.orders.get_refund_plan;self.refunds=RefundCoordinator(self);self.fulfillment=FulfillmentCoordinator(self);self.return_coordinator=ReturnCoordinator(self);self.recovery=RecoveryCoordinator(self)
        if not maintenance:self.initialize()

    def actor(self,credential):
        context=self.identity.resolve_session(credential)
        if context['kind']=='preauth':return context
        with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,context,'application.actor',context['id'])) as tx:
            association=tx.execute('SELECT cart_id FROM application.cart_associations WHERE principal_id=%s',(context['id'],)).fetchone()
        return dict(context,cart_id=association['cart_id'] if association else None,cart_grant=None)

    def admit_obligation(self,tx,id,kind,principal_id,resource_id,resources,operations):
        canonical_id(id)
        if not isinstance(operations,(list,tuple,set)) or not operations or any(not isinstance(x,str) for x in operations):raise ValueError('finite_obligation_operations_required')
        resources=sorted(set(str(x) for x in resources if x is not None)|{str(resource_id),id})
        digest=hashlib.sha256(json.dumps([id,kind,principal_id,resource_id,resources,sorted(operations)],separators=(',',':')).encode()).hexdigest()
        row=tx.execute('INSERT INTO application.obligations(id,kind,principal_id,resource_id,resources,operations,admission_receipt,state) VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,\'ADMITTED\') ON CONFLICT(id) DO NOTHING RETURNING *',(id,kind,principal_id,str(resource_id),json.dumps(resources),json.dumps(sorted(operations)),digest)).fetchone()
        if not row:
            row=tx.execute('SELECT * FROM application.obligations WHERE id=%s',(id,)).fetchone()
            if row['admission_receipt']!=digest:raise ValueError('idempotency_conflict')
        return dict(row)

    def admit_restored_instance(self,*,source_environment_id,backup_sha256,independent_evidence_sha256,run_id):
        self.test_control_guard(self._test_control_token,run_id,'PAC33-restore-admit')
        canonical_id(source_environment_id)
        if source_environment_id==self.environment_id or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in (backup_sha256,independent_evidence_sha256)):raise ValueError('restore_identity_and_evidence_required')
        self.verify_schemas()
        with self.db.transaction('application') as tx:
            row=tx.execute('SELECT * FROM application.instance WHERE singleton=1 FOR UPDATE').fetchone()
            if not row or (row['environment_id'],row['merchant_id'],row['profile'])!=(source_environment_id,self.merchant_id,self.profile):raise ValueError('restore_source_instance_mismatch')
            tx.execute("UPDATE application.release_state SET state='RECOVERY_HOLD',release_id=%s WHERE id=1",(self.release_id,))
            tx.execute('UPDATE application.instance SET environment_id=%s,release_id=%s WHERE singleton=1 AND environment_id=%s',(self.environment_id,self.release_id,source_environment_id))
            tx.stage_audit(step_id(run_id,'restore-admission'),{'operation':'restore_admission','source_environment_id':source_environment_id,'replacement_environment_id':self.environment_id,'backup_sha256':backup_sha256,'independent_evidence_sha256':independent_evidence_sha256,'state':'RECOVERY_HOLD','session_epoch_reset':'REQUIRED_BEFORE_RELEASE','independent_reconciliation':'REQUIRED_BEFORE_RELEASE'})
        self.closed=True
        return {'state':'RECOVERY_HOLD','replacement_environment_id':self.environment_id,'session_epoch_reset':'REQUIRED_BEFORE_RELEASE','independent_reconciliation':'REQUIRED_BEFORE_RELEASE','public_admission':'DENIED','swap':'NOT_PERFORMED'}

    def asset(self,asset_id):
        assets=self.config.get('assets',[])
        record=next((dict(a) for a in assets if a.get('id')==asset_id),None)
        if not record:raise ValueError('unknown_asset')
        path=(ROOT/'app/views/public/demo'/record['filename']).resolve()
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=record['checksum']:raise ValueError('asset_integrity_failed')
        return dict(record,sha256=record['checksum'],object_id=asset_id,state='clean',version=record['checksum'],public_url=record['url'],synthetic=True)

    def authorize(self,tx,actor,operation,resource_id):
        release=tx.execute('SELECT state FROM application.release_state WHERE id=1 FOR SHARE').fetchone()
        if not release or release['state']=='RECOVERY_HOLD':raise PermissionError('instance_recovery_hold')
        if self.closed:raise PermissionError('admission_closed')
        if not isinstance(actor,dict) or actor.get('merchant_id')!=self.merchant_id:raise PermissionError('unauthenticated')
        if actor.get('kind')=='recovery':
            context={'merchant_id':self.merchant_id,'machine_principal_id':actor['id'],'service_id':'sme-worker','credential_ref':actor['credential_ref'],'operation':operation,'resource_id':str(resource_id),'obligation_id':actor['obligation_id'],'expected_epochs':actor['expected_epochs']}
            token=CURRENT_WORKLOAD_ACTOR.set(actor)
            try:return self.authority.workload_guard(**context).admit(tx)
            finally:CURRENT_WORKLOAD_ACTOR.reset(token)
        permission=permission_for(operation)
        if permission is None:raise PermissionError('operation_not_allowed')
        def evaluate(snapshots,owner,action):
            native=self.identity.admit_session(tx,actor)
            if snapshots['session']['data'].get('roles')!=native['roles']:return False
            if actor['kind']=='staff' and native['auth_strength']!='password+totp':return False
            return bool(set(native['roles'])&permission) and self.policy.evaluate(snapshots,owner,action,resource_allowed=True)
        return self.authority.guard(merchant_id=self.merchant_id,principal_id=actor['id'],session_id=actor['session_id'],service_id='sme-web',policy_evaluator=evaluate,action=operation,guest=actor['kind']=='guest').admit(tx)

    def bind_test_checkpoint(self,callback,*,run_id,case_id):
        self.test_control_guard(self._test_control_token,run_id,case_id)
        return self.integrations.bind_test_checkpoint(callback,run_id=run_id,case_id=case_id,actor=self._test_control_token)


    def capture_guest_handoff(self,old_actor,new_principal,operation_id):
        if not old_actor or old_actor.get('kind')!='guest':return None
        old=self.enrich_actor(old_actor)
        if not old.get('cart_id'):return None
        cart=self.cart.get_cart(old['cart_id'],actor=old)
        return self.cart.capture_guest_handoff(old['cart_id'],new_principal,cart['version'],actor=old,command_id=operation_id)

    def cart_view(self,actor):
        if not actor or not actor.get('cart_id'):raise ValueError('cart_not_available')
        cart=self.cart.get_cart(actor['cart_id'],actor=actor);view=dict(cart)
        products=self.catalog_view()['products'];variants={v['id']:(p,v) for p in products for v in p['variants']};lines=[]
        for line in cart['lines']:
            product,variant=variants.get(line['variant_id'],({'name':'Unavailable object'},{'unit_minor':None,'available':0,'sku':''}))
            lines.append(dict(line,product_name=product['name'],sku=variant['sku'],unit_minor=variant['unit_minor'],available=variant['available']))
        view['lines']=lines;view['subtotal_minor']=sum((line['unit_minor'] or 0)*line['quantity'] for line in lines);return view

    def catalog_view(self,query=None,slug=None,filters=None,cursor=None):
        filters=dict(filters or {});
        if query is not None:filters['query']=query
        result=self.catalog.get_public_product(slug) if slug else self.catalog.list_public_products(filters,cursor)
        if slug and result.get('redirect'):return result
        products=[result] if slug else result.get('items',result.get('products',[]));ids=[v['id'] for p in products for v in p.get('variants',[])]
        prices=self.pricing.get_prices(ids);stock=self.inventory.get_availability(ids)
        views=[]
        for product in products:
            p=dict(product);p['category']=p.get('attributes',{}).get('category','Essentials');p['variants']=[dict(v,unit_minor=prices.get(v['id'],{}).get('unit_minor'),available=stock.get(v['id'],{}).get('available',0),stock_revision=stock.get(v['id'],{}).get('version'),price_revision=prices.get(v['id'],{}).get('version')) for v in p.get('variants',[])];views.append(p)
        if slug:return views[0]
        return {'products':views,'categories':result.get('categories',sorted({p['category'] for p in views})),'next_cursor':result.get('next_cursor')}

    def close(self):
        self.closed=True;self.identity.close();return {'state':'DRAINED','unresolved_preserved':True}

    def complete_authentication(self,credential):
        actor=self.actor(credential)
        if actor['kind']=='guest':return self.enrich_actor(actor)
        handoffs=self.cart.pending_handoffs(actor=actor).get('items',[])
        for handoff in handoffs:
            if handoff['state']=='prepared':
                merged=self.cart.merge_guest_cart(handoff['guest_cart_id'],actor['id'],handoff['destination_version'],handoff_receipt=handoff,actor=actor,command_id=step_id(handoff['id'],'qualified-login-merge'))
                with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,actor,'application.actor',actor['id'])) as tx:tx.execute('INSERT INTO application.cart_associations(principal_id,cart_id) VALUES(%s,%s) ON CONFLICT(principal_id) DO UPDATE SET cart_id=excluded.cart_id',(actor['id'],merged['id']))
        return actor if actor['kind']=='staff' else self.enrich_actor(actor)

    def dispatch_permit(self,tx,obligation_id,step_id_value,owner,operation,resource_id,lease_token):
        if tx.owner!='application':raise PermissionError('coordinator_transaction_required')
        row=tx.execute('SELECT * FROM application.obligations WHERE id=%s FOR UPDATE',(obligation_id,)).fetchone()
        now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
        action=owner+'.'+operation
        if not row or row['state'] not in ('ADMITTED','PENDING','UNKNOWN','REVIEW') or row['lease_token']!=lease_token or not row['lease_until'] or row['lease_until']<=now or action not in row['operations'] or str(resource_id) not in row['resources']:raise PermissionError('current_dispatch_authority_required')
        ident=str(uuid.uuid5(uuid.UUID(step_id_value),action+':'+str(resource_id)+':'+lease_token))
        expiry=min(row['lease_until'],now+timedelta(seconds=15))
        tx.execute('INSERT INTO application.dispatch_permits(id,obligation_id,step_id,owner,operation,resource_id,lease_token,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(ident,obligation_id,step_id_value,owner,action,str(resource_id),lease_token,expiry))
        return {'id':ident,'state':'ADMITTED','expires_at':expiry.isoformat()}

    def enrich_actor(self,actor):
        if not actor or actor.get('kind') in ('preauth','recovery'):raise PermissionError('unauthenticated')
        if actor.get('kind')=='staff':return actor
        with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,actor,'application.actor',actor['id'])) as tx:
            row=tx.execute('SELECT cart_id FROM application.cart_associations WHERE principal_id=%s',(actor['id'],)).fetchone()
        if not row:
            cart=self.cart.create_guest_cart(account_id=actor['id'] if actor['kind']=='customer' else None,actor=actor,command_id=step_id(actor['id'],'account-cart'))
            with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,actor,'application.actor',actor['id'])) as tx:tx.execute('INSERT INTO application.cart_associations(principal_id,cart_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(actor['id'],cart['id']))
            row={'cart_id':cart['id']}
        return dict(actor,cart_id=row['cart_id'],cart_grant=None)

    def extend_obligation(self,tx,id,resources):
        row=tx.execute('SELECT resources FROM application.obligations WHERE id=%s FOR UPDATE',(id,)).fetchone()
        if not row:raise PermissionError('obligation_missing')
        values=sorted(set(row['resources'])|{str(x) for x in resources if x is not None});tx.execute('UPDATE application.obligations SET resources=%s::jsonb WHERE id=%s',(json.dumps(values),id))

    def initialize(self):
        self.verify_schemas()
        with self.db.transaction('application') as tx:
            row=tx.execute('SELECT * FROM application.instance WHERE singleton=1').fetchone()
            if not row or row['environment_id']!=self.environment_id or row['merchant_id']!=self.merchant_id or row['profile']!=self.profile or row['release_id']!=self.release_id:raise ValueError('instance_identity_mismatch')
            release=tx.execute('SELECT state FROM application.release_state WHERE id=1').fetchone()
            if not release or release['state']=='RECOVERY_HOLD':raise ValueError('instance_recovery_hold')

    def inject_test_fault(self,provider,operation,fault,*,run_id,case_id,effect_key,count=1):
        self.test_control_guard(self._test_control_token,run_id,case_id)
        return self.integrations.inject_fault(provider,operation,fault,count,run_id=run_id,case_id=case_id,effect_key=effect_key,actor=self._test_control_token)

    def inspect_checkout_claim(self,tx,checkout_id,attempt_id):
        if tx.owner not in ('checkout','application'):raise PermissionError('checkout_claim_verifier_owner_required')
        row=tx.execute('SELECT w.id,w.checkout_id,w.state AS workflow_state,w.progress->\'confirmation\' AS confirmation,o.state,o.admission_receipt FROM application.purchase_workflows w JOIN application.obligations o ON o.id=w.id WHERE w.id=%s AND w.checkout_id=%s AND o.kind=\'purchase\' FOR SHARE OF w,o',(attempt_id,checkout_id)).fetchone()
        if not row:raise ValueError('actual_purchase_admission_missing')
        return {'checkout_id':row['checkout_id'],'attempt_id':row['id'],'state':row['state'],'admission_receipt':row['admission_receipt'],'purchase_state':'PAID' if isinstance(row['confirmation'],dict) and row['confirmation'].get('payment_state')=='paid' else row['workflow_state'],'confirmation':row['confirmation']}

    def inspect_obligation(self,tx,id,actor):
        self.authorize(tx,actor,'application.operation',id);row=tx.execute('SELECT * FROM application.obligations WHERE id=%s',(id,)).fetchone()
        if not row or (actor['kind'] not in ('staff','recovery') and row['principal_id']!=actor['id']):raise ValueError('not_found')
        return {'id':row['id'],'kind':row['kind'],'state':row['state'],'resource_id':row['resource_id'],'version':row['version'],'effect_certainty':'unknown' if row['state'] in ('UNKNOWN','PENDING','REVIEW') else 'committed' if row['state']=='COMPLETED' else 'none'}

    def migrate(self):
        if not self._maintenance:raise PermissionError('explicit_maintenance_required')
        self.db.migrate('application',1,DDL);self.db.migrate('application',2,PF_DDL);self.db.migrate('application',3,PF_COORD_DDL);self.db.migrate_delivery();self.authority.migrate();self.identity.migrate()
        for owner in self.owners+[self.payments,self.checkout]:owner.migrate()
        with self.db.transaction('application') as tx:
            existing=tx.execute('SELECT * FROM application.instance WHERE singleton=1 FOR UPDATE').fetchone()
            if existing and (existing['environment_id'],existing['merchant_id'],existing['profile'])!=(self.environment_id,self.merchant_id,self.profile):raise ValueError('instance_identity_mismatch')
            tx.execute('INSERT INTO application.instance(singleton,environment_id,merchant_id,profile,release_id) VALUES(1,%s,%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET release_id=excluded.release_id',(self.environment_id,self.merchant_id,self.profile,self.release_id))
            tx.execute('UPDATE application.release_state SET release_id=%s WHERE id=1',(self.release_id,))
            tx.stage_audit(step_id(self.environment_id,'migration-release:'+self.release_id),{'operation':'explicit_schema_release_binding','environment_id':self.environment_id,'release_id':self.release_id})

    def notification_source(self,kind,aggregate_id,*,actor):
        if kind!='order_confirmation':raise ValueError('unsupported_notification_source')
        order=self.orders.get_order(aggregate_id,actor=actor);contact=order['snapshot']['contact'];recipient=contact['email']
        if not recipient.endswith('.invalid'):raise ValueError('synthetic_recipient_required')
        return {'owner_id':order['owner_id'],'recipient':recipient,'variables':{'order_id':order['id'],'total_minor':order['total_minor']}}

    def owner_delivery(self,owner,actor):
        allowed={o.owner for o in self.owners}|{'payments','checkout','application','authority','demo_identity'}
        if owner not in allowed:raise ValueError('unknown_delivery_owner')
        verifier=getattr(self.events,'verify_audit_receipt',None)
        return self.db.owner_delivery(owner,lambda tx,operation,resource:self.authorize(tx,actor,owner+'.'+operation,resource),audit_verifier=verifier)

    def protected_key(self,name):
        reference=self.config.get(name)
        if not isinstance(reference,str) or not reference.startswith('file:'):raise PermissionError('protected_key_reference_required')
        raw=read_protected_file(reference[5:]).strip()
        if not re.fullmatch('[0-9a-f]{64}',raw):raise PermissionError('invalid_protected_key')
        return bytes.fromhex(raw)

    def provider_dispatch(self,tx,actor,provider,operation,key,request):
        return self.authorize(tx,actor,'integrations.invoke',key)

    def provider_inspection(self,tx,actor,provider,kind,id,snapshot):
        if provider not in ('stripe','easypost','ses') or kind not in ('effect','object','callback') or not isinstance(actor,dict) or actor.get('kind')!='recovery':raise PermissionError('provider_inspection_denied')
        if snapshot is None:return self.authorize(tx,actor,'integrations.inspect',id)
        if not isinstance(snapshot,dict) or snapshot.get('merchant_id')!=self.merchant_id:raise PermissionError('provider_object_binding_denied')
        admitted=tx.execute('SELECT resources FROM application.obligations WHERE id=%s FOR SHARE',(actor['obligation_id'],)).fetchone()
        # The bound provider owner supplies its current native object metadata.
        # A lost reply can reveal an object ID for the first time; its original
        # admitted attempt/parcel/message remains the authority resource.
        candidates=[str(id),snapshot.get('attempt_id'),snapshot.get('shipment_id'),snapshot.get('aggregate_id')]
        scope=next((value for value in candidates if isinstance(value,str) and admitted and value in admitted['resources']),None)
        if scope is None:raise PermissionError('provider_object_binding_denied')
        return self.authorize(tx,actor,'integrations.inspect',scope)

    def readiness(self):
        result={'profile':self.profile,'release_id':self.release_id,'live_production':'DENIED','external_provider_effects':'DISABLED'}
        try:
            self.initialize()
            with self.db.transaction('application') as tx:
                row=tx.execute('SELECT expires_at>clock_timestamp() AS active FROM application.worker_lease WHERE singleton=1').fetchone()
            ready=bool(row and row['active'] and not self.closed)
            return dict(result,ready=ready,reason=None if ready else 'worker_lease_not_ready')
        except Exception:return dict(result,ready=False,reason='required_capability_or_identity_unavailable')

    def resource_predicate(self,tx,actor,operation,resource):
        if actor.get('kind')!='recovery':return True
        self.verify_workload_obligation(tx,{'merchant_id':self.merchant_id,'machine_principal_id':actor['id'],'service_id':'sme-worker','operation':operation,'resource_id':str(resource),'obligation_id':actor['obligation_id']},actor=actor)
        return True

    def seed_demo(self,*,actor):
        if actor.get('kind')!='staff' or not ('merchant_admin' in actor['roles'] or {'catalog_editor','stock_operator'}<=set(actor['roles'])):raise PermissionError('seed_operator_roles_required')
        revision='pf-demo-six-20261028'
        products=[('Everyday canvas tote','everyday-canvas-tote','A sturdy illustrated canvas tote.','Carry',3800,'Natural','pf-tote'),('Stoneware daily mug','stoneware-daily-mug','An illustrated stoneware mug for the demo.','Home',2800,'Chalk','pf-mug'),('Soft linen throw','soft-linen-throw','A soft illustrated linen throw.','Home',7800,'Oat','pf-linen'),('Pocket notebook set','pocket-notebook-set','Illustrated pocket notebooks for small ideas.','Desk',1800,'Moss','pf-notebook'),('Walnut desk tray','walnut-desk-tray','An illustrated walnut desk tray.','Desk',4600,'Walnut','pf-tray'),('Weekender pouch','weekender-pouch','A compact illustrated cotton pouch.','Carry',2400,'Clay','pf-pouch')]
        with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,actor,'application.seed',actor['id'])) as tx:tx.execute("INSERT INTO application.seed_receipts(revision,state) VALUES(%s,'IN_PROGRESS') ON CONFLICT DO NOTHING",(revision,))
        def step(name,inputs,call):
            with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,actor,'application.seed',actor['id'])) as tx:
                row=tx.execute('SELECT * FROM application.seed_receipts WHERE revision=%s FOR UPDATE',(revision,)).fetchone();steps=dict(row['steps']);entry=steps.get(name)
                if entry and entry['state']=='DONE':return entry['result']
                if entry is None:
                    entry={'state':'PREPARED','input':safe(inputs),'command_id':step_id(revision,name)};steps[name]=entry;tx.execute('UPDATE application.seed_receipts SET steps=%s::jsonb WHERE revision=%s',(json.dumps(steps),revision))
            result=call(entry['input'],entry['command_id'])
            with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,actor,'application.seed',actor['id'])) as tx:
                row=tx.execute('SELECT steps FROM application.seed_receipts WHERE revision=%s FOR UPDATE',(revision,)).fetchone();steps=dict(row['steps']);steps[name]={'state':'DONE','input':entry['input'],'command_id':entry['command_id'],'result':safe(result)};tx.execute('UPDATE application.seed_receipts SET steps=%s::jsonb,updated_at=clock_timestamp() WHERE revision=%s',(json.dumps(steps),revision))
            return result
        for name,slug,description,category,price,color,asset_id in products:
            product=step(slug+':product',{'name':name,'description':description,'slug':slug,'category':category},lambda x,key:self.catalog.create_product(x['name'],x['description'],x['slug'],{'category':x['category'],'tax_code':'demo_physical_standard','synthetic':True,'tags':[revision]},actor=actor,command_id=key))
            current=self.catalog.get_merchant_product(product['id'],actor=actor)
            variant=step(slug+':variant',{'product_id':product['id'],'revision':current['version']},lambda x,key:self.catalog.upsert_variant(x['product_id'],None,'PF-'+slug.upper(),{'Color':color},250,[160,100,50],x['revision'],actor=actor,command_id=key));vid=variant.get('variant_id',variant['id'])
            step(slug+':price',{'variant_id':vid,'price':price},lambda x,key:self.pricing.set_price(x['variant_id'],x['price'],expected_revision=0,actor=actor,command_id=key))
            step(slug+':stock',{'variant_id':vid},lambda x,key:self.inventory.adjust_stock(x['variant_id'],25,'PF1 synthetic opening stock',0,actor=actor,command_id=key))
            current=self.catalog.get_merchant_product(product['id'],actor=actor)
            step(slug+':media',{'product_id':product['id'],'revision':current['version'],'asset_id':asset_id},lambda x,key:self.catalog.attach_media(x['product_id'],x['asset_id'],name,0,x['revision'],actor=actor,command_id=key))
            current=self.catalog.get_merchant_product(product['id'],actor=actor)
            step(slug+':publish',{'product_id':product['id'],'revision':current['version']},lambda x,key:self.catalog.publish(x['product_id'],x['revision'],None,actor=actor,command_id=key))
        with self.db.transaction('application',authority_guard=lambda tx:self.authorize(tx,actor,'application.seed',actor['id'])) as tx:
            row=tx.execute('SELECT * FROM application.seed_receipts WHERE revision=%s FOR UPDATE',(revision,)).fetchone()
            if len(row['steps'])!=36 or any(e['state']!='DONE' for e in row['steps'].values()):raise ValueError('incomplete_seed')
            tx.execute("UPDATE application.seed_receipts SET state='COMPLETE',updated_at=clock_timestamp() WHERE revision=%s",(revision,))
        return {'state':'COMPLETE','fixture_revision':revision,'products':6,'steps':36}

    def test_control_guard(self,actor,run_id,case_id):
        if self.profile!='demo-test' or not self._maintenance or actor is not self._test_control_token:raise PermissionError('private_attested_test_control_required')
        if not isinstance(case_id,str) or not re.fullmatch(r'(?:PAC|BM)[0-9]{2}(?:-[A-Za-z0-9_-]+)?',case_id):raise ValueError('registered_test_case_required')
        guard = DisposableResourceGuard(self.config['disposable_proof_file'], run_id=run_id, environment_id=self.environment_id, profile='demo-test')
        guard.attest_dsn(self._instance_dsn)
        return True

    def verify_schemas(self):
        self.db.initialize();self.db.verify_migration('application',1,DDL,allow_newer=True);self.db.verify_migration('application',2,PF_DDL,allow_newer=True);self.db.verify_migration('application',3,PF_COORD_DDL);self.db.verify_migration('persistence_delivery',1,self.db.DELIVERY_DDL);self.authority.initialize();self.identity.initialize()
        for owner in self.owners+[self.payments,self.checkout]:owner.initialize()

    def verify_workload_obligation(self,tx,context,actor=None):
        row=tx.execute('SELECT * FROM application.obligations WHERE id=%s FOR SHARE',(context['obligation_id'],)).fetchone()
        if not row or (row['state'] not in ('ADMITTED','PENDING','UNKNOWN','REVIEW') and not (row['state']=='COMPLETED' and context['operation']=='application.step' and tx.owner=='application' and str(context['resource_id'])==row['id'])) or context['machine_principal_id']!=self.workload_principal_id or context['merchant_id']!=self.merchant_id or context['service_id']!='sme-worker' or context['operation'] not in row['operations'] or str(context['resource_id']) not in row['resources']:raise PermissionError('workload_obligation_denied')
        actor=actor or CURRENT_WORKLOAD_ACTOR.get()
        if actor and actor.get('worker_token'):
            worker=tx.execute('SELECT token,process_id,expires_at>clock_timestamp() AS active FROM application.worker_lease WHERE singleton=1 FOR SHARE').fetchone()
            if not worker or worker['token']!=actor['worker_token'] or worker['process_id']!=str(os.getpid()) or not worker['active']:raise PermissionError('current_original_worker_lease_required')
        read_operations={'application.purchase.read','application.recovery.read','application.operation','application.inspect_obligation','orders.get_order','orders.get_receipt','orders.get_refund_plan','inventory.inspect_reservation','inventory.inspect_checkout_reservation','inventory.inspect_allocation','inventory.inspect_receipt','inventory.get_availability','pricing.get_quote','pricing.inspect_redemption_hold','checkout.get_checkout','checkout.inspect_receipt','checkout.inspect_purchase_claim','checkout.inspect_reservation_input','cart.get_cart','payments.inspect_attempt','payments.inspect_refund','payments.inspect_receipt','integrations.inspect','shipping.get_shipment','shipping.get_offer','shipping.get_receipt','shipping.inspect_receipt','shipping.inspect_cancel','shipping.get_unit_status','returns.get_return','returns.get_refund_plan','returns.get_receipt','tax.get_calculation','tax.inspect_transaction','notifications.get_delivery_status'}
        read_operations|={owner+'.'+operation for owner in ('catalog','pricing','inventory','cart','orders','tax','shipping','returns','notifications','events','integrations','payments','checkout','application','authority','demo_identity') for operation in ('inspect_publication','inspect_audit')}
        control_operations={name+'.'+verb for name in ('catalog','pricing','inventory','cart','orders','tax','shipping','returns','notifications','events','integrations','payments','checkout','application','authority','demo_identity') for verb in ('pending','safety_snapshot','claim_publications','claim_audits','inspect_publication','inspect_audit','ack_publication','defer_publication','ack_audit','defer_audit')}
        control=row['kind']=='worker_control' and context['operation'] in control_operations
        coordinator_control=context['operation']=='application.step' and tx.owner=='application' and str(context['resource_id'])==row['id']
        if context['operation'] not in read_operations and not control and not coordinator_control:
            if not actor or not actor.get('lease_token') or row['lease_token']!=actor['lease_token'] or row['lease_until'] is None or row['lease_until']<=tx.execute('SELECT clock_timestamp() now').fetchone()['now']:raise PermissionError('current_dispatch_lease_required')
            parents={'shipping.label_intent':'shipping.purchase_label','shipping.label_unknown':'shipping.purchase_label','shipping.label_purchased':'shipping.purchase_label','notifications.dispatch_unknown':'notifications.dispatch_claim','notifications.dispatch_settle':'notifications.dispatch_claim','payments.authorize':'payments.create_authorization','payments.cancel':'payments.cancel_authorization'}
            accepted=[context['operation']]+([parents[context['operation']]] if context['operation'] in parents else [])
            permit=tx.execute('SELECT * FROM application.dispatch_permits WHERE obligation_id=%s AND operation=ANY(%s) AND resource_id=%s AND lease_token=%s AND expires_at>clock_timestamp() AND state=\'ADMITTED\' ORDER BY issued_at DESC LIMIT 1 FOR SHARE',(row['id'],accepted,str(context['resource_id']),actor['lease_token'])).fetchone()
            if not permit or (context['operation']!='integrations.invoke' and actor.get('dispatch_permit_id')!=permit['id']):raise PermissionError('current_dispatch_permit_required')
        return {**context,'admission_receipt':row['admission_receipt'],'operations':row['operations'],'resources':row['resources']}

    def workload_actor(self,obligation_id,*,lease_token=None,dispatch_permit_id=None):
        return {'id':self.workload_principal_id,'kind':'recovery','roles':[],'merchant_id':self.merchant_id,'obligation_id':obligation_id,'credential_ref':self.workload_credential_ref,'expected_epochs':self.authority.workload_epochs(self.merchant_id,self.workload_principal_id,'sme-worker',self.workload_credential_ref)['expected_epochs'],'lease_token':lease_token,'dispatch_permit_id':dispatch_permit_id,'worker_token':CURRENT_WORKER_LEASE.get()}

    def workload_secret(self,reference):
        if reference!=self.workload_credential_ref or not reference.startswith('file:'):raise PermissionError('workload_reference_denied')
        raw=read_protected_file(reference[5:]).strip()
        if not re.fullmatch('[0-9a-f]{64}',raw):raise PermissionError('invalid_workload_credential')
        return bytes.fromhex(raw)

    def authorize_command(self, actor):
        with self.db.transaction('application', authority_guard=lambda tx: self.authorize(tx, actor, 'application.command', actor['id'])):
            pass

    def operation_status(self, identifier, actor):
        with self.db.transaction('application') as tx:
            return self.inspect_obligation(tx, canonical_id(identifier), actor)

    def mail_queue_key(self):
        mail=self.config.get('mail')
        if not mail or not isinstance(mail.get('queue_key_ref'),str) or not mail['queue_key_ref'].startswith('file:/'):
            raise PermissionError('protected_mail_queue_key_required')
        return read_protected_file(mail['queue_key_ref'][5:]).strip()

    def mail_worker_guard(self, tx, worker_token):
        """Mail claims/finalization use only this process's current private worker lease."""
        if self.closed or tx.owner!='notifications' or getattr(tx,'_persistence',None) is not self.db or not tx.active:
            raise PermissionError('native_mail_worker_transaction_required')
        instance=tx.execute('SELECT * FROM application.instance WHERE singleton=1 FOR SHARE').fetchone()
        release=tx.execute('SELECT state FROM application.release_state WHERE id=1 FOR SHARE').fetchone()
        worker=tx.execute('SELECT token,process_id,expires_at>clock_timestamp() AS active,GREATEST(0,floor(extract(epoch FROM (expires_at-clock_timestamp()))*1000))::bigint AS lease_remaining_ms FROM application.worker_lease WHERE singleton=1 FOR SHARE').fetchone()
        if not instance or (instance['environment_id'],instance['merchant_id'],instance['profile'],instance['release_id'])!=(self.environment_id,self.merchant_id,self.profile,self.release_id):
            raise PermissionError('mail_instance_binding_denied')
        if not release or release['state']=='RECOVERY_HOLD' or not worker or worker['token']!=worker_token or worker['process_id']!=str(os.getpid()) or not worker['active']:
            raise PermissionError('current_native_mail_worker_required')
        # Check current finite native machine credentials in addition to the
        # private current-worker proof and the mail owner's exact token intent.
        actor=self.workload_actor(self.recovery._control_id())
        actor['worker_token']=worker_token
        self.authorize(tx,actor,'notifications.pending','notifications')
        return {'state':'CURRENT','worker_token':worker_token,'lease_remaining_ms':worker['lease_remaining_ms']}
