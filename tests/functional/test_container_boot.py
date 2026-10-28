"""Controlled container lifecycle regressions; no real process or DB startup."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import pytest

# Normal repository test import; staging verification may override the path.
SOURCE=Path(os.environ.get('SME_CONTAINER_TEST_SOURCE',Path(__file__).resolve().parents[2]/'app/controllers/container_boot.py'))
spec=importlib.util.spec_from_file_location('container_boot_tests',SOURCE)
boot=importlib.util.module_from_spec(spec);sys.modules[spec.name]=boot;spec.loader.exec_module(boot)


def test_state_private_owner_mode_is_checked_without_repair(tmp_path):
    tmp_path.chmod(0o700);boot.assert_state_directory(tmp_path)
    tmp_path.chmod(0o755)
    with pytest.raises(boot.StartupFailure,match='state_volume_owner_or_mode_mismatch'):boot.assert_state_directory(tmp_path)
    assert tmp_path.stat().st_mode&0o777==0o755


def test_state_reference_cannot_escape_or_follow_symlink(tmp_path):
    tmp_path.chmod(0o700)
    with pytest.raises(boot.StartupFailure,match='state_binding_outside'):boot.inside_state('/outside/secret',tmp_path)
    link=tmp_path/'link';link.symlink_to('/outside')
    with pytest.raises(boot.StartupFailure,match='state_symlink_denied'):boot.inside_state(link/'secret',tmp_path)


def test_flock_loser_same_namespace_pid_cannot_remove_winner_ready_marker(tmp_path):
    tmp_path.chmod(0o700);winner=boot.ContainerRuntime(tmp_path);loser=boot.ContainerRuntime(tmp_path)
    winner.acquire();marker={'state':'READY','pid':os.getpid(),'boot_nonce':winner.boot_nonce};boot.atomic_json(tmp_path/'container-ready.json',marker)
    try:
        with pytest.raises(boot.StartupFailure,match='another_instance_owns_state_volume'):loser.acquire()
        loser.shutdown()
        assert boot.private_json(tmp_path/'container-ready.json')==marker
    finally:winner.shutdown()
    assert not (tmp_path/'container-ready.json').exists()


def test_ready_cleanup_requires_owned_boot_nonce(tmp_path):
    tmp_path.chmod(0o700);runtime=boot.ContainerRuntime(tmp_path);runtime.acquire()
    marker={'state':'READY','pid':os.getpid(),'boot_nonce':'f'*32};boot.atomic_json(tmp_path/'container-ready.json',marker)
    runtime.shutdown()
    assert boot.private_json(tmp_path/'container-ready.json')==marker


def test_hung_application_preserves_reserved_postgres_fast_shutdown_budget(tmp_path,monkeypatch):
    events=[];clock=[0.0]
    monkeypatch.setattr(boot.time,'monotonic',lambda:clock[0])
    class Child:
        def __init__(self,name,hung=False):self.name,self.hung,self.exited=name,hung,False
        def poll(self):return 0 if self.exited else None
        def send_signal(self,value):events.append(('signal',self.name,value))
        def wait(self,timeout):
            events.append(('wait',self.name,timeout))
            if self.hung:
                clock[0]+=timeout
                raise subprocess.TimeoutExpired(self.name,timeout)
            self.exited=True;return 0
        def kill(self):events.append(('kill',self.name));self.hung=False;self.exited=True
    runtime=boot.ContainerRuntime(tmp_path);runtime.children=[('postgres',Child('postgres')),('worker',Child('worker')),('caddy',Child('caddy',True))]
    runtime.shutdown()
    worker_signal=events.index(('signal','worker',signal.SIGTERM));caddy_signal=events.index(('signal','caddy',signal.SIGTERM))
    first_wait=next(i for i,event in enumerate(events) if event[0]=='wait')
    assert worker_signal<first_wait and caddy_signal<first_wait
    assert ('signal','postgres',signal.SIGINT) in events and ('wait','postgres',15) in events
    assert ('kill','postgres') not in events and clock[0]<=31


def test_partial_cluster_is_not_reinitialized_or_started(tmp_path,monkeypatch):
    tmp_path.chmod(0o700);(tmp_path/'postgres').mkdir(mode=0o700)
    runtime=boot.ContainerRuntime(tmp_path);called=[]
    monkeypatch.setattr(runtime,'start',lambda *args,**kw:called.append(args))
    with pytest.raises(boot.StartupFailure,match='partial_or_unsupported_cluster_preserved'):runtime.database()
    assert called==[] and not (tmp_path/'profile.json').exists()


def test_dead_child_prevents_readiness_even_if_probe_would_pass(tmp_path):
    class Dead:
        def poll(self):return 1
    runtime=boot.ContainerRuntime(tmp_path);runtime.children=[('worker',Dead())];probes=[]
    with pytest.raises(boot.StartupFailure,match='child_exited_worker'):runtime.wait(lambda:probes.append(True) or True,.1,'app')
    assert probes==[]


def test_wrong_actual_instance_is_rejected_before_maintenance_store_or_ddl(tmp_path,monkeypatch):
    tmp_path.chmod(0o700)
    boot.atomic_json(tmp_path/'profile.json',{'placeholder':True});boot.atomic_json(tmp_path/'proof.json',{'run_id':'run'})
    config={'disposable_proof_file':str(tmp_path/'proof.json'),'environment_id':'environment','merchant_id':'merchant','machine_principal_id':'machine'}
    runtime=boot.ContainerRuntime(tmp_path)
    monkeypatch.setattr(boot,'load_config',lambda *args,**kwargs:config)
    monkeypatch.setattr(boot,'validate_bindings',lambda value,state:value)
    monkeypatch.setattr(boot,'guarded_test_context',lambda *args:None)
    def reject(*args):raise ValueError('database_environment_binding_mismatch')
    monkeypatch.setattr(boot,'validate_runtime_binding',reject)
    monkeypatch.setattr(boot,'Store',lambda *args,**kwargs:pytest.fail('maintenance entered before actual instance check'))
    with pytest.raises(ValueError,match='database_environment_binding_mismatch'):runtime.application()


def test_unexpected_failure_diagnostics_do_not_serialize_exception_secrets(monkeypatch,capsys,tmp_path):
    runtime=boot.ContainerRuntime(tmp_path)
    def fail():raise RuntimeError('private-secret-123')
    monkeypatch.setattr(runtime,'run',fail)
    monkeypatch.setattr(boot,'ContainerRuntime',lambda:runtime)
    assert boot.main([])==1
    raw=capsys.readouterr().out;result=json.loads(raw)
    assert 'private-secret-123' not in raw and result['code']=='startup_failed' and result['error_type']=='RuntimeError'
    assert set(result['tracepoint'])=={'file','function','line'}
