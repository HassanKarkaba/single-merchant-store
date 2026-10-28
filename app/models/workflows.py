"""Durable purchase, fulfillment, refund, return and recovery domain workflows."""
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

class _PendingStep(RuntimeError):pass

class CoordinatorBase:
    def __init__(self,app):self.app=app

    def _claim(self,id):
        actor=self.app.workload_actor(id)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',id)) as tx:
            row=tx.execute('SELECT * FROM application.obligations WHERE id=%s FOR UPDATE',(id,)).fetchone()
            if not row or row['state'] not in ('ADMITTED','PENDING','UNKNOWN','REVIEW'):return None
            now=tx.execute('SELECT clock_timestamp() now').fetchone()['now']
            if row['lease_until'] and row['lease_until']>now:return None
            token=str(uuid.uuid4());tx.execute("UPDATE application.obligations SET lease_token=%s,lease_until=clock_timestamp()+interval '5 seconds',attempts=attempts+1 WHERE id=%s",(token,id));return token

    def _defer(self,id,token,reason,state='PENDING'):
        actor=self.app.workload_actor(id,lease_token=token)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',id)) as tx:
            row=tx.execute('SELECT * FROM application.obligations WHERE id=%s FOR UPDATE',(id,)).fetchone()
            if not row or row['lease_token']!=token or row['lease_until'] is None or row['lease_until']<=tx.execute('SELECT clock_timestamp() now').fetchone()['now']:return
            tx.execute("UPDATE application.obligations SET state=%s,last_reason=%s,eligible_at=clock_timestamp()+interval '1 second',lease_token=NULL,lease_until=NULL,version=version+1 WHERE id=%s",(state,reason,id))
            table='application.purchase_workflows' if row['kind']=='purchase' else 'application.flow_workflows'
            tx.execute('UPDATE '+table+' SET last_reason=%s,eligible_at=clock_timestamp()+interval \'1 second\',version=version+1 WHERE id=%s',(reason,id))

    def _extend(self,id,token,values):
        actor=self.app.workload_actor(id,lease_token=token)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',id)) as tx:
            current=tx.execute('SELECT lease_token,lease_until FROM application.obligations WHERE id=%s FOR UPDATE',(id,)).fetchone()
            if not current or current['lease_token']!=token or current['lease_until'] is None or current['lease_until']<=tx.execute('SELECT clock_timestamp() now').fetchone()['now']:raise RuntimeError('stale_worker')
            self.app.extend_obligation(tx,id,values)

    def _flow_admit(self,kind,operation,resource,data,command_id,actor,owner_id=None,permission=None):
        canonical_id(command_id);digest=_coord_digest([operation,resource,data]);id=step_id(actor['id']+':'+command_id,operation)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,permission or 'application.command',resource)) as tx:
            previous=tx.execute('SELECT * FROM application.client_commands WHERE principal_id=%s AND command_id=%s AND operation=%s FOR UPDATE',(actor['id'],command_id,operation)).fetchone()
            if previous:
                if previous['digest']!=digest:raise ValueError('idempotency_conflict')
                return previous['workflow_id']
            tx.execute('INSERT INTO application.flow_workflows(id,kind,owner_id,resource_id,input,input_digest) VALUES(%s,%s,%s,%s,%s::jsonb,%s)',(id,kind,owner_id or actor['id'],resource,json.dumps(data),digest));self.app.admit_obligation(tx,id,kind,owner_id or actor['id'],resource,_coord_refs(data)|{resource,id},PF_WORK_OPS);tx.execute('INSERT INTO application.client_commands VALUES(%s,%s,%s,%s,%s)',(actor['id'],command_id,operation,digest,id));tx.stage_audit(step_id(id,'admission-audit'),{'operation':operation,'principal_id':actor['id'],'resource_id':resource,'workflow_id':id});return id

    def _get(self,id,actor):
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.operation',id)) as tx:
            row=tx.execute('SELECT * FROM application.flow_workflows WHERE id=%s',(id,)).fetchone()
            if not row or (actor['kind'] not in ('staff','recovery') and row['owner_id']!=actor['id']):raise ValueError('not_found')
            return dict(row)

    def _progress(self,id,kind='purchase'):
        actor=self.app.workload_actor(id)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',id)) as tx:
            row=tx.execute('SELECT * FROM '+('application.purchase_workflows' if kind=='purchase' else 'application.flow_workflows')+' WHERE id=%s',(id,)).fetchone()
            if not row:raise ValueError('not_found')
            return dict(row)

    def _state(self,id,token,state,kind='purchase',done=False):
        actor=self.app.workload_actor(id,lease_token=token)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',id)) as tx:
            current=tx.execute('SELECT * FROM application.obligations WHERE id=%s FOR UPDATE',(id,)).fetchone()
            if not current or current['lease_token']!=token or current['lease_until'] is None or current['lease_until']<=tx.execute('SELECT clock_timestamp() now').fetchone()['now']:raise RuntimeError('stale_worker')
            tx.execute('UPDATE '+('application.purchase_workflows' if kind=='purchase' else 'application.flow_workflows')+' SET state=%s,version=version+1 WHERE id=%s',(state,id));tx.execute("UPDATE application.obligations SET state=%s,version=version+1,eligible_at=clock_timestamp()+interval '1 second',lease_token=NULL,lease_until=NULL WHERE id=%s",('COMPLETED' if done else 'PENDING',id))

    def _step(self,id,token,name,owner,operation,resource,call,*,kind='purchase',accept=None,extra_permits=()):
        row=self._progress(id,kind);progress=row['progress']
        if name in progress:return progress[name]
        actor=self.app.workload_actor(id,lease_token=token)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',id)) as tx:
            current=tx.execute('SELECT lease_token,lease_until FROM application.obligations WHERE id=%s FOR UPDATE',(id,)).fetchone()
            if not current or current['lease_token']!=token or current['lease_until']<=tx.execute('SELECT clock_timestamp() now').fetchone()['now']:raise RuntimeError('stale_worker')
            tx.execute("UPDATE application.obligations SET lease_until=clock_timestamp()+interval '5 seconds' WHERE id=%s",(id,))
            self.app.extend_obligation(tx,id,{resource,step_id(id,name)}|{p[2] for p in extra_permits});permit=self.app.dispatch_permit(tx,id,step_id(id,name),owner,operation,str(resource),token)
            for p_owner,p_operation,p_resource in extra_permits:self.app.dispatch_permit(tx,id,step_id(id,name+':'+p_owner+':'+p_operation),p_owner,p_operation,str(p_resource),token)
        dispatch=self.app.workload_actor(id,lease_token=token,dispatch_permit_id=permit['id']);result=safe(call(dispatch))
        if not isinstance(result,dict):raise ValueError('invalid_owner_receipt')
        complete=accept(result) if accept else True
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,dispatch,'application.step',id)) as tx:
            current=tx.execute('SELECT * FROM application.obligations WHERE id=%s FOR UPDATE',(id,)).fetchone()
            if not current or current['lease_token']!=token or current['lease_until']<=tx.execute('SELECT clock_timestamp() now').fetchone()['now']:raise RuntimeError('stale_worker')
            latest=tx.execute('SELECT progress FROM '+('application.purchase_workflows' if kind=='purchase' else 'application.flow_workflows')+' WHERE id=%s FOR UPDATE',(id,)).fetchone()['progress'];self.app.extend_obligation(tx,id,_coord_refs(result))
            if complete:
                old=tx.execute('SELECT receipt FROM application.coordinator_steps WHERE workflow_id=%s AND step=%s',(id,name)).fetchone()
                if old and old['receipt']!=result:raise RuntimeError('step_receipt_changed')
                tx.execute('INSERT INTO application.coordinator_steps(workflow_id,step,receipt) VALUES(%s,%s,%s::jsonb) ON CONFLICT DO NOTHING',(id,name,json.dumps(result)));latest[name]=result
            else:latest.setdefault('pending_steps',{})[name]=result
            tx.execute('UPDATE '+('application.purchase_workflows' if kind=='purchase' else 'application.flow_workflows')+' SET progress=%s::jsonb,version=version+1 WHERE id=%s',(json.dumps(latest),id));
            if row.get('kind')!='delivery':tx.stage_audit(step_id(id,'step-audit:'+name+':'+str(result.get('version',1))),{'workflow_id':id,'step':name,'owner':owner,'receipt_id':result.get('receipt_id',result.get('id'))})
        if not complete:raise _PendingStep(name)
        return result

