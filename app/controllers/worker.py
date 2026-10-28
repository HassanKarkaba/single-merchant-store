"""Supervised worker controller with one current durable authority lease."""
from __future__ import annotations
import json
import os
from app.models.common import CURRENT_WORKER_LEASE
def worker_loop(args,config,app):
    import time,uuid,signal
    token=str(uuid.uuid4());stopping=False;acquired=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            with app.db.transaction('application') as tx:
                if not acquired:
                    row=tx.execute("INSERT INTO application.worker_lease(singleton,token,process_id,expires_at) VALUES(1,%s,%s,clock_timestamp()+interval '5 seconds') ON CONFLICT(singleton) DO UPDATE SET token=excluded.token,process_id=excluded.process_id,expires_at=excluded.expires_at WHERE application.worker_lease.expires_at<=clock_timestamp() RETURNING token",(token,str(os.getpid()))).fetchone()
                    if not row:raise ValueError('worker_owner_already_active')
                    acquired=True
                else:
                    row=tx.execute("UPDATE application.worker_lease SET expires_at=clock_timestamp()+interval '5 seconds' WHERE singleton=1 AND token=%s AND process_id=%s AND expires_at>clock_timestamp() RETURNING token",(token,str(os.getpid()))).fetchone()
                    if not row:raise ValueError('worker_current_lease_lost')
            observer=getattr(args,'_lease_observer',None)
            if observer is not None:observer(token);args._lease_observer=None
            result=app.recovery.run_recovery_scan(worker_token=token,limit=100)
            mail=getattr(app,'account_mail',None)
            if mail is not None and mail.config:
                # Renew only the still-current original lease before bounded SMTP I/O.
                with app.db.transaction('application') as tx:
                    renewed=tx.execute("UPDATE application.worker_lease SET expires_at=clock_timestamp()+interval '5 seconds' WHERE singleton=1 AND token=%s AND process_id=%s AND expires_at>clock_timestamp() RETURNING token",(token,str(os.getpid()))).fetchone()
                    if not renewed:raise ValueError('worker_current_lease_lost')
                result['account_mail']=mail.run_once(worker_token=token,limit=1)
            if args.once:print(json.dumps(result));return result
            time.sleep(0.5)
    finally:
        with app.db.transaction('application') as tx:tx.execute('UPDATE application.worker_lease SET expires_at=clock_timestamp() WHERE singleton=1 AND token=%s',(token,))
        app.close()
