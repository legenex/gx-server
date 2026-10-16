"""One reviewed maintenance window, one activation, durable receipts and recovery.

Only ModelSwitcher performs lifecycle changes. A lost response is ambiguous:
never repeat a POST. A restarted runner can only reconcile/restore V4.1.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import shlex

SAST = dt.timezone(dt.timedelta(hours=2))
TARGET = 'dsv4-flash-primary'
ROLLBACK = 'dsv41-rollback'
TOOLS = Path(__file__).resolve().parent
LIVE = Path('/home/legenex/Documents/Projects/Server/gx-cluster')
STATE = Path('/srv/projects/gx-cluster/state/dsv4f-window')
LOGS = Path('/srv/logs/dsv4f-window')
WORKER = 'legenex-02@10.60.21.41'


def atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as out:
        json.dump(data, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def execute(args: list[str], timeout: float, *, stdin: str | None = None,
            env: dict | None = None) -> str:
    """Bound the entire child process group, including SSH/grandchildren."""
    proc = subprocess.Popen(args, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True, env=env)
    try:
        output, _ = proc.communicate(stdin, timeout=timeout)
    except BaseException:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)
        raise
    if proc.returncode:
        # Tool output goes only to the private run directory, never credentials.
        raise RuntimeError(f'{Path(args[0]).name} failed ({proc.returncode}): {output[-2000:]}')
    return output


class Operations:
    def __init__(self, run: Path, window: str):
        self.run = run
        self.window = window
        values = {}
        for line in (LIVE / 'legenex/gateway/.env').read_text().splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                values[key] = value.strip().strip('"').strip("'")
        self.headers = {'Authorization': 'Bearer ' + values['GX_ORCHESTRATOR_API_KEY'],
                        'X-GX-Operator-Token': Path('/srv/projects/gx-cluster/secrets/orchestrator/operator-token').read_text().strip(),
                        'X-GX-Operator': 'gx-dsv4f-window-' + window,
                        'Content-Type': 'application/json'}

    def now(self) -> dt.datetime:
        return dt.datetime.now(SAST)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def api(self, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request('http://127.0.0.1:18900' + path,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers=self.headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError('orchestrator returned a non-object')
        return data

    def owner_api(self, path: str, body: dict | None = None) -> dict:
        # Existing owner credential remains inside its existing portal container.
        # Only the enrolled GX agent and native ModelSwitcher enact the change.
        if path not in ('/api/gx-models', '/api/gx-models/activate'):
            raise ValueError('unsupported owner control path')
        script = """
import json,os,urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
p=json.load(__import__('sys').stdin)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
r=urllib.request.Request('http://127.0.0.1:8101'+p['path'],data=None if p['body'] is None else json.dumps(p['body']).encode(),headers={'Authorization':'Bearer '+os.environ['AI_OS_ADMIN_TOKEN'],'Content-Type':'application/json'})
with opener.open(r,timeout=55) as response:
 raw=response.read(1024*1024+1)
 if len(raw)>1024*1024:raise ValueError('owner response exceeds limit')
 print(raw.decode())