class FulfillmentCoordinator(CoordinatorBase):

    def cancel_units(self,order_id,unit_ids,reason,revision,command_id,*,actor):
        order=self.app.orders.get_order(order_id,actor=actor);require_revision(revision)
        if order['version']!=revision:raise ValueError('version_conflict')
        valid={u['unit_id'] for u in order['unit_records']}
        if not isinstance(unit_ids,list) or not unit_ids or len(unit_ids)!=len(set(unit_ids)) or not set(unit_ids)<=valid:raise ValueError('invalid_units')
        id=self._flow_admit('cancellation','purchase.cancel',order_id,{'order_id':order_id,'unit_ids':unit_ids,'reason':reason,'order_revision':revision},command_id,actor,order['owner_id'],'application.cancellation.submit');self.run(id);return self.get_status(id,actor=actor)
    def command(self,operation,data,revision,command_id,*,actor):
        if operation=='quote_parcel':return self.app.shipping.quote_for_order(data['order_id'],data['unit_ids'],actor=actor,command_id=command_id)
        if operation=='plan_shipment':
            order=self.app.orders.get_order(data['order_id'],actor=actor);require_revision(revision)
            if order['version']!=revision:raise ValueError('version_conflict')
            offer=self.app.shipping.get_offer(data['offer_id'],actor=actor);payload={'operation':operation,'order_id':order['id'],'unit_ids':data['unit_ids'],'offer_id':data['offer_id'],'package':offer['package']};resource=order['id'];permission='application.fulfillment.submit'
        else:
            shipment=self.app.shipping.get_shipment(data['shipment_id'],actor=actor);require_revision(revision)
            if shipment['version']!=revision:raise ValueError('version_conflict')
            payload={'operation':operation,'shipment_id':shipment['id'],'order_id':shipment['order_id'],'expected_revision':revision,'status':data.get('status')};resource=shipment['id'];permission='application.fulfillment.submit';order=self.app.orders.get_order(shipment['order_id'],actor=actor)
        id=self._flow_admit('fulfillment','fulfillment.'+operation,resource,payload,command_id,actor,order['owner_id'],permission);self.run(id);return self.get_status(id,actor=actor)

    def get_status(self,id,*,actor):
        row=self._get(id,actor);progress=row['progress'];shipment=progress.get('shipment',{});return {'id':id,'operation_id':id,'version':row['version'],'state':row['state'],'order_id':row['input'].get('order_id'),'shipment_id':shipment.get('id',row['input'].get('shipment_id')),'refund_operation_id':progress.get('refund_admission',{}).get('id'),'result':progress.get('result') or progress.get('handover') or shipment,'effect_certainty':'unknown' if row['state'] in ('PENDING','UNKNOWN','REVIEW') else 'committed','status_url':'/api/v1/operations/'+id}

    def run(self,id):
        token=self._claim(id)
        if token is None:return {'id':id,'state':'BUSY'}
        try:
            row=self._progress(id,'flow');data=row['input'];operation=data.get('operation');ctx=self.app.workload_actor(id,lease_token=token)
            if row['kind']=='cancellation':
                order=self.app.orders.get_order(data['order_id'],actor=ctx)
                fence=self._step(id,token,'cancel_fence','shipping','cancel_units',order['id'],lambda a:self.app.shipping.cancel_units(order['id'],data['unit_ids'],data['reason'],actor=a,command_id=step_id(id,'cancel_fence')),kind='flow')
                plan=self._step(id,token,'unit_plan','orders','plan_units',order['id'],lambda a:self.app.orders.plan_units(order['id'],data['unit_ids'],'cancellation',data['reason'],fence,merchant_fault=data['reason'] in ('merchant_cancel','wrong_item','damaged'),expected_revision=data['order_revision'],actor=a,command_id=step_id(id,'unit_plan')),kind='flow')
                allocation=self.app.inventory.inspect_allocation(order_id=order['id'],actor=ctx);quantities=_coord_quantities(plan['unit_allocations'])
                self._extend(id,token,{allocation['id']})
                self._step(id,token,'stock_release','inventory','release_allocation',order['id'],lambda a:self.app.inventory.release_allocation(order['id'],quantities,'Paid cancellation with approved refund obligation',plan,actor=a,command_id=step_id(id,'stock_release')),kind='flow')
                refund=self.app.refunds.admit_approved_plan(plan,id,ctx,return_id=None)
                self._extend(id,token,{refund['id']})
                self._step(id,token,'refund_admission','application','step',id,lambda a:refund,kind='flow');self._state(id,token,'CANCELED_REFUND_PENDING','flow',done=True);return {'id':id,'state':'CANCELED_REFUND_PENDING','refund_operation_id':refund['id']}
            order=self.app.orders.get_order(data['order_id'],actor=ctx)
            if operation=='plan_shipment':
                offer=self.app.shipping.get_offer(data['offer_id'],actor=ctx);allocation=self.app.inventory.inspect_allocation(order_id=order['id'],actor=ctx);self._extend(id,token,{allocation['id']});lines=_coord_lines(order,data['unit_ids'])
                self._step(id,token,'shipment','shipping','create_shipment',order['id'],lambda a:self.app.shipping.create_shipment(order['id'],lines,data['package'],offer['id'],allocation_id=allocation['id'],actor=a,command_id=step_id(id,'shipment')),kind='flow');self._state(id,token,'CREATED','flow',done=True);return {'id':id,'state':'CREATED'}
            shipment=self.app.shipping.get_shipment(data['shipment_id'],actor=ctx);self._extend(id,token,_coord_refs(shipment))
            if operation=='purchase_label':
                key=shipment['snapshot']['label_effect_key'];create_key=str(uuid.uuid5(uuid.UUID(key),'create'));self._extend(id,token,{key,create_key})
                self._step(id,token,'result','shipping','purchase_label',shipment['id'],lambda a:self.app.shipping.purchase_label(shipment['id'],step_id(id,'label'),actor=a),kind='flow',accept=lambda r:r.get('label_state')=='purchased',extra_permits=[('integrations','invoke',key),('integrations','invoke',create_key)])
            elif operation=='reserve_handover':self._step(id,token,'result','shipping','reserve_handover',shipment['id'],lambda a:self.app.shipping.reserve_handover(shipment['id'],data['expected_revision'],actor=a,command_id=step_id(id,'reserve')),kind='flow')
            elif operation=='confirm_handover':
                handover=self._step(id,token,'handover','shipping','confirm_handover',shipment['id'],lambda a:self.app.shipping.confirm_handover(shipment['id'],'demo_handover',actor=a,command_id=step_id(id,'handover')),kind='flow')
                movement=self._step(id,token,'stock_movement','inventory','record_shipment',shipment['allocation_id'],lambda a:self.app.inventory.record_shipment(shipment['allocation_id'],shipment['quantities'],shipment['id'],actor=a,command_id=step_id(id,'stock_movement')),kind='flow')
                self._step(id,token,'result','orders','record_fulfillment',order['id'],lambda a:self.app.orders.record_fulfillment(order['id'],handover,inventory_receipt={'id':movement['id'],'kind':'shipment'},actor=a,command_id=step_id(id,'order_shipment')),kind='flow')
            elif operation=='simulate_tracking':
                tracking=self._step(id,token,'tracking','shipping','simulate_tracking',shipment['id'],lambda a:self.app.shipping.simulate_tracking(shipment['id'],data['status'],data['expected_revision'],actor=a,command_id=step_id(id,'tracking')),kind='flow')
                if tracking['tracking_state']=='delivered':self._step(id,token,'result','orders','record_delivery',order['id'],lambda a:self.app.orders.record_delivery(order['id'],tracking,actor=a,command_id=step_id(id,'order_delivery')),kind='flow')
            else:raise ValueError('unsupported_fulfillment_operation')
            self._state(id,token,'COMPLETE','flow',done=True);return {'id':id,'state':'COMPLETE'}
        except _PendingStep:self._defer(id,token,'label_pending');return {'id':id,'state':'PENDING'}
        except Exception as exc:self._defer(id,token,_coord_reason(exc),'UNKNOWN' if isinstance(exc,TimeoutError) else 'REVIEW');return {'id':id,'state':'REVIEW'}

