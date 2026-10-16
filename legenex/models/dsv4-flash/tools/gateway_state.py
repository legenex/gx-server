"""Verify loaded gateway alias metadata/route parameters against reviewed source."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import urllib.request
import urllib.parse
import yaml

FILES={'dsv4':'config.dsv4-flash.yaml','landing':'config.dsv41.yaml'}


def matches(expected: dict, actual: dict) -> bool:
    want=expected.get('model_list') or []
    got=actual.get('data') or []
    if len(got)!=len(want) or {r.get('model_name') for r in got}!={r.get('model_name') for r in want}:
        return False
    by_name={r['model_name']:r for r in got}
    for row in want:
        live=by_name[row['model_name']]
        for field,values in row.items():
            if field not in ('model_info','litellm_params'):continue
            for key,value in values.items():
                if key=='api_key':continue  # never compare or expose a resolved credential
                if live.get(field,{}).get(key)!=value:return False
    return True


def verify(repo: Path,target: str, base: str='http://127.0.0.1:4000') -> bool:
    parsed=urllib.parse.urlsplit(base)
    if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('','/','/v1')):
        raise ValueError('gateway verification is loopback-only')
    gw=repo/'legenex/gateway'
    expected=yaml.safe_load((gw/'litellm'/FILES[target]).read_text())
    env={}
    for line in (gw/'.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);env[key.strip()]=value.strip().strip('"').strip("'")
    req=urllib.request.Request('http://'+parsed.netloc+'/model/info',headers={'Authorization':'Bearer '+env['LITELLM_MASTER_KEY']})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(req,timeout=10) as response:
        raw=response.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('gateway metadata exceeds limit')
    actual=json.loads(raw)
    return matches(expected,actual)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo',type=Path,required=True);p.add_argument('--target',choices=FILES,required=True);p.add_argument('--gateway-url',default='http://127.0.0.1:4000');a=p.parse_args()
    try:ok=verify(a.repo,a.target,a.gateway_url)
    except Exception:ok=False  # credentials/HTTP bodies never leave this verifier
    print('loaded gateway configuration matches reviewed target' if ok else 'loaded gateway configuration differs or cannot be verified')
    raise SystemExit(0 if ok else 1)
