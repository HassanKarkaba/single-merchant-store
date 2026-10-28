"""Test-only owned child process with an actual native worker lease.

Only children created here can be terminated. All database accesses require the
fixture's independently preexisting disposable proof. No PID discovery or reset.
"""
import argparse
import multiprocessing
import os
import queue
import threading
import time

from app.config import resolve_database, guarded_test_context
from app.models.store import Store
from app.controllers.worker import worker_loop


def worker_process(config, run_id, case_id, events, checkpoint_key=None):
    guarded_test_context(config, run_id)
    app=Store(resolve_database(config),profile='demo-test',config=config,maintenance=checkpoint_key is not None)
    app.initialize()
    if checkpoint_key is not None:
        def checkpoint(provider,operation,key,phase):
            if (provider,operation,key,phase)==('stripe','capture',checkpoint_key,'after_provider_commit'):
                events.put({'event':'checkpoint','phase':phase})
                threading.Event().wait(120)
        app.bind_test_checkpoint(checkpoint,run_id=run_id,case_id=case_id)
    options=argparse.Namespace(once=False)
    options._lease_observer=lambda token:events.put({'event':'lease','token':token,'process_id':os.getpid()})
    worker_loop(options,config,app)


class OwnedWorker:
    def __init__(self,config,run_id,case_id,*,checkpoint_key=None):
        guarded_test_context(config,run_id)
        self.control=Store(resolve_database(config),profile='demo-test',config=config,maintenance=True)
        self.control.initialize();self.checkpoint=threading.Event();self._changed=threading.Condition();self.lease=None;self._stopped=False
        with self.control.db.transaction('application') as tx:
            if tx.execute('SELECT token FROM application.worker_lease WHERE singleton=1 AND expires_at>clock_timestamp()').fetchone():raise ValueError('exclusive_owned_worker_process_required')
        context=multiprocessing.get_context('spawn')
        self.events=context.Queue(maxsize=16)
        self.process=context.Process(target=worker_process,args=(config,run_id,case_id,self.events,checkpoint_key))
        self.process.start()
        def receive():
            while not self._stopped:
                try:event=self.events.get(timeout=.2)
                except queue.Empty:continue
                with self._changed:
                    if event['event']=='lease':self.lease=event
                    elif event['event']=='checkpoint':self.checkpoint.set()
                    self._changed.notify_all()
        self.listener=threading.Thread(target=receive,daemon=True);self.listener.start()
        deadline=time.monotonic()+15
        with self._changed:
            while self.lease is None and time.monotonic()<deadline and self.process.is_alive():self._changed.wait(.1)
        if self.lease is None:
            if self.process.is_alive():self.process.kill()
            self.process.join(5);self._stopped=True
            raise ValueError('actual_owned_worker_lease_observation_required')
        if self.lease['process_id']!=self.process.pid:raise ValueError('actual_owned_child_pid_required')

    def stop(self,*,crash=False):
        if self._stopped:return
        if self.process.is_alive():
            if crash:self.process.kill()
            else:self.process.terminate()
        self.process.join(5)
        if self.process.is_alive():self.process.kill();self.process.join(5)
        if self.process.is_alive():raise ValueError('owned_worker_death_not_observed')
        # Current row can be expired only by the exact token/PID born here.
        with self.control.db.transaction('application') as tx:
            tx.execute('UPDATE application.worker_lease SET expires_at=clock_timestamp() WHERE singleton=1 AND token=%s AND process_id=%s',(self.lease['token'],str(self.lease['process_id'])))
        self._stopped=True;self.listener.join(1);self.control.close();self.events.close()