class PurchaseCoordinator(CoordinatorBase):

    def _replacement_outcome(self,id,token,checkout):
        return_id=checkout.get('replacement_return_id')
        if return_id:
            actor=self.app.workload_actor(id,lease_token=token);returned=self.app.returns.get_return(return_id,actor=actor);self._extend(id,token,_coord_refs(returned)|{return_id})
            self._step(id,token,'replacement_outcome','returns','record_exchange_outcome',return_id,lambda a:self.app.returns.record_exchange_outcome(return_id,actor=a,command_id=step_id(id,'replacement_outcome')))

    def compensate(self,id,token,terminal):
        row=self._progress(id);progress=row['progress']
        fenced=self._step(id,token,'capture_fence','payments','cancel_authorization',id,lambda a:self.app.payments.fence_capture(id,actor=a),extra_permits=[('integrations','invoke',self.app.payments.effect_id(id,'cancel'))],accept=lambda r:str(r.get('state')).upper() not in ('UNKNOWN','PENDING','PROCESSING'))
        if str(fenced.get('state')).upper() in ('CAPTURED','ZERO_TOTAL','ZERO_SETTLEMENT'):
            self._defer(id,token,'captured_requires_forward_completion','REVIEW');return {'id':id,'state':'REVIEW'}
        cancel=self._step(id,token,'authorization_cancel','payments','cancel_authorization',id,lambda a:self.app.payments.cancel_authorization(id,actor=a),accept=lambda r:str(r.get('state')).upper() in ('CANCELED','DECLINED','FAILED','NOT_CREATED'),extra_permits=[('integrations','invoke',self.app.payments.effect_id(id,'cancel'))])
        if 'allocation' in progress:
            alloc=progress['allocation'];self._step(id,token,'allocation_release','inventory','release_allocation',progress['order']['id'],lambda a:self.app.inventory.release_allocation(progress['order']['id'],alloc['lines'],'Definitive no capture',cancel,actor=a,command_id=step_id(id,'allocation_release')))
        elif 'reservation' in progress:self._step(id,token,'reservation_release','inventory','release_reservation',progress['reservation']['id'],lambda a:self.app.inventory.release_reservation(progress['reservation']['id'],actor=a,command_id=step_id(id,'reservation_release')))
        if 'promotion' in progress and progress['promotion'].get('state')!='not_required':self._step(id,token,'promotion_release','pricing','release_redemption_hold',id,lambda a:self.app.pricing.release_redemption_hold(id,evidence=cancel,actor=a,command_id=step_id(id,'promotion_release')))
        if 'order' in progress:self._step(id,token,'order_failed','orders','mark_payment_failed',progress['order']['id'],lambda a:self.app.orders.mark_payment_failed(progress['order']['id'],cancel,actor=a,command_id=step_id(id,'order_failed')))
        if progress['admission']['checkout'].get('replacement_return_id'):
            actor=self.app.workload_actor(id,lease_token=token)
            with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',id)) as tx:tx.execute('UPDATE application.purchase_workflows SET state=%s WHERE id=%s AND EXISTS(SELECT 1 FROM application.obligations WHERE id=%s AND lease_token=%s)',(terminal,id,id,token))
            self._replacement_outcome(id,token,progress['admission']['checkout'])
        self._state(id,token,terminal,done=True);return {'id':id,'state':terminal}
    def get_status(self,id,*,actor):
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.operation',id)) as tx:
            row=tx.execute('SELECT * FROM application.purchase_workflows WHERE id=%s',(id,)).fetchone()
            if not row or (actor['kind'] not in ('staff','recovery') and row['owner_id']!=actor['id']):raise ValueError('not_found')
            progress=row['progress'];paid=progress.get('confirmation');state='PAID' if paid and paid.get('payment_state')=='paid' else row['state'];return {'id':id,'operation_id':id,'version':row['version'],'state':state,'order_id':progress.get('order',{}).get('id'),'order':paid,'progress':{k:v.get('state') for k,v in progress.items() if isinstance(v,dict) and k!='admission'},'pending_followups':bool(paid and row['state']!='COMPLETE'),'status_url':'/api/v1/purchases/'+id,'retry_after_ms':1000,'effect_certainty':'committed' if paid else 'unknown' if state in ('PENDING','UNKNOWN','REVIEW','AUTHORIZATION_PENDING','CAPTURE_PENDING') else 'none','next_action':'View order' if paid else 'Start a new checkout' if state in ('DECLINED','CANCELED','EXPIRED') else 'Wait for server reconciliation'}

    def inspect_admission(self,attempt_id,*,actor):
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',attempt_id)) as tx:
            row=tx.execute('SELECT * FROM application.purchase_workflows WHERE id=%s',(attempt_id,)).fetchone()
            if not row:raise ValueError('not_found')
            quote=row['progress']['admission']['quote'];frozen=row['progress'].get('frozen_quote',{}).get('frozen_quote_until');frozen=int(datetime.fromisoformat(frozen).timestamp()*1000) if isinstance(frozen,str) else frozen;return {'frozen_quote_until':frozen,'id':attempt_id,'attempt_id':attempt_id,'owner_id':row['owner_id'],'quote_digest':quote['digest'],'total_minor':quote['total_minor'],'currency':'USD','demo_instrument':row['demo_instrument'],'obligation_id':attempt_id,'checkout_id':row['checkout_id'],'accepted_quote_id':row['accepted_quote_id']}

    def run(self,id):
        token=self._claim(id)
        if token is None:return {'id':id,'state':'BUSY'}
        try:
            row=self._progress(id);admission=row['progress']['admission'];checkout=admission['checkout'];quote=admission['quote'];ctx=self.app.workload_actor(id,lease_token=token)
            claim=self._step(id,token,'checkout_claim','checkout','claim_purchase',checkout['id'],lambda a:self.app.checkout.claim_purchase(checkout['id'],quote['id'],id,actor=a,command_id=step_id(id,'checkout_claim')))
            reservation=self._step(id,token,'reservation','inventory','reserve',checkout['id'],lambda a:self.app.inventory.reserve(checkout['id'],checkout['lines'],actor=a,command_id=step_id(id,'reservation')))
            self._step(id,token,'frozen_quote','checkout','bind_reservation',checkout['id'],lambda a:self.app.checkout.bind_reservation(checkout['id'],id,reservation,actor=a))
            self._step(id,token,'promotion','pricing','reserve_redemption',id,lambda a:self.app.pricing.reserve_redemption(quote['pricing_quote_id'],id,expires_at=reservation['expires_at'],actor=a,command_id=step_id(id,'promotion')))
            authorization=self._step(id,token,'authorization','payments','create_authorization',id,lambda a:self.app.payments.create_authorization(id,quote['digest'],quote['total_minor'],'USD',checkout['email'],actor=a,demo_instrument=row['demo_instrument']),accept=lambda r:str(r.get('state')).upper() not in ('PENDING','UNKNOWN','PROCESSING','REQUESTED','AUTHORIZATION_PENDING'),extra_permits=[('integrations','invoke',self.app.payments.effect_id(id,'create_authorization'))])
            order=self._step(id,token,'order','orders','create_provisional',checkout['id'],lambda a:self.app.orders.create_provisional(checkout['id'],quote,{'actor_id':row['owner_id'],'owner_id':row['owner_id'],'email':checkout['email'],'address':checkout['address']},{'offer_id':quote['shipping_quote_id'],'offer':quote.get('shipping_offer'),'amount_minor':quote['shipping_minor']},quote['policy'],actor=a,command_id=step_id(id,'order')))
            if str(authorization.get('state')).upper() in ('DECLINED','FAILED','CANCELED'):
                return self.compensate(id,token,'DECLINED')
            allocation=self._step(id,token,'allocation','inventory','allocate',reservation['id'],lambda a:self.app.inventory.allocate(reservation['id'],order['id'],actor=a,command_id=step_id(id,'allocation')))
            self._step(id,token,'redemption','pricing','commit_redemption',order['id'],lambda a:self.app.pricing.commit_redemption(quote['pricing_quote_id'],order['id'],actor=a,command_id=step_id(id,'redemption')))
            capture=self._step(id,token,'capture','payments','capture',id,lambda a:self.app.payments.capture(id,order,allocation,actor=a),accept=lambda r:str(r.get('state')).upper() in ('CAPTURED','ZERO_SETTLEMENT','ZERO_TOTAL','DECLINED','FAILED','CANCELED'),extra_permits=[('integrations','invoke',self.app.payments.effect_id(id,'capture'))])
            if str(capture.get('state')).upper() in ('DECLINED','FAILED','CANCELED'):return self.compensate(id,token,'DECLINED')
            confirmation=self._step(id,token,'confirmation','orders','confirm_payment',order['id'],lambda a:self.app.orders.confirm_payment(order['id'],capture,allocation,actor=a,command_id=step_id(id,'confirmation')))
            self._step(id,token,'tax','tax','commit_tax',order['id'],lambda a:self.app.tax.commit_tax(order['id'],quote['tax_calculation_id'],capture,actor=a,command_id=step_id(id,'tax')))
            self._step(id,token,'cart','cart','clear_purchased_lines',checkout['cart_id'],lambda a:self.app.cart.clear_purchased_lines(checkout['cart_id'],confirmation,actor=a,command_id=step_id(id,'cart')))
            self._replacement_outcome(id,token,checkout)
            notice=self._step(id,token,'notification','notifications','enqueue_transactional',order['id'],lambda a:self.app.notifications.enqueue_transactional('order_confirmation',order['id'],checkout['email'],'portfolio-v1',{'order_id':order['id'],'total_minor':quote['total_minor']},confirmation['event_id'],actor=a,command_id=step_id(id,'notification')))
            self._extend(id,token,{str(uuid.uuid5(uuid.UUID(notice['id']),'synthetic-delivery'))})
            self._step(id,token,'notification_delivery','notifications','dispatch_claim',notice['id'],lambda a:self.app.notifications.dispatch(notice['id'],actor=a),accept=lambda r:r.get('state')=='simulated_delivered',extra_permits=[('integrations','invoke',str(uuid.uuid5(uuid.UUID(notice['id']),'synthetic-delivery')))])
            self._state(id,token,'COMPLETE',done=True);return {'id':id,'state':'PAID','order_id':order['id']}
        except _PendingStep:
            self._defer(id,token,'owner_pending');return {'id':id,'state':'PENDING'}
        except Exception as exc:
            reason=_coord_reason(exc)
            try:
                if 'expired' in reason or reason in ('unavailable','ineligible','quote_not_accepted','variant_not_sellable','cart_changed_create_new_checkout','quote_expired','reservation_expired'):return self.compensate(id,token,'EXPIRED' if 'expired' in reason else 'FAILED')
                self._defer(id,token,reason,'UNKNOWN' if isinstance(exc,TimeoutError) else 'PENDING')
            except Exception:self._defer(id,token,'reconciliation_required','REVIEW')
            return {'id':id,'state':'PENDING','reason':reason}

    def submit(self,checkout_id,accepted_quote_id,command_id,*,actor,demo_instrument):
        canonical_id(checkout_id);canonical_id(accepted_quote_id);canonical_id(command_id)
        if demo_instrument not in ('demo_success','demo_decline','demo_pending'):raise ValueError('invalid_demo_instrument')
        checkout=safe(self.app.checkout.get_checkout(checkout_id,actor=actor));quote=checkout.get('quote')
        if not isinstance(quote,dict) or quote.get('id')!=accepted_quote_id or checkout.get('accepted_quote')!=accepted_quote_id:raise ValueError('quote_not_accepted')
        digest=_coord_digest([checkout_id,accepted_quote_id,demo_instrument]);attempt=step_id(actor['id']+':'+command_id,'purchase')
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.command',checkout_id)) as tx:
            old=tx.execute("SELECT * FROM application.client_commands WHERE principal_id=%s AND command_id=%s AND operation='purchase.submit' FOR UPDATE",(actor['id'],command_id)).fetchone()
            if old:
                if old['digest']!=digest:raise ValueError('idempotency_conflict')
                attempt=old['workflow_id']
            else:
                existing=tx.execute('SELECT * FROM application.purchase_workflows WHERE checkout_id=%s FOR UPDATE',(checkout_id,)).fetchone()
                if existing:
                    if existing['owner_id']!=actor['id']:raise ValueError('not_found')
                    if existing['accepted_quote_id']!=accepted_quote_id or existing['demo_instrument']!=demo_instrument:raise ValueError('checkout_already_claimed')
                    attempt=existing['id']
                else:
                    payload={'admission':{'quote':quote,'checkout':checkout}};tx.execute("INSERT INTO application.purchase_workflows(id,checkout_id,owner_id,state,progress,accepted_quote_id,demo_instrument,input_digest) VALUES(%s,%s,%s,'ADMISSION_PENDING',%s::jsonb,%s,%s,%s)",(attempt,checkout_id,actor['id'],json.dumps(payload),accepted_quote_id,demo_instrument,digest));resources=_coord_refs(payload)|{attempt,checkout_id,accepted_quote_id,checkout['cart_id']}
                    for kind in ('create_authorization','capture','cancel'):resources.add(self.app.payments.effect_id(attempt,kind))
                    self.app.admit_obligation(tx,attempt,'purchase',actor['id'],checkout_id,resources,PF_WORK_OPS);tx.stage_audit(step_id(attempt,'admission-audit'),{'workflow_id':attempt,'owner_id':actor['id'],'checkout_id':checkout_id,'quote_digest':quote['digest']})
                tx.execute("INSERT INTO application.client_commands VALUES(%s,%s,'purchase.submit',%s,%s)",(actor['id'],command_id,digest,attempt))
        return self.get_status(attempt,actor=actor)

