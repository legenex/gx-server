#!/usr/bin/env python3
"""Real production Hermes and LibreChat requests; no credentials leave hosts.

Only the pre-existing synthetic LibreChat QA account is used. The test creates
QA conversations and retains count/latency evidence, never text or credentials.
No model lifecycle operation or provider configuration is performed here.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess

HERMES = r'''
import json,os,time,urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
for profile,expected in [('bossman','gx-medium'),('jarvis','gx-fast'),('odin','gx-fast'),('hunter','gx-fast'),('alfred','gx-medium')]:
 start=time.monotonic()
 request=urllib.request.Request('http://127.0.0.1:8101/api/agentos/actions/profile-smoke',data=json.dumps({'profile':profile}).encode(),headers={'Authorization':'Bearer '+os.environ['AI_OS_ADMIN_TOKEN'],'Content-Type':'application/json'})
 with opener.open(request,timeout=160) as response:d=json.load(response)
 inner=d['result']['response'];smoke=inner['smoke']
 assert d.get('ok') is True and inner.get('ok') is True and smoke.get('ok') is True and smoke.get('verified_model') is True and smoke.get('model')==expected
 print(json.dumps({'client':'production-hermes','profile':profile,'model':expected,'ok':True,'seconds':round(time.monotonic()-start,3)}),flush=True)
'''

LIBRECHAT = r'''
import json,time,urllib.request,uuid
from pathlib import Path
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
qa=json.loads(Path('/root/librechat-qa-acceptance.json').read_text())
def call(path,body=None,token=None,limit=4*1024*1024):
 headers={'User-Agent':'Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36','Content-Type':'application/json'}
 if token:headers['Authorization']='Bearer '+token
 request=urllib.request.Request('http://127.0.0.1:3080'+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
 with opener.open(request,timeout=180) as response:
  raw=response.read(limit+1)
  assert len(raw)<=limit,'bounded client response required'
  return raw.decode()
login=json.loads(call('/api/auth/login',{'email':qa['email'],'password':qa['password']}));token=login['token']
for model in ('gx-auto','gx-fast','gx-medium','gx-high','gx-max'):
 start=time.monotonic()
 payload={'text':'Synthetic maintenance acceptance. Reply exactly: GX client accepted.','sender':'User','isCreatedByUser':True,'parentMessageId':'00000000-0000-0000-0000-000000000000','messageId':str(uuid.uuid4()),'conversationId':'new','endpoint':'GX','endpointType':'custom','model':model,'stream':True,'isContinued':False}
 job=json.loads(call('/api/agents/chat/GX',payload,token));stream=call('/api/agents/chat/stream/'+job['streamId'],token=token)
 events=[json.loads(line[6:]) for line in stream.splitlines() if line.startswith('data: ')]
 final=next((event for event in events if event.get('final')),{});response=final.get('responseMessage',{});content=response.get('content',[])
 text=response.get('text','')+''.join(str(part.get('text','')) for part in content if part.get('type')=='text')
 assert text.strip() and not any(part.get('type')=='error' for part in content) and '<think>' not in text.lower() and '</think>' not in text.lower(),'client completion failed'
 assert response.get('model')==model,'application returned a different logical model'
 print(json.dumps({'client':'production-librechat','model':model,'ok':True,'stream_events':len(events),'characters':len(text),'seconds':round(time.monotonic()-start,3),'qa_conversation_id':job['conversationId']}),flush=True)
'''


def run(out: Path) -> None:
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, host, command, script, expected in (
        ('hermes', 'dashflo', ['docker','exec','-i','ai-os-portal','python','-'], HERMES, 5),
        ('librechat', 'kvm2', ['python3','-'], LIBRECHAT, 5),
    ):
        result = subprocess.run(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10',host,shlex.join(command)],
                                input=script, text=True, capture_output=True, timeout=1000, check=False)
        (out/(name+'.jsonl')).write_text(result.stdout)
        # Errors are retained only in the private acceptance directory.
        (out/(name+'.stderr')).write_text(result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        if result.returncode or len(rows)!=expected or not all(row.get('ok') is True for row in rows):
            raise RuntimeError(name+' production client acceptance failed; inspect private evidence')
    print('CLIENT ACCEPTANCE: GO')


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out',type=Path)
    run(parser.parse_args().out)
