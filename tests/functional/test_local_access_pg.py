"""Native private CLI validation; captured credentials never reach test output."""
import errno
import json
import os
import pty
import selectors
import subprocess
import sys
import time

import pytest

from app.config import ROOT, guarded_test_context, load_config, read_secret
from app.controllers.local_access import _setup_records, inspect_local_access

pytestmark = pytest.mark.native


def test_local_access_deliberate_terminal_uses_actual_generated_native_accounts():
    profile = os.environ.get('SME_TEST_CONFIG')
    run = os.environ.get('SME_TEST_RUN_ID')
    if not profile or not run:
        pytest.fail('explicit generated guarded disposable profile required')
    config = load_config(profile, 'demo-test', unit='local-access')
    guarded_test_context(config, run)
    fixture_run, records = _setup_records(profile, config)
    if fixture_run != run:
        pytest.fail('local access fixture identity mismatch')
    before = inspect_local_access(profile, config)
    if before['state'] != 'CURRENT':
        pytest.fail('local access fixture bootstrap is stale')
    command = [sys.executable, str(ROOT / 'manage.py'), 'local-access', '--config', profile]
    master, slave = pty.openpty()
    child = None
    chunks = bytearray()
    try:
        child = subprocess.Popen(command, cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
                                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), close_fds=True)
        os.close(slave)
        slave = None
        selector = selectors.DefaultSelector()
        selector.register(master, selectors.EVENT_READ)
        deadline = time.monotonic()+15
        while time.monotonic() < deadline and len(chunks) <= 32768:
            if not selector.select(timeout=0.2):
                if child.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            chunks.extend(chunk)
        selector.close()
        if child.wait(timeout=5) != 0:
            pytest.fail('local access terminal command failed (output intentionally withheld)')
        text = chunks.decode('utf-8')
        if 'LOCAL DEMO CREDENTIALS' not in text or 'otpauth://totp/' not in text:
            pytest.fail('local access terminal did not show deliberate enrollment guidance')
        for record in records:
            if record['password'] not in text or record['username'] not in text:
                pytest.fail('local access terminal did not match the generated native account')
        if config.get('mail', {}).get('mode') == 'local_capture':
            mail_password = read_secret(config['mail']['password_ref']).strip()
            if mail_password not in text or 'Username: operator' not in text:
                pytest.fail('local access terminal omitted generated local mailbox access')
        # Clear captured output rather than retaining it in an evidence artifact.
        chunks[:] = b'\0'*len(chunks)
        del text
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        if slave is not None:
            os.close(slave)
        os.close(master)
    redirected = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15)
    if redirected.returncode != 1 or redirected.stdout:
        pytest.fail('local access allowed nonterminal credential export')
    try:
        error = json.loads(redirected.stderr)
    except ValueError:
        pytest.fail('local access error was not sanitized')
    if error.get('code') != 'local_access_interactive_terminal_required':
        pytest.fail('local access redirect denial was not explicit')
    for record in records:
        if record['password'] in redirected.stderr:
            pytest.fail('local access error leaked protected material')
    if config.get('mail', {}).get('mode') == 'local_capture' and mail_password in redirected.stderr:
        pytest.fail('local access error leaked local mailbox material')
    after = inspect_local_access(profile, config)
    if [{key:value for key,value in item.items() if key!='checked_at'} for item in before['accounts']] != [{key:value for key,value in item.items() if key!='checked_at'} for item in after['accounts']]:
        pytest.fail('local access changed native identity state')
