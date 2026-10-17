"""Controller replacement refusals; systemd calls are recorded, never executed."""
import datetime as dt
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('install_window', Path(__file__).resolve().parents[1] / 'tools/install_window.py')
I = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(I)


class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026,10,16,20,0,tzinfo=tz)


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home, self.repo, self.live = (self.root/n for n in ('home','repo','live'))
        self.name = I.KIT + '/tools/window_run.sh'
        for root in (self.repo,self.live):
            path = root/self.name
            path.parent.mkdir(parents=True)
            path.write_text('#!/bin/bash\nexit 0\n')
        self.units = self.home/'.config/systemd/user'
        self.units.mkdir(parents=True)
        self.service = self.units/'gx-dsv4f-window.service'
        self.timer = self.units/'gx-dsv4f-window.timer'
        self.service.write_text('[Service]\nExecStart=/previous/reviewed/entry.sh\n')
        self.timer.write_text('[Timer]\nOnCalendar=2026-10-17 00:57:00 Africa/Johannesburg\n')
        self.calls = []
        self.active = 'inactive'
        self.fail_verify = False
        self.used = self.root/'used'
        self.used.mkdir()
        self.patches = [patch.object(I,'REPO',self.repo),patch.object(I,'LIVE',self.live),
                        patch.object(I,'USED_DIR',self.used),
                        patch.object(I.Path,'home',return_value=self.home),
                        patch.object(I,'checked',side_effect=self.checked),patch.object(I.dt,'datetime',Clock)]
        for p in self.patches:
            p.start()
        self.previous_umask = os.umask(0o077)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        os.umask(self.previous_umask)
        for p in self.patches:
            p.stop()
        for p in self.root.rglob('*'):
            p.chmod(0o700 if p.is_dir() else 0o600)
        self.temp.cleanup()

    def checked(self,args):
        self.calls.append(args)
        if args[:2] == ['git','status']:
            return ''
        if args[0] == 'git' and 'rev-parse' in args:
            return 'a'*40
        if args[:2] == ['git','ls-files']:
            return self.name
        if args[0] == 'loginctl':
            return 'yes'
        if args[:3] == ['systemctl','--user','show']:
            return self.active if 'ActiveState' in args else 'Sat 2026-10-17 00:57:00 SAST'
        if args[0] == 'systemd-analyze' and self.fail_verify:
            raise RuntimeError('candidate rejected')
        return ''

    def test_existing_controller_is_never_overwritten_implicitly(self):
        with self.assertRaisesRegex(ValueError,'explicit reviewed'):
            I.install(dt.date(2026,10,17))
        self.assertFalse(any('stop' in c for c in self.calls))

    def test_reviewed_replacement_keeps_one_absolute_timer_and_old_receipt(self):
        receipt = I.install(dt.date(2026,10,17),True)
        self.assertEqual(sum('stop' in c for c in self.calls),1)
        self.assertIn('Persistent=false',self.timer.read_text())
        self.assertIn('a'*40,self.service.read_text())
        self.assertTrue((Path(receipt['release'])/'PREVIOUS_UNITS.json').exists())
        self.assertEqual(sorted(p.name for p in self.units.glob('*.timer')),['gx-dsv4f-window.timer'])

    def test_active_service_is_never_replaced(self):
        self.active = 'activating'
        with self.assertRaisesRegex(ValueError,'executing'):
            I.install(dt.date(2026,10,17),True)
        self.assertFalse(any('stop' in c for c in self.calls))

    def test_live_dirty_source_is_not_blessed_in_a_new_manifest(self):
        (self.live/self.name).write_text('unreviewed')
        with self.assertRaisesRegex(ValueError,'differs from reviewed'):
            I.install(dt.date(2026,10,17),True)
        self.assertFalse(any('stop' in c for c in self.calls))

    def test_october_18_window_is_authorized(self):
        receipt = I.install(dt.date(2026, 10, 18), True)
        self.assertIn('2026-10-18 00:57:00 Africa/Johannesburg', self.timer.read_text())
        self.assertEqual(receipt['window'], '2026-10-18')

    def test_failed_candidate_validation_leaves_previous_timer_enabled(self):
        self.fail_verify = True
        before = self.timer.read_text()
        with self.assertRaisesRegex(RuntimeError,'candidate rejected'):
            I.install(dt.date(2026,10,17),True)
        self.assertEqual(self.timer.read_text(),before)
        self.assertFalse(any('stop' in c for c in self.calls))
