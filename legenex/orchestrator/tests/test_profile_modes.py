"""Task classification reuses gx-auto and obeys authenticated AgentOS bounds."""
import sys
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gx_orchestrator import modes as M
from gx_orchestrator.profile_modes import decide, ProfileModeError
from gx_orchestrator.profiles import load_registry
from gx_orchestrator.identity import load_policy
from gx_orchestrator.lifecycle import State
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server_harness import OrchestratorHarness, signed_identity
from test_dsv4_primary import FakeManager

REG = Path(__file__).resolve().parents[3] / 'legenex/models/registry.json'
POLICIES = {
    'bossman': {'floor': 'gx-medium', 'ceiling': 'gx-max'},
    'jarvis': {'floor': 'gx-fast', 'ceiling': 'gx-high'},
    'odin': {'floor': 'gx-fast', 'ceiling': 'gx-high'},
    'hunter': {'floor': 'gx-fast', 'ceiling': 'gx-high'},
    'alfred': {'floor': 'gx-medium', 'ceiling': 'gx-high'},
}


class TestProfileModes(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry(REG)

    def choose(self, profile, text, alias=None, fetch=lambda: POLICIES):
        return decide('hermes-profile-' + profile, alias or POLICIES[profile]['floor'],
                      {'messages': [{'role':'user','content':text}]}, self.registry, fetch)

    def test_routine_requests_stay_at_each_profiles_floor(self):
        for profile, spec in POLICIES.items():
            self.assertEqual('gx-' + self.choose(profile, 'hello').decision.mode, spec['floor'])

    def test_task_classification_is_the_existing_auto_decision(self):
        prompt = 'Design the architecture for a distributed service with failure recovery and database migrations.'
        auto = M.choose_mode('gx-auto', {'messages':[{'role':'user','content':prompt}]}, self.registry)
        bounded = self.choose('jarvis', prompt)
        self.assertEqual(bounded.decision.route.intent, auto.route.intent)
        self.assertEqual(bounded.decision.mode, auto.mode)
        self.assertNotEqual(bounded.decision.mode, 'fast')

    def test_long_context_cannot_escape_the_ceiling(self):
        bounded = self.choose('jarvis', 'x' * (200_000 * 4))
        self.assertEqual(bounded.decision.mode, 'high')
        self.assertIn('capped', bounded.decision.reason)
        self.assertEqual(M.mode_budget({'messages':[{'role':'user','content':'x'*800_000}]},
                         self.registry.mode('high'), 'gx-fast').status, 'overflow')

    def test_direct_aliases_cannot_raise_or_lower_bounds(self):
        for profile in ('jarvis','odin','hunter','alfred'):
            with self.assertRaises(ProfileModeError):
                self.choose(profile, 'hello', 'gx-max')
        with self.assertRaises(ProfileModeError):
            self.choose('bossman', 'hello', 'gx-fast')

    def test_policy_outage_refuses_instead_of_unbounded_fallback(self):
        def unavailable():
            raise TimeoutError('management tunnel unavailable')
        with self.assertRaises(ProfileModeError) as raised:
            self.choose('jarvis', 'hello', fetch=unavailable)
        self.assertEqual(raised.exception.status, 503)

    def test_untrusted_identity_cannot_select_profile(self):
        for identity in ('', 'anonymous', 'hermes', 'team:hermes-profile-bossman'):
            self.assertIsNone(decide(identity, 'gx-fast', {'profile':'bossman'}, self.registry))

    def test_unknown_profile_and_invalid_policy_fail_closed(self):
        with self.assertRaises(ProfileModeError):
            decide('hermes-profile-stranger', 'gx-auto', {}, self.registry, lambda: POLICIES)
        with self.assertRaises(ProfileModeError):
            self.choose('jarvis', 'hi', fetch=lambda:{'jarvis':{'floor':'gx-max','ceiling':'gx-fast'}})


class TestProfileModeHTTP(OrchestratorHarness):
    def setUp(self):
        super().setUp()
        reg = load_registry(REG)
        self.registry = replace(reg, runtimes={k:replace(v, api=self.upstream.base) for k,v in reg.runtimes.items()})
        self._handler_cls.registry = self.registry

    def select(self, deployment):
        self._handler_cls.manager = FakeManager(self.registry, deployment, self.lifecycle)
        runtime = self.registry.runtime(self.registry.deployment(deployment).runtime)
        self.upstream.model_id = runtime.served_model_id

    def test_auto_and_direct_aliases_obey_ceiling_on_both_deployments(self):
        with patch('gx_orchestrator.profile_modes.fetch_policies', return_value=POLICIES):
            for deployment in ('dsv41-rollback','dsv4-flash-primary'):
                self.select(deployment)
                status, _, _ = self.chat({'model':'gx-max','messages':[{'role':'user','content':'hi'}]},
                                          headers=signed_identity('hermes-profile-jarvis'))
                self.assertEqual(status, 403)
                status, headers, _ = self.chat({'model':'gx-auto','messages':[{'role':'user','content':'hi'}]},
                                                headers=signed_identity('hermes-profile-bossman'))
                self.assertEqual(status, 200)
                self.assertEqual(headers['X-GX-Mode'], 'medium')

    def test_live_policy_outage_has_no_unbounded_inference_fallback(self):
        with patch('gx_orchestrator.profile_modes.fetch_policies', side_effect=TimeoutError):
            for deployment in ('dsv41-rollback','dsv4-flash-primary'):
                self.select(deployment)
                for alias in ('gx-auto','gx-max','gx-fast'):
                    status, _, _ = self.chat({'model':alias,'messages':[{'role':'user','content':'hi'}]},
                                              headers=signed_identity('hermes-profile-jarvis'))
                    self.assertEqual(status, 503)
        self.assertEqual(len(self.upstream.requests), 0)

    def test_context_failure_retains_profile_task_and_outcome(self):
        with patch('gx_orchestrator.profile_modes.fetch_policies', return_value=POLICIES):
            self.select('dsv41-rollback')
            headers={**signed_identity('hermes-profile-jarvis'),'X-GX-Task':'bounded-context-test'}
            status,_,_=self.chat({'model':'gx-fast','messages':[{'role':'user','content':'x'*800000}]},headers=headers)
            self.assertEqual(status,400)
            rows=[json.loads(line) for line in self.journal.path.read_text().splitlines()]
            self.assertEqual(rows[-1]['outcome'],'context_rejected')
            self.assertEqual(rows[-1]['agent_profile'],'jarvis')
            self.assertEqual(rows[-1]['task_id'],'bounded-context-test')

    def test_scoped_complex_request_can_wake_without_claiming_health_priority(self):
        self._handler_cls.identity_policy=load_policy()
        with patch('gx_orchestrator.profile_modes.fetch_policies', return_value=POLICIES):
            for deployment in ('dsv41-rollback','dsv4-flash-primary'):
                self.select(deployment); self.lifecycle._state=State.DOWN
                headers={**signed_identity('hermes-profile-bossman'),'X-GX-Priority':'health'}
                status,_,_=self.chat({'model':'gx-medium','messages':[{'role':'user','content':'hi'}]},headers=headers)
                self.assertEqual(status,200)
        self.assertTrue(self.lifecycle.acquire_calls)


if __name__ == '__main__':
    unittest.main()