class RecoveryCoordinator(CoordinatorBase):
    DELIVERY_OWNERS=('application','authority','demo_identity','catalog','pricing','inventory','cart','orders','tax','shipping','returns','notifications','events','integrations','payments','checkout')
    COVERAGE=('application','purchases','payments','refunds','cancellations','fulfillment','returns','tax','notifications','events','audit')

    def _control_id(self):
        return step_id(self.app.environment_id+':'+self.app.workload_principal_id,'worker-control-v2:'+_coord_digest(self._control_scope()))

    def _control_scope(self):
        operations={'application.recovery.read','application.step'}
        operations|={owner+'.'+op for owner in self.DELIVERY_OWNERS for op in ('pending','safety_snapshot','claim_publications','claim_audits')}
        return sorted(operations)

    def _delivery_admit(self,owner,kind,claim,controller):
        if owner not in self.DELIVERY_OWNERS or kind not in ('publication','audit'):raise ValueError('unknown_source_family')
        fact={k:claim[k] for k in ('owner','event_id','payload')};fact['topic']=claim.get('topic') if kind=='publication' else None
        if fact['owner']!=owner:raise ValueError('source_owner_mismatch')
        digest=_coord_digest(fact);id=step_id(owner+':'+claim['event_id']+':'+str(fact['topic']),'native-'+kind)
        data={'owner':owner,'kind':kind,'event_id':claim['event_id'],'topic':fact['topic'],'payload_digest':_coord_digest(claim['payload']),'claim':claim}
        operations={'application.step','application.operation','events.archive_publication','events.record_audit',owner+'.inspect_publication',owner+'.ack_publication',owner+'.defer_publication',owner+'.inspect_audit',owner+'.ack_audit',owner+'.defer_audit','notifications.receive_event','notifications.dispatch_claim','notifications.dispatch_unknown','notifications.dispatch_settle','notifications.get_delivery_status','orders.get_order','orders.get_receipt','integrations.invoke','integrations.inspect'}
        resources=_coord_refs(claim['payload'])|{id,owner,claim['event_id']}
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,controller,'application.step',self._control_id())) as tx:
            prior=tx.execute('SELECT * FROM application.flow_workflows WHERE id=%s FOR UPDATE',(id,)).fetchone()
            if prior:
                if prior['input_digest']!=digest:raise RuntimeError('immutable_source_fact_changed')
                tx.execute('UPDATE application.flow_workflows SET input=%s::jsonb WHERE id=%s',(json.dumps(safe(data)),id))
            else:
                tx.execute("INSERT INTO application.flow_workflows(id,kind,owner_id,resource_id,input,input_digest) VALUES(%s,'delivery',%s,%s,%s::jsonb,%s)",(id,self.app.workload_principal_id,claim['event_id'],json.dumps(safe(data)),digest))
                self.app.admit_obligation(tx,id,'delivery',self.app.workload_principal_id,claim['event_id'],resources,operations)
        return id

    def _discover_delivery(self,controller,limit):
        ids=[];failures=[];pairs=[(owner,kind) for owner in self.DELIVERY_OWNERS for kind in ('publication','audit')]
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,controller,'application.step',self._control_id())) as tx:
            position=tx.execute('SELECT version FROM application.obligations WHERE id=%s',(self._control_id(),)).fetchone()['version']%len(pairs)
        deadline=time.monotonic()+2
        for offset in range(len(pairs)):
            if offset and time.monotonic()>=deadline:break
            owner,kind=pairs[(position+offset)%len(pairs)]
            self._renew_worker(CURRENT_WORKER_LEASE.get(),controller);port=self.app.owner_delivery(owner,controller)
            try:
                claimfn=port.claim_publications if kind=='publication' else port.claim_audits
                # A small source batch is consumed immediately; expiry still denies every stale acknowledgement.
                for claim in claimfn(batch=min(limit,5),lease=30):
                    self._renew_worker(CURRENT_WORKER_LEASE.get(),controller)
                    id=self._delivery_admit(owner,kind,claim,controller);ids.append(id);self.run_delivery(id)
            except Exception as exc:failures.append({'owner':owner,'kind':kind,'reason':_coord_reason(exc)})
            with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,controller,'application.step',self._control_id())) as tx:
                tx.execute('UPDATE application.obligations SET version=version+1 WHERE id=%s',(self._control_id(),))
        return ids,failures

    def _renew_worker(self,token,actor):
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',self._control_id())) as tx:
            row=tx.execute("UPDATE application.worker_lease SET expires_at=clock_timestamp()+interval '5 seconds' WHERE singleton=1 AND token=%s AND process_id=%s AND expires_at>clock_timestamp() RETURNING token",(token,str(os.getpid()))).fetchone()
            if not row:raise PermissionError('original_global_worker_lease_expired')

    def list_items(self,*,actor):
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.recovery.read',actor['id'])) as tx:return [dict(r) for r in tx.execute("SELECT id,kind,state,resource_id,eligible_at,last_reason,attempts,version FROM application.obligations WHERE kind<>'worker_control' AND state<>'COMPLETED' ORDER BY eligible_at,id LIMIT 100").fetchall()]

    def provision_control(self):
        if not self.app._maintenance or self.app.profile not in ('demo-test','portfolio-demo'):raise PermissionError('explicit_verified_maintenance_required')
        id=self._control_id();self.app.workload_actor(id)
        with self.app.db.transaction('application') as tx:
            instance=tx.execute('SELECT * FROM application.instance WHERE singleton=1 FOR SHARE').fetchone()
            if not instance or (instance['environment_id'],instance['merchant_id'],instance['profile'])!=(self.app.environment_id,self.app.merchant_id,self.app.profile):raise PermissionError('instance_identity_mismatch')
            self.app.admit_obligation(tx,id,'worker_control',self.app.workload_principal_id,id,[id,*self.DELIVERY_OWNERS],self._control_scope())
            tx.stage_audit(step_id(id,'private-controller-provision'),{'operation':'private_controller_provision','environment_id':self.app.environment_id,'controller_id':id,'scope_digest':_coord_digest(self._control_scope())})
        return {'id':id,'state':'PROVISIONED','profile':self.app.profile}

    def run(self):return self.run_once()

    def run_delivery(self,id):
        token=self._claim(id)
        if not token:return {'id':id,'state':'BUSY'}
        row=self._progress(id,'flow');data=row['input'];owner=data['owner'];kind=data['kind'];event_id=data['event_id'];claim=data['claim'];topic=data.get('topic')
        try:
            probe_actor=self.app.workload_actor(id,lease_token=token)
            source=self.app.owner_delivery(owner,probe_actor)
            native=source.inspect_publication(event_id,topic=topic) if kind=='publication' else source.inspect_audit(event_id)
            recorded=native['state']==('DELIVERED' if kind=='publication' else 'RECORDED')
            if not recorded and (native.get('lease_id')!=claim['fence'] or native.get('lease_active') is not True):
                self._defer(id,token,'awaiting_current_native_source_claim');return {'id':id,'state':'PENDING'}
            if kind=='publication':
                archived=self._step(id,token,'archive','events','archive_publication',event_id,lambda a:self.app.events.archive_publication(owner,event_id,topic,actor=a),kind='flow')
                payload=claim['payload']
                if isinstance(payload,dict) and payload.get('producer')=='orders' and payload.get('payload',{}).get('operation')=='confirm_payment':
                    intent=self._step(id,token,'notification','notifications','receive_event',payload['aggregate_id'],lambda a:self.app.notifications.receive_event(payload,actor=a),kind='flow')
                    key=str(uuid.uuid5(uuid.UUID(intent['id']),'synthetic-delivery'));self._extend(id,token,{intent['id'],key})
                    self._step(id,token,'notification_delivery','notifications','dispatch_claim',intent['id'],lambda a:self.app.notifications.dispatch(intent['id'],actor=a),kind='flow',extra_permits=(('integrations','invoke',key),),accept=lambda r:r['state'] in ('simulated_delivered','suppressed','failed'))
                self._step(id,token,'source_ack',owner,'inspect_publication' if recorded else 'ack_publication',event_id,lambda a:({'owner':owner,'event_id':event_id,'topic':topic,'state':'DELIVERED'} if recorded else self.app.owner_delivery(owner,a).ack_publication(event_id,claim['fence'],topic=topic)),kind='flow')
            else:
                receipt=self._step(id,token,'archive','events','record_audit',event_id,lambda a:self.app.events.record_audit(owner,event_id,actor=a),kind='flow')
                self._step(id,token,'source_ack',owner,'inspect_audit' if recorded else 'ack_audit',event_id,lambda a:(native['receipt'] if recorded else self.app.owner_delivery(owner,a).ack_audit(event_id,claim['fence'],receipt)),kind='flow')
            self._state(id,token,'COMPLETE','flow',done=True);return {'id':id,'state':'COMPLETE'}
        except _PendingStep:self._defer(id,token,'delivery_pending');return {'id':id,'state':'PENDING'}
        except Exception as exc:self._defer(id,token,_coord_reason(exc),'REVIEW');return {'id':id,'state':'REVIEW'}

    def run_once(self,limit=100,*,_worker_token=None):
        limit=min(max(int(limit),1),100);control_id=self._control_id();actor=self.app.workload_actor(control_id)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.recovery.read',control_id)) as tx:
            row=tx.execute('SELECT * FROM application.worker_lease WHERE singleton=1 FOR UPDATE').fetchone();now=tx.execute('SELECT clock_timestamp() now').fetchone()['now'];process=str(os.getpid())
            if _worker_token is not None and (not row or row['token']!=_worker_token or row['process_id']!=process or row['expires_at']<=now):raise PermissionError('original_global_worker_lease_expired')
            if row and row['expires_at']>now and row['process_id']!=process:return {'processed':0,'deferred':0,'held':1,'state':'ANOTHER_WORKER','delivery_discovered':0,'failures':[]}
            token=row['token'] if row and row['process_id']==process and row['expires_at']>now else str(uuid.uuid4());tx.execute("INSERT INTO application.worker_lease VALUES(1,%s,%s,clock_timestamp()+interval '5 seconds') ON CONFLICT(singleton) DO UPDATE SET token=excluded.token,process_id=excluded.process_id,expires_at=excluded.expires_at",(token,process))
        binding=CURRENT_WORKER_LEASE.set(token)
        try:
            actor=self.app.workload_actor(control_id)
            self._renew_worker(token,actor)
            with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.recovery.read',control_id)) as tx:
                rows=tx.execute("SELECT id,kind FROM application.obligations WHERE kind NOT IN ('worker_control','delivery') AND state IN ('ADMITTED','PENDING','UNKNOWN','REVIEW') AND eligible_at<=clock_timestamp() AND (lease_until IS NULL OR lease_until<=clock_timestamp()) ORDER BY eligible_at,id LIMIT %s",(limit,)).fetchall()
            counts={'processed':0,'deferred':0,'held':0,'state':'TURN_COMPLETED','delivery_discovered':0,'failures':[]}
            for row in rows:
                self._renew_worker(token,actor)
                handler=self.app.purchase if row['kind']=='purchase' else self.app.refunds if row['kind']=='refund' else self.app.fulfillment if row['kind'] in ('fulfillment','cancellation') else self.app.return_coordinator if row['kind']=='return' else None
                if row['kind']=='delivery':outcome=self.run_delivery(row['id'])
                elif handler is None:counts['held']+=1;continue
                else:outcome=handler.run(row['id'])
                state=outcome.get('state');counts['deferred' if state in ('PENDING','BUSY','UNKNOWN') else 'held' if state=='REVIEW' else 'processed']+=1
            discovered,failures=self._discover_delivery(actor,limit);counts['delivery_discovered']=len(discovered);counts['held']+=len(failures);counts['failures']=failures
            # An ACK may have committed at its producer before the APP step receipt was saved.
            # Reconcile a few such archived children independently; an expired source fence waits re-claim.
            with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.recovery.read',control_id)) as tx:
                resumable=tx.execute("SELECT o.id FROM application.obligations o JOIN application.flow_workflows f ON f.id=o.id WHERE o.kind='delivery' AND o.state<>'COMPLETED' AND f.progress ? 'archive' AND o.eligible_at<=clock_timestamp() AND (o.lease_until IS NULL OR o.lease_until<=clock_timestamp()) ORDER BY o.eligible_at,o.id LIMIT 5").fetchall()
            for row in resumable:
                self._renew_worker(token,actor);outcome=self.run_delivery(row['id']);counts['processed' if outcome['state']=='COMPLETE' else 'deferred']+=1
            return counts
        finally:CURRENT_WORKER_LEASE.reset(binding)

    def run_recovery_scan(self,*,worker_token,limit=100):
        return self.run_once(limit,_worker_token=worker_token)

    # NAV RecoveryCoordinator.start_restore_reconciliation :: private staged replacement hold
    def start_restore_reconciliation(self,*,guard):
        if not self.app._maintenance or self.app.profile!='demo-test':raise PermissionError('private_verified_staging_maintenance_required')
        proof=self.app.db.attest_disposable(guard)
        if proof.get('state')!='DISPOSABLE_VERIFIED' or proof.get('profile')!='demo-test' or proof.get('environment_id')!=self.app.environment_id:raise PermissionError('independent_replacement_identity_required')
        seed_revision='pf-demo-six-20261028';id=step_id(self.app.environment_id,'staged-replacement-hold:'+proof['run_id']);digest=_coord_digest([self.app.environment_id,self.app.merchant_id,self.app.profile,self.app.release_id,proof['proof_sha256'],seed_revision])
        def admit(tx):
            instance=tx.execute('SELECT * FROM application.instance WHERE singleton=1 FOR SHARE').fetchone();seed=tx.execute('SELECT * FROM application.seed_receipts WHERE revision=%s FOR SHARE',(seed_revision,)).fetchone()
            if not instance or (instance['environment_id'],instance['merchant_id'],instance['profile'],instance['release_id'])!=(self.app.environment_id,self.app.merchant_id,self.app.profile,self.app.release_id):raise PermissionError('replacement_instance_identity_mismatch')
            if not seed or seed['state']!='COMPLETE' or len(seed['steps'])!=36 or any(step.get('state')!='DONE' for step in seed['steps'].values()):raise PermissionError('actual_native_seed_completion_required')
            release=tx.execute('SELECT * FROM application.release_state WHERE id=1 FOR UPDATE').fetchone()
            if not release:raise PermissionError('actual_release_state_required')
            prior=tx.command_record(id)
            if prior is not None and prior['state']=='COMPLETED' and release['state']!='RECOVERY_HOLD':raise PermissionError('held_receipt_state_mismatch')
            return True
        with self.app.db.transaction('application',command_id=id,request_digest=digest,authority_guard=admit) as tx:
            prior=tx.command_lookup()
            if prior is not None:return prior
            tx.execute("UPDATE application.release_state SET state='RECOVERY_HOLD' WHERE id=1")
            result={'id':id,'receipt_id':id,'state':'RECOVERY_HOLD','profile':self.app.profile,'environment_id':self.app.environment_id,'seed_revision':seed_revision,'audit_stage':'SOURCE_ACCEPTED','scope':'PRIVATE_STAGED_REPLACEMENT_ONLY','reconciliation':'NOT_COMPLETED','session_reset':'NOT_PERFORMED','reopen':'NOT_PERFORMED','swap':'NOT_PERFORMED'}
            tx.stage_audit(id,{'operation':'private_staged_replacement_hold','environment_id':self.app.environment_id,'fixture_run_id':proof['run_id'],'seed_revision':seed_revision,'state':'RECOVERY_HOLD','scope':'staging_only','reopen':'NOT_PERFORMED','swap':'NOT_PERFORMED'});tx.command_complete(result)
            return result

    def unresolved_count(self):
        snapshot=self.unresolved_snapshot()
        if not snapshot['complete']:raise RuntimeError('incomplete_owner_safety_coverage')
        return snapshot['unresolved']

    def unresolved_snapshot(self):
        id=self._control_id();actor=self.app.workload_actor(id);failures=[];counts={};details={}
        try:
            with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.recovery.read',id)) as tx:
                rows=tx.execute("SELECT kind,count(*) n FROM application.obligations WHERE kind<>'worker_control' AND state<>'COMPLETED' GROUP BY kind").fetchall();counts['application']=sum(r['n'] for r in rows);details['application']={r['kind']:r['n'] for r in rows}
        except Exception as exc:failures.append({'owner':'application','reason':_coord_reason(exc)})
        services={service.owner:service for service in self.app.owners+[self.app.payments,self.app.checkout]}
        for owner in self.DELIVERY_OWNERS:
            try:
                snapshot=self.app.owner_delivery(owner,actor).safety_snapshot();counts[owner+'.delivery']=snapshot['unresolved'];details[owner+'.delivery']=snapshot
                if snapshot.get('complete')is not True:failures.append({'owner':owner,'reason':'metadata_coverage_incomplete'})
            except Exception as exc:failures.append({'owner':owner,'reason':_coord_reason(exc)})
            if owner in services:
                try:
                    snapshot=services[owner].safety_snapshot(actor=actor);counts[owner+'.native']=snapshot['unresolved'];details[owner+'.native']=snapshot
                    if snapshot.get('complete')is not True:failures.append({'owner':owner,'reason':'native_coverage_incomplete'})
                except Exception as exc:failures.append({'owner':owner,'reason':_coord_reason(exc)})
        return {'complete':not failures,'coverage':list(self.COVERAGE),'unresolved':int(sum(counts.values())),'failures':failures,'counts':counts,'details':details,'profile':self.app.profile,'audit_guarantee':'DEMO_RECORDED; original replicated audit qualification remains blocked'}

