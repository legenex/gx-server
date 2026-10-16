import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import yaml

KIT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gateway_state',KIT/'tools/gateway_state.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
EXPECTED={'model_list':[{'model_name':'gx-fast','litellm_params':{'model':'openai/gx-fast','api_base':'http://host.docker.internal:18900/v1','api_key':'os.environ/KEY','merge_reasoning_content_in_choices':False},'model_info':{'max_input_tokens':65536,'max_output_tokens':8192}}]}
ACTUAL={'data':[{'model_name':'gx-fast','litellm_params':{**EXPECTED['model_list'][0]['litellm_params'],'api_key':'masked'},'model_info':EXPECTED['model_list'][0]['model_info']}]}

class GatewayStateTests(unittest.TestCase):
    def test_compares_loaded_alias_context_route_and_reasoning_fields(self):
        self.assertTrue(module.matches(EXPECTED,ACTUAL))
        for section,key,value in [('model_info','max_input_tokens',24576),('litellm_params','api_base','http://wrong'),('litellm_params','merge_reasoning_content_in_choices',True)]:
            changed=copy.deepcopy(ACTUAL);changed['data'][0][section][key]=value
            self.assertFalse(module.matches(EXPECTED,changed))
        self.assertFalse(module.matches(EXPECTED,{'data':[]}))
    def test_rejects_nonloopback_destinations_before_reading_any_credential(self):
        for url in ['https://example.org','http://127.0.0.1@evil.example','http://localhost:4000','http://127.0.0.1:4000/other']:
            with self.assertRaises(ValueError):module.verify(Path('/does-not-exist'),'landing',url)
    def test_actual_loaded_match_prevents_duplicate_gateway_recreation(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                raw=json.dumps(ACTUAL).encode();self.send_response(200);self.end_headers();self.wfile.write(raw)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);gw=p/'repo/legenex/gateway';(gw/'litellm').mkdir(parents=True)
                (gw/'.env').write_text('LITELLM_MASTER_KEY=unit-test-placeholder\n')
                (gw/'docker-compose.gateway.yml').write_text('services: {}\n')
                raw=yaml.safe_dump(EXPECTED)
                for name in ['config.dsv41.yaml','config.yaml']:(gw/'litellm'/name).write_text(raw)
                (gw/'identity-hmac.sh').write_text('#!/bin/sh\necho identity-verified >> "$FAKE_LOG"\n');(gw/'identity-hmac.sh').chmod(0o755)
                docker=p/'docker';docker.write_text('#!/bin/sh\necho "$*" >> "$FAKE_LOG"\nif [ "$1" = inspect ]; then echo "$FAKE_LABEL"; fi\n');docker.chmod(0o755)
                env={**os.environ,'GX_REPO_ROOT':str(p/'repo'),'GX_DOCKER':str(docker),'GX_GATEWAY_URL':f'http://127.0.0.1:{server.server_port}/v1','FAKE_LABEL':str(gw/'docker-compose.gateway.yml'),'FAKE_LOG':str(p/'log')}
                for _ in range(2):
                    run=subprocess.run(['bash',str(KIT/'tools/gateway_config.sh'),'landing'],capture_output=True,text=True,env=env,timeout=15)
                    self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                    self.assertIn('already loaded',run.stdout)
                    self.assertNotIn('unit-test-placeholder',run.stdout+run.stderr)
                log=(p/'log').read_text();self.assertNotIn('compose ',log);self.assertEqual(log.count('identity-verified'),2)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