"""
        value = json.loads(execute(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                    'dashflo', shlex.join(['docker', 'exec', '-i', 'ai-os-portal', 'python', '-c', script])],
                                   70, stdin=json.dumps({'path': path, 'body': body})))
        if not isinstance(value, dict):
            raise ValueError('owner response is not an object')
        return value

    def native_switch_settled(self) -> bool:
        # After expiry, also exclude a request accepted just before its cutoff
        # but not yet visible in /models/status. Native request takes this lock
        # before publishing a running record or starting any lifecycle work.
        path = Path('/srv/projects/gx-cluster/state/orchestrator/model-switch.lock')
        try:
            with path.open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def preflight(self) -> dict:
        status = self.api('/models/status')
        if status.get('active') != ROLLBACK or (status.get('switch') or {}).get('state') == 'running':
            raise ValueError('expected V4.1 with no running switch')
        ready = self.api('/models/rollback-readiness')['deployments']
        if set(ready) != {TARGET, ROLLBACK} or not all(d.get('rollback_ready') is True for d in ready.values()):
            raise ValueError('both deployment rollback-readiness checks must pass')
        nodes = {}
        for node, args in (('head', ['/usr/bin/python3', '-I', str(TOOLS / 'window_preflight.py')]),
                           ('worker', ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                       WORKER, 'python3 -I - --worker'])):
            raw = execute(args, 900, stdin=(TOOLS / 'window_preflight.py').read_text() if node == 'worker' else None)
            (self.run / f'preflight-{node}.json').write_text(raw)
            nodes[node] = json.loads(raw)
        # When V4.1 is resident we cannot infer reclaimed memory. The typed
        # switch checks both nodes again after its sanctioned V4.1 release.
        if status['lifecycle']['state'] == 'down' and not all(n['memory_boot_safe'] for n in nodes.values()):
            raise ValueError('idle node memory is below the fixed boot floor')
        for node in ('node1', 'node2'):
            guard = Path('/srv/projects/gx-cluster/state/guard') / f'{node}.maintenance-hold'
            if guard.exists():
                raise ValueError('maintenance hold exists: ' + node)
        lock = Path('/srv/projects/gx-cluster/state/orchestrator/model-switch.lock')
        if lock.exists():
            with lock.open('r') as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue = self.api('/scheduler/status')
        if int(queue.get('active', -1)) != 0 or int(queue.get('queued', -1)) != 0:
            raise ValueError('in-flight or queued model requests; refuse disruption')
        # Fixed authenticated read, no owner token leaves the portal container.
        script = ('import os,json,urllib.request; '
                  'r=urllib.request.Request("http://127.0.0.1:8101/api/work-orders?limit=200",'
                  'headers={"Authorization":"Bearer "+os.environ["AI_OS_ADMIN_TOKEN"]}); '
                  'd=json.load(urllib.request.urlopen(r,timeout=15)); '
                  'print(json.dumps({"active":[w["short_ref"] for w in d["work_orders"] '
                  'if w["state"] in ("RECEIVED","TRIAGED","READY","RUNNING","TESTING","REVIEW","DEPLOYING","VERIFYING")]}))')
        import shlex
        orders = json.loads(execute(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                    'dashflo', 'docker exec ai-os-portal python -c ' + shlex.quote(script)], 30))
        if orders['active']:
            raise ValueError('AI OS work orders still executing')
        have = execute(['docker', 'inspect', '-f', '{{index .Config.Labels "com.docker.compose.project.config_files"}}', 'gx-litellm'], 15).strip()
        expected = str(LIVE / 'legenex/gateway/docker-compose.gateway.yml')
        if os.path.realpath(have) != expected:
            raise ValueError('gateway compose source differs from approved live checkout')
        # Gateway configuration helper resolves assets from the reviewed live
        # checkout; its file set is pinned by the installed release manifest.
        with urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=5) as response:
            if response.status != 200:
                raise ValueError('gateway not live')
        owner = self.owner_api('/api/gx-models')
        if (not owner.get('ok') or owner.get('checks', {}).get('status', {}).get('active') != ROLLBACK
                or owner.get('action_expiry_seconds') != 120 or status.get('action_expiry_seconds') != 120):
            raise ValueError('AI OS owner model inventory unavailable or deployment changed')
        return {'owner_inventory': owner, 'nodes': nodes, 'rollback_readiness': ready, 'queue': queue, 'work_orders': orders}

    def gateway(self, target: str, seconds: float) -> None:
        env = {**os.environ, 'GX_REPO_ROOT': str(LIVE)}
        output = execute(['/bin/bash', str(TOOLS / 'gateway_config.sh'), target], seconds, env=env)
        (self.run / f'gateway-{target}.log').write_text(output)

    def accept(self, seconds: float, label: str = 'accept') -> None:
        env = {**os.environ, 'GX_GATEWAY_ENV': str(LIVE / 'legenex/gateway/.env')}
        output = execute(['/bin/bash', str(TOOLS / 'accept.sh'), str(self.run / label)], seconds, env=env)
        (self.run / (label + '.log')).write_text(output)
        if 'ACCEPTANCE: GO' not in output.splitlines():
            raise ValueError('acceptance did not explicitly return GO')

    def clients(self, seconds: float, label: str) -> None:
        output = execute(['/usr/bin/python3', '-I', str(TOOLS / 'client_accept.py'), str(self.run / label)], seconds)
        (self.run / (label + '.log')).write_text(output)
        if 'CLIENT ACCEPTANCE: GO' not in output.splitlines():
            raise ValueError('production clients did not explicitly return GO')


class Runner:
    def __init__(self, ops: Operations, window: str, state: Path, run: Path):
        self.ops, self.window, self.state, self.run = ops, window, state, run
        self.marker = state / (window + '.used.json')
        self.final = state / (window + '.result.json')
        self.progress: dict = {'window': window, 'phase': 'preflight', 'switch_ids': [], 'evidence': str(run)}
        self.end = lambda clock: dt.datetime.fromisoformat(window + 'T' + clock + '+02:00')

    def step(self, name: str, detail: object) -> None:
        record = {'at': self.ops.now().isoformat(), 'step': name, 'detail': detail}
        with (self.run / 'steps.jsonl').open('a') as output:
            output.write(json.dumps(record) + '\n')
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps(record), flush=True)

    def save(self, phase: str, **values) -> None:
        self.progress.update(phase=phase, **values)
        atomic(self.marker, self.progress)

    def finish(self, result: str, detail: str, code: int) -> int:
        receipt = {**self.progress, 'result': result, 'detail': detail,
                   'ended': self.ops.now().isoformat(), 'evidence': str(self.run),
                   'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        release = TOOLS.parents[3] / 'RELEASE.json'
        receipt['release'] = json.loads(release.read_text()) if release.exists() else {'source': 'development worktree'}
        try:
            receipt['models_status'] = self.ops.api('/models/status')
            receipt['switch_history'] = self.ops.api('/models/history')
        except Exception as exc:
            receipt['status_error'] = type(exc).__name__
        atomic(self.run / 'result.json', receipt)
        atomic(self.final, receipt)
        self.step('result', {'result': result, 'detail': detail})
        return code

    def poll_switch(self, ident: str, deadline: dt.datetime) -> dict:
        while self.ops.now() < deadline:
            status = self.ops.api('/models/status')
            current = status.get('switch') or {}
            if current.get('id') != ident:
                raise ValueError('switch identity changed; do not infer success')
            self.step('switch_poll', {'id': ident, 'state': current.get('state')})
            if current.get('state') != 'running':
                return status
            self.ops.sleep(15)
        raise TimeoutError('switch deadline reached')

    def activate(self, target: str) -> str:
        key = 'activation' if target == TARGET else 'rollback'
        # Persist intent BEFORE the side effect. Lost acknowledgement is never
        # permission for a second activation or a second rollback request.
        if not self.end('01:00:00') <= self.ops.now() < self.end('04:30:00'):
            raise TimeoutError('insufficient authorized time for a native activation/rollback')
        expiry = int(self.ops.now().timestamp()) + 90
        self.save(key + '_requested', **{key + '_requested': True, key + '_not_after': expiry})
        response = self.ops.api('/models/activate', {'target': target, 'confirm': True, 'not_after': expiry})
        ident = response['switch']['id']
        self.progress['switch_ids'].append(ident)
        self.save(key + '_waiting', **{key + '_switch': ident})
        return ident

    def owner_switch(self, phase: str, target: str, expected: str, deadline: dt.datetime) -> None:
        if self.ops.now() >= deadline or not self.end('01:00:00') <= self.ops.now() < self.end('04:15:00'):
            raise TimeoutError('owner drill deadline reached before intent')
        if self.progress.get(phase + '_requested'):
            raise ValueError('owner drill intent already exists; never repeat a POST')
        operation = str(uuid.uuid5(uuid.NAMESPACE_URL, 'legenex/gx-window/' + self.window + '/' + phase))
        expiry = int(self.ops.now().timestamp()) + 90
        self.save(phase + '_requested', **{phase + '_requested': True, phase + '_operation': operation, phase + '_not_after': expiry})
        response = self.ops.owner_api('/api/gx-models/activate', {
            'target': target, 'expected_active': expected, 'operation_id': operation, 'confirm': target, 'not_after': expiry})
        atomic(self.run / (phase + '-owner-receipt.json'), response)
        receipt = response.get('receipt') or {}
        if (response.get('ok') is not True or response.get('accepted') is not True
                or receipt.get('operation_id') != operation or receipt.get('target') != target
                or receipt.get('expected_active') != expected or receipt.get('not_after') != expiry):
            raise ValueError('owner drill receipt does not match its durable intent')
        ident = receipt['result']['switch']['id']
        self.progress['switch_ids'].append(ident)
        self.save(phase + '_waiting', **{phase + '_switch': ident})
        status = self.poll_switch(ident, deadline)
        if ((status.get('switch') or {}).get('state') != 'succeeded'
                or status.get('active') != target or status['lifecycle']['state'] != 'ready'
                or status['switch'].get('target') != target or status['switch'].get('previous') != expected
                or status['switch'].get('actor') != 'ai-os-owner-' + operation + ' (token) via 127.0.0.1'):
            raise ValueError('owner drill switch did not reach validated readiness')
        left = (deadline - self.ops.now()).total_seconds()
        if left <= 0:
            raise TimeoutError('owner drill gateway deadline')
        self.ops.gateway('dsv4' if target == TARGET else 'landing', min(180, left))
        self.save(phase + '_verified', **{phase + '_verified': True})

    def restore(self, reason: str) -> int:
        self.step('recovery', reason)
        # Reserve the last fifteen minutes for a human intervention; no new typed
        # operation is ever sent outside the authorized 01:00-05:00 window.
        deadline = self.end('04:45:00')
        while self.end('01:00:00') <= self.ops.now() < deadline:
            expiry = max((self.progress.get(p + '_not_after', 0) for p in ('activation','rollback','drill_v41','drill_flash')), default=0)
            if self.ops.now().timestamp() <= expiry or not self.ops.native_switch_settled():
                self.ops.sleep(5)
                continue
            status = self.ops.api('/models/status')
            switch = status.get('switch') or {}
            if switch.get('state') == 'running':
                self.ops.sleep(15)
                continue
            if status.get('active') == ROLLBACK and status['lifecycle']['state'] == 'ready':
                self.ops.gateway('landing', min(180, (deadline - self.ops.now()).total_seconds()))
                return self.finish('rolled_back', reason + '; V4.1 ready and gateway verified', 5)
            if status.get('active') != TARGET:
                # An aborted pre-stop request can leave healthy V4.1 down.
                if status.get('active') == ROLLBACK and switch.get('state') == 'aborted':
                    return self.finish('aborted', reason + '; V4.1 untouched', 4)
                raise ValueError('rollback state is not safe to act on')
            if self.progress.get('rollback_requested'):
                # Do not repeat a rollback whose acknowledgement was lost.
                raise ValueError('rollback outcome ambiguous; refusing a duplicate POST')
            ident = self.activate(ROLLBACK)
            status = self.poll_switch(ident, deadline)
            if (status.get('switch') or {}).get('state') != 'succeeded':
                raise ValueError('typed rollback did not succeed')
        return self.finish('rollback_failed', reason + '; authorized recovery deadline expired', 6)

    def run_window(self, *, dry: bool = False, recover: bool = False) -> int:
        if dry:
            self.final = self.run / 'dry-run-result.json'
        unresolved = (not dry and self.final.exists()
                      and json.loads(self.final.read_text()).get('result') == 'rollback_failed')
        if not dry and self.final.exists() and not (recover and unresolved):
            self.step('duplicate', 'window has a retained final receipt; no action')
            return 0
        if not dry and self.marker.exists():
            self.progress = json.loads(self.marker.read_text())
            if not self.progress.get('activation_requested'):
                return self.finish('not_started', 'interrupted before activation; no recovery mutation', 4)
            try:
                return self.restore('interrupted runner; recovery only')
            except Exception as exc:
                return self.finish('rollback_failed', f'recovery failed: {type(exc).__name__}: {exc}', 6)
        if recover:
            return 0
        now = self.ops.now()
        if not dry and not self.end('00:50:00') <= now < self.end('01:15:00'):
            return self.finish('missed_window', 'late/early trigger refused; no activation', 4)
        try:
            self.step('preflight', self.ops.preflight())
        except Exception as exc:
            return self.finish('preflight_failed', f'{type(exc).__name__}: {exc}', 4)
        if dry:
            # Dry-run receipts must not consume the actual window.
            self.final = self.run / 'dry-run-result.json'
            return self.finish('dry_run_ok', 'all non-disruptive preflight checks passed', 0)
        self.save('waiting_for_window')
        try:
            while self.ops.now() < self.end('01:00:30'):
                self.ops.sleep(10)
            # Refresh cheap safety gates immediately before the switch.
            while self.ops.now() < self.end('01:20:00'):
                status = self.ops.api('/models/status')
                if status['lifecycle']['state'] in ('ready', 'down'):
                    break
                self.ops.sleep(15)
            if self.ops.now() >= self.end('01:20:00'):
                return self.finish('not_started', 'lifecycle/cutoff gate failed; V4.1 untouched', 4)
            if status.get('active') != ROLLBACK or (status.get('switch') or {}).get('state') == 'running':
                return self.finish('not_started', 'deployment changed after preflight; no activation', 4)
            ident = self.activate(TARGET)
            status = self.poll_switch(ident, self.end('01:50:00'))
            switch = status.get('switch') or {}
            if switch.get('state') != 'succeeded' or status.get('active') != TARGET or status['lifecycle']['state'] != 'ready':
                return self.restore('Flash switch did not reach validated readiness')
            self.ops.gateway('dsv4', min(180, (self.end('02:30:00') - self.ops.now()).total_seconds()))
            left = (self.end('02:30:00') - self.ops.now()).total_seconds()
            if left <= 0:
                raise TimeoutError('acceptance hard stop')
            self.save('acceptance')
            self.ops.accept(left)
            if self.ops.now() >= self.end('02:30:00'):
                raise TimeoutError('acceptance passed the hard stop')
            # The successful rollback drill uses the real AI OS owner controls,
            # within this same locked controller. Interrupted runs recover only.
            self.owner_switch('drill_v41', ROLLBACK, TARGET, self.end('02:50:00'))
            left = (self.end('02:50:00') - self.ops.now()).total_seconds()
            if left <= 0:raise TimeoutError('V4.1 client validation deadline')
            self.ops.clients(left, 'drill-v41-clients')
            self.save('drill_v41_clients_verified', drill_v41_clients_verified=True)
            self.owner_switch('drill_flash', TARGET, ROLLBACK, self.end('03:30:00'))
            left = (self.end('04:15:00') - self.ops.now()).total_seconds()
            if left <= 0:
                raise TimeoutError('final acceptance deadline')
            self.save('final_acceptance')
            self.ops.accept(left, 'accept-final')
            if self.ops.now() >= self.end('04:15:00'):
                raise TimeoutError('final acceptance hard stop')
            return self.finish('activated', 'Flash acceptance, AI OS owner V4.1→Flash drill and final acceptance passed', 0)
        except Exception as exc:
            reason = f'{type(exc).__name__}: {exc}'
            try:
                if self.progress.get('activation_requested'):
                    return self.restore(reason)
                return self.finish('not_started', reason + '; activation never requested', 4)
            except Exception as failure:
                return self.finish('rollback_failed', reason + '; ' + str(failure), 6)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window', required=True, type=dt.date.fromisoformat)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    run = LOGS / (stamp + ('-dry' if args.dry_run else '-recover' if args.recover else '-real'))
    run.mkdir(mode=0o700)
    def interrupted(signum, frame):
        raise InterruptedError(f'worker interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with (STATE / 'runner.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('another runner owns the activation lock; no action', flush=True)
            return 0
        try:
            ops = Operations(run, str(args.window))
        except Exception as exc:
            atomic(run / 'result.json', {'result': 'bootstrap_failed', 'error': type(exc).__name__,
                                        'detail': str(exc), 'window': str(args.window)})
            return 4
        runner = Runner(ops, str(args.window), STATE, run)
        # Even failed dry preflight must not consume the actual window.
        if args.dry_run:
            runner.final = run / 'dry-run-result.json'
        return runner.run_window(dry=args.dry_run, recover=args.recover)


if __name__ == '__main__':
    sys.exit(main())