class RefundCoordinator(CoordinatorBase):

    def admit_approved_plan(self,plan,parent_id,actor,return_id=None):
        actual=self.app.orders.get_refund_plan(plan['id'],actor=actor);id=step_id(actual['id'],'refund-workflow');data={'plan_id':actual['id'],'order_id':actual['order_id'],'return_id':return_id};digest=_coord_digest(data)
        with self.app.db.transaction('application',authority_guard=lambda tx:self.app.authorize(tx,actor,'application.step',parent_id)) as tx:
            parent=tx.execute('SELECT lease_token,lease_until FROM application.obligations WHERE id=%s FOR SHARE',(parent_id,)).fetchone()
            if not parent or parent['lease_token']!=actor.get('lease_token') or parent['lease_until'] is None or parent['lease_until']<=tx.execute('SELECT clock_timestamp() now').fetchone()['now']:raise RuntimeError('stale_worker')
            prior=tx.execute('SELECT * FROM application.flow_workflows WHERE id=%s',(id,)).fetchone()
            if prior:
                if prior['input_digest']!=digest:raise ValueError('idempotency_conflict')
            else:
                tx.execute("INSERT INTO application.flow_workflows(id,kind,owner_id,resource_id,input,input_digest) VALUES(%s,'refund',%s,%s,%s::jsonb,%s)",(id,actual['owner_id'],actual['order_id'],json.dumps(data),digest));self.app.admit_obligation(tx,id,'refund',actual['owner_id'],actual['order_id'],_coord_refs(actual)|{id,parent_id,actual['order_id']},PF_WORK_OPS);tx.stage_audit(step_id(id,'admission'),{'parent_obligation':parent_id,'plan_id':actual['id'],'workflow_id':id})
        return {'id':id,'state':'ADMITTED','version':1}

    def get_status(self,id,*,actor):
        row=self._get(id,actor);return {'id':id,'operation_id':id,'version':row['version'],'state':row['state'],'plan_id':row['input']['plan_id'],'order_id':row['input']['order_id'],'refund':row['progress'].get('refund'),'status_url':'/api/v1/operations/'+id,'effect_certainty':'committed' if row['state']=='SUCCEEDED' else 'unknown'}
    def request_plan(self,return_id,command_id,*,actor):
        proposal=self.app.returns.get_refund_plan(return_id,actor=actor)
        if not proposal.get('order_plan_id'):raise ValueError('approved_refund_plan_required')
        plan=self.app.orders.get_refund_plan(proposal['order_plan_id'],actor=actor)
        id=self._flow_admit('refund','refund.request',plan['order_id'],{'plan_id':plan['id'],'order_id':plan['order_id'],'return_id':return_id},command_id,actor,plan['owner_id'],'application.refund.submit')
        self.run(id);return self.get_status(id,actor=actor)

    def run(self,id):
        token=self._claim(id)
        if token is None:return {'id':id,'state':'BUSY'}
        try:
            row=self._progress(id,'flow');data=row['input'];ctx=self.app.workload_actor(id,lease_token=token);plan=self.app.orders.get_refund_plan(data['plan_id'],actor=ctx)
            if plan['amount_minor']==0:
                settled=self._step(id,token,'refund','orders','settle_zero_plan',plan['id'],lambda a:self.app.orders.settle_zero_plan(plan['id'],actor=a,command_id=step_id(id,'zero')),kind='flow')
            else:
                purchase=_coord_purchase_for_order(self.app,plan['order_id'],ctx,id);attempt=purchase['id'];effect=self.app.payments.effect_id(attempt,'refund',id);self._extend(id,token,{attempt,effect}|_coord_refs(purchase['progress'].get('capture',{})))
                settled=self._step(id,token,'refund','payments','refund',attempt,lambda a:self.app.payments.refund(attempt,id,plan,plan['reason'],actor=a),kind='flow',accept=lambda r:str(r.get('state')).upper() in ('SUCCEEDED','ZERO_REFUND'),extra_permits=[('integrations','invoke',effect)])
                self._step(id,token,'order_refund','orders','record_refund',plan['order_id'],lambda a:self.app.orders.record_refund(plan['order_id'],settled,plan_id=plan['id'],actor=a,command_id=step_id(id,'order_refund')),kind='flow')
            purchase=_coord_purchase_for_order(self.app,plan['order_id'],ctx,id);tax=purchase['progress'].get('tax')
            if tax:
                self._extend(id,token,{tax['id']})
                self._step(id,token,'tax_reversal','tax','reverse_tax',tax['id'],lambda a:self.app.tax.reverse_tax(id,tax['id'],plan['unit_allocations'],shipping_tax_minor=plan['shipping_tax_refund_minor'],actor=a,command_id=step_id(id,'tax')),kind='flow')
            else:raise _PendingStep('original_tax_record')
            if data.get('return_id'):
                self._extend(id,token,{data['return_id']})
                self._step(id,token,'return_close','returns','close_return',data['return_id'],lambda a:self.app.returns.close_return(data['return_id'],{'id':plan['id'],'plan_id':plan['id']},actor=a,command_id=step_id(id,'return_close')),kind='flow')
            self._state(id,token,'SUCCEEDED','flow',done=True);return {'id':id,'state':'SUCCEEDED'}
        except _PendingStep:self._defer(id,token,'refund_or_tax_pending');return {'id':id,'state':'PENDING'}
        except Exception as exc:self._defer(id,token,_coord_reason(exc),'UNKNOWN' if isinstance(exc,TimeoutError) else 'PENDING');return {'id':id,'state':'PENDING'}

