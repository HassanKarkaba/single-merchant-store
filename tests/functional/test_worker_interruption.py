"""Actual owned-process interruption after an independently committed capture.

This is one supplemental PAC24/PAC23 fault case, not the Linux BM04 workload.
The caller must stop its ordinary test worker first. Only children born here
may be terminated; the production harness checks their actual lease tokens.
"""
import os
import sys
import time
import uuid

import pytest

from test_application_journey import ROOT, command, instance, prepared_checkout
from worker_harness import OwnedWorker

pytestmark = pytest.mark.native


def test_owned_worker_death_after_capture_reconciles_original_effect(instance):
    app, buyer, _ = instance

    run = os.environ['SME_TEST_RUN_ID']
    config = app.config
    deadline = time.monotonic() + 8
    while True:
        with app.db.transaction('application') as tx:
            active = tx.execute('SELECT 1 FROM application.worker_lease WHERE singleton=1 AND expires_at>clock_timestamp()').fetchone()
        if not active:
            break
        if time.monotonic() >= deadline:
            pytest.fail('BLOCKED: ordinary worker must be stopped before this owned-process fault case')
        time.sleep(.1)

    variant = next(v['id'] for p in app.catalog_view()['products'] for v in p['variants'] if v['available'] >= 1 and v['unit_minor'] > 0)
    checkout, quote = prepared_checkout(app, buyer, variant)
    key = str(uuid.uuid4())
    primary = sys.modules[type(app).__module__]
    attempt = primary.step_id(buyer['id'] + ':' + key, 'purchase')
    effect = app.payments.effect_id(attempt, 'capture')
    body = {'checkout_id': checkout['id'], 'accepted_quote_id': quote['id'], 'demo_instrument': 'demo_success'}
    first = OwnedWorker(config, run, 'PAC24-postcapture', checkpoint_key=effect)
    successor = None
    try:
        submitted = command(app, buyer, 'purchases', 'submit', body, key=key)
        assert submitted['id'] == attempt
        assert first.checkpoint.wait(30), 'Actual after_provider_commit checkpoint was not acknowledged'
        with app.db.transaction('application') as tx:
            captured = tx.execute("SELECT count(*) n,coalesce(sum(amount_minor),0) amount FROM integrations.money_ledger WHERE kind='capture' AND object_id=(SELECT provider_id FROM payments.attempts WHERE id=%s)", (attempt,)).fetchone()
            before = tx.execute('SELECT state FROM application.purchase_workflows WHERE id=%s', (attempt,)).fetchone()
        assert captured['n'] == 1 and captured['amount'] == quote['total_minor']
        assert before['state'] != 'PAID'
        original_pid = first.process.pid
        first.stop(crash=True)
        assert not first.process.is_alive()
        successor = OwnedWorker(config, run, 'PAC24-postcapture-restart')
        assert successor.process.pid != original_pid
        deadline = time.monotonic() + 45
        while True:
            status = app.purchase.get_status(attempt, actor=buyer)
            if status['state'] == 'PAID' and not status['pending_followups']:
                break
            if time.monotonic() >= deadline:
                pytest.fail('Original interrupted purchase did not converge; current state=' + status['state'])
            time.sleep(.1)
        assert command(app, buyer, 'purchases', 'submit', body, key=key)['id'] == attempt
        with app.db.transaction('application') as tx:
            captured = tx.execute("SELECT count(*) n,coalesce(sum(amount_minor),0) amount FROM integrations.money_ledger WHERE kind='capture' AND object_id=(SELECT provider_id FROM payments.attempts WHERE id=%s)", (attempt,)).fetchone()
            effects = tx.execute("SELECT count(*) n FROM integrations.effects WHERE provider='stripe' AND effect_key=%s", (effect,)).fetchone()['n']
            orders = tx.execute('SELECT count(*) n FROM orders.orders WHERE id=%s', (status['order_id'],)).fetchone()['n']
        assert captured['n'] == 1 and captured['amount'] == quote['total_minor']
        assert effects == 1 and orders == 1
        assert app.orders.get_order(status['order_id'], actor=buyer)['total_minor'] == quote['total_minor']
    finally:
        if successor is not None:
            successor.stop()
        first.stop(crash=True)
