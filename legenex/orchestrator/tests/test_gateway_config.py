"""The native controller owns metadata reconciliation and rollback, not a new controller."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_dsv4_primary import SwitchTestBase,SUCCEEDED,ROLLED_BACK,ROLLBACK_FAILED
from gx_orchestrator.gateway_config import GatewayConfigurator,source_snapshot
import json
import hashlib
import os
import tempfile
import unittest

class TestNativeGateway(SwitchTestBase):
    def test_gateway_is_configured_while_paused_before_target_validation(self):
        events=[]
        def gateway(name):
            events.append(('gateway',name));self.assertEqual(self.sched.pause_log,['pause'])
        def validate(name,token):events.append(('validate',name));return self.validator(name,token)
        sw=self.switcher(configure_gateway=gateway);sw._validate=validate
        sw.request({'target':'dsv4-flash-primary','confirm':True},actor='owner')
        self.assertEqual(self.hist.read()[-1]['state'],SUCCEEDED)
        self.assertLess(events.index(('gateway','dsv4-flash-primary')),events.index(('validate','dsv4-flash-primary')))
        self.assertIn('gateway_config',[s['step'] for s in self.hist.read()[-1]['steps']])
    def test_target_metadata_failure_restores_previous_metadata_before_successful_rollback(self):
        calls=[]
        def gateway(name):
            calls.append(name)
            if name=='dsv4-flash-primary':raise RuntimeError('target metadata mismatch')
        self.switcher(configure_gateway=gateway).request({'target':'dsv4-flash-primary','confirm':True},actor='owner')
        self.assertEqual(calls,['dsv4-flash-primary','dsv41-rollback'])
        self.assertEqual(self.hist.read()[-1]['state'],ROLLED_BACK)
        self.assertEqual(self.mgr.active_name,'dsv41-rollback')
    def test_rollback_gateway_failure_is_never_reported_restored(self):
        def gateway(name):raise RuntimeError('gateway unavailable')
        self.switcher(configure_gateway=gateway).request({'target':'dsv4-flash-primary','confirm':True},actor='owner')
        self.assertEqual(self.hist.read()[-1]['state'],ROLLBACK_FAILED)
    def test_validation_failure_reconciles_previous_metadata(self):
        self.valid['dsv4-flash-primary']=False;calls=[]
        self.switcher(configure_gateway=calls.append).request({'target':'dsv4-flash-primary','confirm':True},actor='owner')
        self.assertEqual(calls,['dsv4-flash-primary','dsv41-rollback'])
        self.assertEqual(self.hist.read()[-1]['state'],ROLLED_BACK)

class TestGatewayProcess(unittest.TestCase):
    def test_receipt_uses_configured_registry_instead_of_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);src=p/'legenex/orchestrator/gx_orchestrator';src.mkdir(parents=True)
            (src/'server.py').write_text('code');(src.parent/'identity-policy.json').write_text('{}')
            custom=p/'custom-registry.json';custom.write_text('{"actual":true}')
            receipt=source_snapshot(p,custom)
            self.assertEqual(receipt['configured_registry_path'],str(custom))
            self.assertEqual(receipt['startup_registry_file_sha256'],hashlib.sha256(custom.read_bytes()).hexdigest())
            self.assertIsNotNone(receipt['startup_source_files_sha256'])
    def test_source_receipt_is_a_startup_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);src=p/'legenex/orchestrator/gx_orchestrator';src.mkdir(parents=True)
            (src/'server.py').write_text('old');(src.parent/'identity-policy.json').write_text('{}')
            (p/'legenex/models').mkdir();(p/'legenex/models/registry.json').write_text('{}')
            before=source_snapshot(p);saved=json.dumps(before)
            (src/'server.py').write_text('new');after=source_snapshot(p)
            self.assertNotEqual(before['startup_source_files_sha256'],after['startup_source_files_sha256'])
            self.assertEqual(json.dumps(before),saved)
    def test_timeout_terminates_script_and_its_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);tools=p/'legenex/models/dsv4-flash/tools';tools.mkdir(parents=True)
            pidfile=p/'child.pid';(tools/'gateway_config.sh').write_text('sleep 60 &\necho $! > "'+str(pidfile)+'"\nwait\n')
            with self.assertRaisesRegex(RuntimeError,'timed out'):
                GatewayConfigurator(p,p/'logs',timeout=.2)('dsv4-flash-primary')
            child=int(pidfile.read_text());stat=Path('/proc')/str(child)/'stat'
            self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z','descendant still running')
            self.assertEqual(len(list((p/'logs').glob('model-gateway-*.log'))),1)

if __name__=='__main__':unittest.main()
