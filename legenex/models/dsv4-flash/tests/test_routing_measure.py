import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('routing_measure',Path(__file__).resolve().parents[1]/'tools/routing_measure.py')
M=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class TestMeasurement(unittest.TestCase):
    def test_classification_outcome_and_latency_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'journal.jsonl'
            fields={'agent_profile':'jarvis','initial_mode':'gx-fast','escalated_mode':'gx-high'}
            records=[{'ts':1000,'request_id':'a','event':'decision',**fields},
                     {'ts':1001,'request_id':'a','event':'completed','routing':fields,'status':200,'elapsed_ms':125},
                     {'ts':1002,'request_id':'b','event':'decision',**fields},
                     {'ts':1003,'request_id':'b','event':'escalation_outcome',**fields,'status':503,'outcome':'failed_before_completion'}]
            path.write_text('\n'.join(json.dumps(r) for r in records))
            row=M.measure([path],2000)['profiles']['jarvis']
            self.assertEqual((row['requests'],row['escalations'],row['failures']), (2,2,1))
            self.assertEqual(row['mean_latency_ms'],125)
            self.assertEqual(row['unresolved_requests'],0)

    def test_empty_or_expired_data_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'journal.jsonl'
            path.write_text(json.dumps({'ts':1,'event':'decision','request_id':'old','agent_profile':'odin'}))
            row=M.measure([path],100000)['profiles']['odin']
            self.assertEqual(row['state'],'NO_DATA')
            self.assertIsNone(row['escalation_rate'])