class ReturnCoordinator(CoordinatorBase):
    def command(self,operation,data,revision,command_id,*,actor):
        if operation=='request_return':
            order=self.app.orders.get_order(data['order_id'],actor=actor);_coord_lines(order,data['unit_ids']);payload={'operation':operation,**data};resource=order['id'];owner=order['owner_id'];permission='application.return.submit'
        else:
            returned=self.app.returns.get_return(data['return_id'],actor=actor);require_revision(revision)
            if returned['version']!=revision:raise ValueError('version_conflict')
            payload={'operation':operation,**data,'expected_revision':revision};resource=returned['id'];owner=returned['owner_id'];permission='application.return.manage'
        id=self._flow_admit('return','return.'+operation,resource,payload,command_id,actor,owner,permission);self.run(id);return self.get_status(id,actor=actor)

    def get_status(self,id,*,actor):
        row=self._get(id,actor);result=row['progress'].get('result') or row['progress'].get('request') or row['progress'].get('inspection');return {'id':id,'operation_id':id,'version':row['version'],'state':row['state'],'return_id':(result or {}).get('return_id',(result or {}).get('id',row['input'].get('return_id'))),'result':result,'replacement_checkout_id':row['progress'].get('replacement_checkout',{}).get('id'),'status_url':'/api/v1/operations/'+id}

    def run(self,id):
        token=self._claim(id)
        if token is None:return {'id':id,'state':'BUSY'}
        try:
            row=self._progress(id,'flow');data=row['input'];op=data['operation'];ctx=self.app.workload_actor(id,lease_token=token)
            if op=='request_return':
                order=self.app.orders.get_order(data['order_id'],actor=ctx);lines=_coord_lines(order,data['unit_ids']);self._step(id,token,'request','returns','request_return',order['id'],lambda a:self.app.returns.request_return(order['id'],lines,data['reason'],data['requested_resolution'],actor=a,command_id=step_id(id,'request')),kind='flow')
            else:
                returned=self.app.returns.get_return(data['return_id'],actor=ctx);self._extend(id,token,_coord_refs(returned));order=self.app.orders.get_order(returned['order_id'],actor=ctx)
                if op=='approve_return':self._step(id,token,'result','returns','approve_return',returned['id'],lambda a:self.app.returns.approve_return(returned['id'],data['decision'],data['reason'],data['expected_revision'],merchant_fault=data['reason'] in ('damaged','wrong_item','merchant_error','merchant_cancel'),actor=a,command_id=step_id(id,'approve')),kind='flow')
                elif op=='record_received':self._step(id,token,'result','returns','record_received',returned['id'],lambda a:self.app.returns.record_received(returned['id'],data['unit_ids'],data['expected_revision'],actor=a,command_id=step_id(id,'received')),kind='flow')
                elif op=='record_inspection':
                    inspection=self._step(id,token,'inspection','returns','record_inspection',returned['id'],lambda a:self.app.returns.record_inspection(returned['id'],data['dispositions'],data['expected_revision'],actor=a,command_id=step_id(id,'inspection')),kind='flow')
                    if inspection['state']=='rejected_disposition':self._step(id,token,'result','returns','close_return',returned['id'],lambda a:self.app.returns.close_return(returned['id'],actor=a,command_id=step_id(id,'rejected_close')),kind='flow')
                    elif inspection['state']=='inspected':
                        self._step(id,token,'order_return','orders','record_return',order['id'],lambda a:self.app.orders.record_return(order['id'],inspection,actor=a,command_id=step_id(id,'order_return')),kind='flow')
                        sellable=[u for u in inspection['units'] if u['accepted'] and u['disposition']=='sellable']
                        for variant,quantity in _coord_quantities(sellable).items():self._step(id,token,'restock:'+variant,'inventory','restock_return',returned['id'],lambda a,v=variant,q=quantity:self.app.inventory.restock_return(returned['id'],v,q,inspection,actor=a,command_id=step_id(id,'restock:'+v)),kind='flow')
                        proposal=self._step(id,token,'proposal','returns','build_refund_plan',returned['id'],lambda a:self.app.returns.build_refund_plan(returned['id'],actor=a,command_id=step_id(id,'proposal')),kind='flow')
                        plan=self._step(id,token,'approved_plan','orders','plan_units',order['id'],lambda a:self.app.orders.plan_units(order['id'],proposal['unit_ids'],'return',returned['snapshot']['reason'],proposal,merchant_fault=proposal['merchant_fault'],actor=a,command_id=step_id(id,'approved_plan')),kind='flow')
                        self._step(id,token,'result','returns','bind_refund_plan',returned['id'],lambda a:self.app.returns.bind_refund_plan(returned['id'],plan,actor=a,command_id=step_id(id,'bind_plan')),kind='flow')
                elif op=='create_exchange_link':
                    cart=self._step(id,token,'replacement_cart','cart','create_replacement_cart',returned['id'],lambda a:self.app.cart.create_replacement_cart({'id':returned['id']},data['replacement_variant_id'],actor=a,command_id=step_id(id,'replacement_cart')),kind='flow')
                    checkout=self._step(id,token,'replacement_checkout','checkout','begin',cart['id'],lambda a:self.app.checkout.begin(cart['id'],cart['version'],actor=a,command_id=step_id(id,'replacement_checkout')),kind='flow')
                    self._step(id,token,'result','returns','create_exchange_link',returned['id'],lambda a:self.app.returns.create_exchange_link(returned['id'],checkout['id'],actor=a,command_id=step_id(id,'exchange_link')),kind='flow')
                elif op=='close_return':
                    plan=returned.get('refund_plan',{}).get('order_plan_id');self._step(id,token,'result','returns','close_return',returned['id'],lambda a:self.app.returns.close_return(returned['id'],{'id':plan,'plan_id':plan} if plan else None,actor=a,command_id=step_id(id,'close')),kind='flow')
                else:raise ValueError('unsupported_return_operation')
            self._state(id,token,'COMPLETE','flow',done=True);return {'id':id,'state':'COMPLETE'}
        except _PendingStep:self._defer(id,token,'return_pending');return {'id':id,'state':'PENDING'}
        except Exception as exc:self._defer(id,token,_coord_reason(exc),'REVIEW');return {'id':id,'state':'REVIEW'}

