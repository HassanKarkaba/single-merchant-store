"""Application process startup and shutdown; every child is owned by this runner."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
from app.config import ROOT
from app.controllers.http import RendererClient


def supervise(config_file, config, *, node='node', caddy='caddy', port=8080):
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('private_application_port_required')
    required = ['dist/client/browser.js', 'dist/client/browser.css', 'dist/server/browser.mjs']
    if any(not (ROOT / name).is_file() for name in required):
        raise ValueError('production_build_required_run_npm_run_build')
    socket = Path(config['renderer_socket'])
    if not socket.parent.is_dir() or socket.parent.is_symlink() or socket.parent.stat().st_mode & 0o077:
        raise ValueError('private_renderer_socket_directory_required')
    if socket.exists():
        raise ValueError('renderer_socket_already_exists_stop_existing_instance')
    state = Path(config_file).resolve().parent
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', SME_ORIGIN=config['origin'], SME_HOST=urlsplit(config['origin']).netloc,
                       SME_APP_PORT=str(port), XDG_DATA_HOME=str(state/'caddy-data'), XDG_CONFIG_HOME=str(state/'caddy-config'))
    children = []
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    original = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    def start(command):
        process = subprocess.Popen(command, cwd=ROOT, env=environment)
        children.append(process)
        return process
    def healthy(process):
        if process.poll() is not None:
            raise RuntimeError('application_child_exited')
    try:
        renderer = start([node, str(ROOT/'app/controllers/renderer.mjs'), '--listen', str(socket), str(ROOT/'dist/server/browser.mjs')])
        deadline = time.monotonic()+15
        while not RendererClient(str(socket)).ready():
            healthy(renderer)
            if time.monotonic()>deadline: raise RuntimeError('renderer_start_timeout')
            time.sleep(.1)
        common = [sys.executable, str(ROOT/'manage.py')]
        capture=config.get('mail',{}).get('capture') if config.get('mail',{}).get('mode')=='local_capture' else None
        if capture:
            import socket as network
            capture_process=start([sys.executable,'-m','app.controllers.mail_capture','--config',str(Path(config_file).resolve())])
            deadline=time.monotonic()+10
            while True:
                healthy(capture_process)
                try:
                    for capture_port in (capture['smtp_port'],capture['http_port']):
                        with network.create_connection(('127.0.0.1',capture_port),timeout=.2):pass
                    break
                except OSError:
                    if time.monotonic()>deadline:raise RuntimeError('mail_capture_start_timeout')
                    time.sleep(.1)
        for command in ('worker-demo', 'serve-demo'):
            options=['--port',str(port)] if command=='serve-demo' else []
            start([*common, command, '--config', str(Path(config_file).resolve()), '--profile', config['profile'],*options])
        start([caddy, 'run', '--config', str(ROOT/'config/Caddyfile'), '--adapter', 'caddyfile'])
        local_mail={'url':'http://127.0.0.1:'+str(capture['http_port']),'username':'operator','password_file':capture['password_ref'][5:]} if capture else None
        print(json.dumps({'origin': config['origin'], 'local_mail_capture':local_mail,'private_reviewer_setup': str(state/'reviewer-setup.json'),
                          'local_ca_certificate': str(state/'caddy-data/caddy/pki/authorities/local/root.crt')}), flush=True)
        while not stopping:
            for process in children: healthy(process)
            time.sleep(.25)
    finally:
        for process in reversed(children):
            if process.poll() is None: process.terminate()
        deadline = time.monotonic()+30
        for process in reversed(children):
            try: process.wait(timeout=max(.1, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        for sig, handler in original.items(): signal.signal(sig, handler)
        # Only our original renderer process could own this socket, and it is dead.
        if children and children[0].poll() is not None and socket.is_socket(): socket.unlink()
