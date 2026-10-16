"""Gateway reconciliation owned by the existing deployment switch controller."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid

TARGETS={'dsv4-flash-primary':'dsv4','dsv41-rollback':'landing'}


class GatewayConfigurator:
    def __init__(self, repo: Path, logs: Path, timeout: float=180):
        self.repo,self.logs,self.timeout=repo,logs,timeout

    def __call__(self, deployment: str) -> None:
        if deployment not in TARGETS:raise ValueError('unknown gateway deployment')
        self.logs.mkdir(parents=True,exist_ok=True)
        log=self.logs/('model-gateway-'+uuid.uuid4().hex+'.log')
        fd=os.open(log,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as out:
            proc=subprocess.Popen(['/bin/bash',str(self.repo/'legenex/models/dsv4-flash/tools/gateway_config.sh'),TARGETS[deployment]],
                                  env={**os.environ,'GX_REPO_ROOT':str(self.repo)},stdin=subprocess.DEVNULL,
                                  stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                code=proc.wait(timeout=self.timeout)
            except BaseException:
                try:os.killpg(proc.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:pass
                # A parent may exit while a descendant ignores TERM. Always
                # terminate the remaining group before declaring the step done.
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                proc.wait(timeout=5)
                raise RuntimeError('gateway reconciliation timed out or was interrupted; private log '+str(log)) from None
        if code:raise RuntimeError(f'gateway reconciliation failed ({code}); private log {log}')


def source_snapshot(repo: Path, registry_path: Path | None = None) -> dict:
    """Capture code/registry provenance ONCE at startup, not a later checkout HEAD."""
    source=None
    try:
        source=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL,timeout=5).strip()
    except (OSError,subprocess.SubprocessError):pass
    digest=hashlib.sha256()
    paths=sorted((repo/'legenex/orchestrator/gx_orchestrator').glob('*.py'))
    registry_path=registry_path or repo/'legenex/models/registry.json'
    paths += [repo/'legenex/orchestrator/identity-policy.json',registry_path]
    try:
        for path in paths:
            name=str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
            digest.update(name.encode()+b'\0'+path.read_bytes()+b'\0')
        fingerprint=digest.hexdigest()
    except OSError:fingerprint=None
    try:registry_sha=hashlib.sha256(registry_path.read_bytes()).hexdigest()
    except OSError:registry_sha=None
    return {'source_sha':source,'startup_source_files_sha256':fingerprint,
            'configured_registry_path':str(registry_path),'startup_registry_file_sha256':registry_sha,'pid':os.getpid(),
            'captured_at':dt.datetime.now(dt.timezone.utc).isoformat()}