def _coord_digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _coord_lines(order,unit_ids):
    if not isinstance(unit_ids,list) or not unit_ids or len(unit_ids)!=len(set(unit_ids)):raise ValueError('invalid_units')
    all_units={u['unit_id']:(line,u) for line in order['lines'] for u in line['units']}
    if not set(unit_ids)<=set(all_units):raise ValueError('foreign_units')
    grouped={}
    for unit in unit_ids:
        line,_=all_units[unit];item=grouped.setdefault(line['variant_id'],{**line,'quantity':0,'unit_ids':[]});item['quantity']+=1;item['unit_ids'].append(unit)
    return list(grouped.values())

def _coord_purchase_for_order(app,order_id,actor,obligation_id):
    with app.db.transaction('application',authority_guard=lambda tx:app.authorize(tx,actor,'application.step',obligation_id)) as tx:
        row=tx.execute("SELECT * FROM application.purchase_workflows WHERE progress->'order'->>'id'=%s",(order_id,)).fetchone()
        if not row:raise ValueError('original_purchase_required')
        return dict(row)

def _coord_quantities(units):
    result={}
    for unit in units:result[unit['variant_id']]=result.get(unit['variant_id'],0)+1
    return result

def _coord_reason(exc):return str(getattr(exc,'code',None) or str(exc).split(':')[0] or type(exc).__name__)[:100]

def _coord_refs(value):
    result=set()
    if isinstance(value,dict):
        for key,item in value.items():
            if key=='id' or key.endswith(('_id','_key')):
                if isinstance(item,str):result.add(item)
            if key.endswith('_ids') and isinstance(item,list):result.update(x for x in item if isinstance(x,str))
            if isinstance(item,(dict,list)):result|=_coord_refs(item)
    elif isinstance(value,list):
        for item in value:result|=_coord_refs(item)
    return result
