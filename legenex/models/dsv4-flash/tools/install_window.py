"""Install a single logout-independent, source-pinned user systemd window."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

REPO = Path(__file__).resolve().parents[4]
KIT = 'legenex/models/dsv4-flash'
LIVE = Path('/home/legenex/Documents/Projects/Server/gx-cluster')


def checked(args):
    return subprocess.check_output(args, text=True, cwd=REPO).strip()


def install(window: dt.date) -> dict:
    if window != dt.date(2026, 10, 17):
        raise ValueError('only the owner-approved 2026-10-17 window is authorized')
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=2)))
    if now >= dt.datetime.combine(window, dt.time(0, 57), now.tzinfo):
        raise ValueError('preflight trigger has passed; never install a catch-up switch')
    if checked(['git', 'status', '--porcelain', '--untracked-files=all']):
        raise ValueError('install only from a clean reviewed commit')
    sha = checked(['git', 'rev-parse', 'HEAD'])
    if checked(['git', '-C', str(LIVE), 'rev-parse', 'HEAD']) != sha:
        raise ValueError('live source must be at the reviewed release SHA')
    if checked(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value']) != 'yes':
        raise ValueError('user lingering is required for logout-independent execution')
    root = Path.home() / '.local/share/legenex/dsv4f-window/releases' / sha
    unit_dir = Path.home() / '.config/systemd/user'
    service = unit_dir / 'gx-dsv4f-window.service'
    timer = unit_dir / 'gx-dsv4f-window.timer'
    if root.exists() or service.exists() or timer.exists():
        raise ValueError('existing release/unit detected; inspect it before changing any schedule')
    os.umask(0o077)
    root.mkdir(parents=True)
    unit_dir.mkdir(parents=True, exist_ok=True)
    files = checked(['git', 'ls-files', KIT]).splitlines()
    for name in files:
        src = REPO / name
        if src.is_symlink() or not src.is_file():
            raise ValueError('release may contain only regular source files')
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        dest.chmod(0o500 if src.stat().st_mode & 0o111 else 0o400)
    live_names = checked(['git', 'ls-files', KIT, 'legenex/orchestrator', 'legenex/models/registry.json',
                          'legenex/gateway/identity-hmac.sh', 'legenex/gateway/docker-compose.gateway.yml',
                          'legenex/gateway/litellm/config.dsv4-flash.yaml',
                          'legenex/gateway/litellm/config.dsv41.yaml']).splitlines()
    for name in live_names:
        if (LIVE / name).is_symlink() or (LIVE / name).read_bytes() != (REPO / name).read_bytes():
            raise ValueError('live source differs from reviewed commit: ' + name)
    if (LIVE / KIT / '.env').exists():
        raise ValueError('unreviewed V4 Flash local overrides; inspect before scheduling')
    live_sums = ''.join(hashlib.sha256((LIVE / n).read_bytes()).hexdigest() + '  ' + n + '\n' for n in live_names)
    (root / 'LIVE.SHA256SUMS').write_text(live_sums)
    receipt = {'source_sha': sha, 'window': str(window), 'installed': now.isoformat(),
               'release': str(root), 'live_source': str(LIVE), 'timer': 'gx-dsv4f-window.timer',
               'service': 'gx-dsv4f-window.service', 'trigger': '2026-10-17 00:57:00 Africa/Johannesburg'}
    (root / 'RELEASE.json').write_text(json.dumps(receipt, indent=2))
    entry = root / 'entry.sh'
    entry.write_text('#!/bin/bash\nset -euo pipefail\n'
                     f'cd "{root}"\nsha256sum --quiet -c SHA256SUMS\n'
                     # Recovery must still work if later authorized source was
                     # changed; its scripts come from this pinned release.
                     f'if [[ " $* " != *" --recover "* ]]; then test ! -e "{LIVE}/{KIT}/.env"; (cd "{LIVE}"; sha256sum --quiet -c "{root}/LIVE.SHA256SUMS"); fi\n'
                     f'exec /bin/bash "{root}/{KIT}/tools/window_run.sh" "$@"\n')
    entry.chmod(0o500)
    sums = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + str(p.relative_to(root)) + '\n'
                   for p in sorted(root.rglob('*')) if p.is_file())
    (root / 'SHA256SUMS').write_text(sums)
    for p in root.rglob('*'):
        if p.is_file():
            p.chmod(0o500 if p.stat().st_mode & 0o111 else 0o400)
    for p in sorted((p for p in root.rglob('*') if p.is_dir()), reverse=True):
        p.chmod(0o500)
    root.chmod(0o500)
    service.write_text(f'''[Unit]
Description=Reviewed V4 Flash single maintenance window ({sha[:12]})
After=network-online.target gx-orchestrator.service
Wants=network-online.target

[Service]
Type=oneshot
UMask=0077
Environment=TZ=Africa/Johannesburg
Environment=XDG_RUNTIME_DIR=%t
ExecStart={entry} --window {window}
ExecStopPost={entry} --window {window} --recover
TimeoutStartSec=13500
TimeoutStopSec=3900
KillMode=control-group
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal
SyslogIdentifier=gx-dsv4f-window
''')
    timer.write_text('''[Unit]
Description=V4 Flash preflight 2026-10-17 00:57 SAST (one approved window)

[Timer]
OnCalendar=2026-10-17 00:57:00 Africa/Johannesburg
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit=gx-dsv4f-window.service

[Install]
WantedBy=timers.target
''')
    checked(['systemd-analyze', '--user', 'verify', str(service), str(timer)])
    checked(['systemctl', '--user', 'daemon-reload'])
    checked(['systemctl', '--user', 'enable', '--now', timer.name])
    receipt['next_trigger'] = checked(['systemctl', '--user', 'show', timer.name, '-p', 'NextElapseUSecRealtime', '--value'])
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window', required=True, type=dt.date.fromisoformat)
    args = parser.parse_args()
    print(json.dumps(install(args.window), indent=2))
