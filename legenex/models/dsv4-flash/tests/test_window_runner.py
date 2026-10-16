"""Acceptance scenarios for the unattended window; no production mutations."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('window_runner', Path(__file__).resolve().parents[1] / 'tools/window_runner.py')
W = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(W)


class FakeOperations:
    def __init__(self):
        self.clock = dt.datetime.fromisoformat('2026-10-17T00:57:00+02:00')
        self.active = W.ROLLBACK
        self.lifecycle = 'ready'
        self.current = None
        self.posts = []
        self.gateways = []
        self.failure = None
        self.polls = 0
        self.accepted = False
        self.status_failures_after_post = 0

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += dt.timedelta(seconds=seconds)

    def preflight(self):
        if self.failure == 'preflight':
            raise ValueError('integrity mismatch')
        return {'nodes': ['head', 'worker'], 'integrity': True}

    def api(self, path, body=None):
        if path == '/models/activate':
            self.posts.append(body)
            self.current = {'id': str(len(self.posts)), 'state': 'running', 'target': body['target']}
            self.polls = 0
            if self.failure == 'transient_poll' and body['target'] == W.TARGET:
                self.status_failures_after_post = 2
            if self.failure == 'lost_rollback_response' and body['target'] == W.ROLLBACK:
                raise TimeoutError('rollback accepted; response lost')
            if self.failure == 'lost_response' and body['target'] == W.TARGET:
                raise TimeoutError('response lost after request accepted')
            return {'switch': dict(self.current)}
        if path == '/models/history':
            return {'data': [self.current] if self.current else []}
        if path == '/models/status':
            if self.status_failures_after_post:
                self.status_failures_after_post -= 1
                raise TimeoutError('temporary status outage')
            if self.current and self.current['state'] == 'running':
                self.polls += 1
                if self.polls >= 3:
                    self.current['state'] = 'succeeded'
                    self.active = self.current['target']
                    self.lifecycle = 'ready'
                    if self.failure == 'activation' and self.active == W.TARGET:
                        self.active = W.ROLLBACK
                        self.current['state'] = 'rolled_back'
            return {'active': self.active, 'lifecycle': {'state': self.lifecycle},
                    'switch': dict(self.current) if self.current else None}
        raise AssertionError(path)

    def gateway(self, target, seconds):
        self.gateways.append(target)
        if self.failure == 'landing' and target == 'landing':
            raise RuntimeError('gateway validation failed')
        if self.failure == 'gateway' and target == 'dsv4':
            raise RuntimeError('gateway recreation failed')

    def accept(self, seconds):
        if self.failure in ('acceptance', 'landing', 'lost_rollback_response'):
            raise ValueError('ACCEPTANCE NO-GO')
        if self.failure == 'interrupt':
            raise InterruptedError('worker TERM')
        if self.failure == 'timeout':
            self.sleep(seconds + 1)
            raise TimeoutError('acceptance time limit')
        self.accepted = True


class TestWindow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.ops = FakeOperations()
        self.run = self.root / 'run'
        self.run.mkdir()
        self.runner = W.Runner(self.ops, '2026-10-17', self.state, self.run)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def result(self):
        return json.loads(self.runner.final.read_text())['result']

    def test_success_and_duplicate_never_posts_twice(self):
        self.assertEqual(self.runner.run_window(), 0)
        self.assertTrue(self.ops.accepted)
        self.assertEqual(self.result(), 'activated')
        self.assertEqual(self.runner.run_window(), 0)
        self.assertEqual(self.ops.posts, [{'target': W.TARGET, 'confirm': True}])
        self.assertEqual(self.ops.gateways, ['dsv4'])

    def test_failed_preflight_never_mutates(self):
        self.ops.failure = 'preflight'
        self.assertEqual(self.runner.run_window(), 4)
        self.assertEqual(self.result(), 'preflight_failed')
        self.assertFalse(self.ops.posts)

    def test_late_trigger_never_replays(self):
        self.ops.clock = self.runner.end('06:00:00')
        self.assertEqual(self.runner.run_window(), 4)
        self.assertEqual(self.result(), 'missed_window')
        self.assertFalse(self.ops.posts)

    def test_wrong_date_never_activates(self):
        self.ops.clock += dt.timedelta(days=1)
        self.assertEqual(self.runner.run_window(), 4)
        self.assertFalse(self.ops.posts)

    def test_failed_activation_already_restored_by_switcher(self):
        self.ops.failure = 'activation'
        self.assertEqual(self.runner.run_window(), 5)
        self.assertEqual(self.result(), 'rolled_back')
        self.assertEqual(len(self.ops.posts), 1)
        self.assertEqual(self.ops.gateways, ['landing'])

    def test_failure_and_worker_interruption_roll_back_once(self):
        for failure in ('gateway', 'acceptance', 'interrupt', 'timeout'):
            with self.subTest(failure=failure):
                self.runner.marker.unlink(missing_ok=True)
                self.runner.final.unlink(missing_ok=True)
                self.ops = FakeOperations()
                self.ops.failure = failure
                self.runner = W.Runner(self.ops, '2026-10-17', self.state, self.run)
                self.assertEqual(self.runner.run_window(), 5)
                self.assertEqual(self.result(), 'rolled_back')
                self.assertEqual([p['target'] for p in self.ops.posts], [W.TARGET, W.ROLLBACK])

    def test_lost_response_reconciles_without_repeating_activation(self):
        self.ops.failure = 'lost_response'
        self.assertEqual(self.runner.run_window(), 5)
        self.assertEqual([p['target'] for p in self.ops.posts], [W.TARGET, W.ROLLBACK])

    def test_landing_failure_never_reports_restored(self):
        self.ops.failure = 'landing'
        self.assertEqual(self.runner.run_window(), 6)
        self.assertEqual(self.result(), 'rollback_failed')
        self.assertEqual(len(self.ops.posts), 2)

    def test_restart_after_sigkill_only_recovers(self):
        self.ops.clock = self.runner.end('02:00:00')
        self.ops.active = W.TARGET
        self.runner.save('acceptance', activation_requested=True)
        self.assertEqual(self.runner.run_window(recover=True), 5)
        self.assertEqual([p['target'] for p in self.ops.posts], [W.ROLLBACK])

    def test_active_pointer_is_not_readiness(self):
        self.ops.clock = self.runner.end('02:00:00')
        self.ops.active = W.ROLLBACK
        self.ops.lifecycle = 'acquiring'
        self.ops.current = {'id': 'a', 'target': W.ROLLBACK, 'state': 'running'}
        self.runner.save('rollback_waiting', activation_requested=True, rollback_requested=True)
        self.assertEqual(self.runner.run_window(recover=True), 5)
        self.assertFalse(self.ops.posts)
        self.assertGreaterEqual(self.ops.polls, 3)

    def test_ambiguous_rollback_is_never_retried(self):
        self.ops.clock = self.runner.end('02:00:00')
        self.ops.active = W.TARGET
        self.runner.save('rollback_requested', activation_requested=True, rollback_requested=True)
        self.assertEqual(self.runner.run_window(recover=True), 6)
        self.assertFalse(self.ops.posts)

    def test_recovery_outside_window_never_posts(self):
        self.ops.clock = self.runner.end('05:01:00')
        self.ops.active = W.TARGET
        self.runner.save('acceptance', activation_requested=True)
        self.assertEqual(self.runner.run_window(recover=True), 6)
        self.assertFalse(self.ops.posts)

    def test_dry_run_does_not_consume_the_window(self):
        self.assertEqual(self.runner.run_window(dry=True), 0)
        self.assertFalse((self.state / '2026-10-17.result.json').exists())
        self.assertFalse(self.runner.marker.exists())
        self.assertFalse(self.ops.posts)

    def test_failed_dry_run_does_not_consume_the_window(self):
        self.ops.failure = 'preflight'
        self.assertEqual(self.runner.run_window(dry=True), 4)
        self.assertFalse((self.state / '2026-10-17.result.json').exists())
        self.assertFalse(self.runner.marker.exists())

    def test_interruption_before_activation_never_rolls_back_someone_else(self):
        self.ops.clock = self.runner.end('01:01:00')
        self.ops.active = W.TARGET
        self.runner.save('waiting_for_window')
        self.assertEqual(self.runner.run_window(recover=True), 4)
        self.assertFalse(self.ops.posts)

    def test_status_outage_final_receipt_does_not_suppress_recovery(self):
        self.ops.failure = 'transient_poll'
        self.assertEqual(self.runner.run_window(), 6)
        self.assertEqual(self.result(), 'rollback_failed')
        self.assertEqual(self.runner.run_window(recover=True), 5)
        self.assertEqual(self.result(), 'rolled_back')
        self.assertEqual([p['target'] for p in self.ops.posts], [W.TARGET, W.ROLLBACK])

    def test_lost_rollback_ack_is_reconciled_without_second_rollback(self):
        self.ops.failure = 'lost_rollback_response'
        self.assertEqual(self.runner.run_window(), 6)
        self.assertEqual(self.runner.run_window(recover=True), 5)
        self.assertEqual(self.result(), 'rolled_back')
        self.assertEqual([p['target'] for p in self.ops.posts], [W.TARGET, W.ROLLBACK])

    def test_cutoff_is_rechecked_after_lifecycle_wait(self):
        self.ops.lifecycle = 'acquiring'
        self.assertEqual(self.runner.run_window(), 4)
        self.assertFalse(self.ops.posts)
        self.assertEqual(self.result(), 'not_started')


if __name__ == '__main__':
    unittest.main()
