"""Read-only node and checkpoint verification; also runs over SSH on stdin."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

MODEL = Path('/srv/models/dsv4-flash-0731-abliterated/21bd923c')
MANIFEST_SHA = '882cd113394504bb4cf1eb8194b0cc4001065cf8bd4a3be0bd8f216885dac670'
REVISION = '21bd923c2574d9edcd7b914885024ce72fd5c076'
IMAGE = 'gx/dsv4-flash-vllm:0.25.1-b12x7dc6fb8f-fi0472b9b3-gx1'
IMAGE_ID = 'sha256:ce6ccb53ecc9362639da1f529e9161186e030176e6067ae4bf3503170e95fc87'
NFS_ID = 'sha256:d87e9c20e4224fde17a2354433d5a358a288a43ef4f4ded9a36da1ac2815925b'


def command(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=30).strip()


def verify_weights(root: Path = MODEL) -> dict:
    raw = (root / 'VERIFY.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA:
        raise ValueError('checkpoint verification manifest differs from reviewed pin')
    manifest = json.loads(raw)
    if (manifest['target_rev'] != REVISION or manifest['verified'] != 60
            or manifest['files'] != 60 or len(manifest['results']) != 60):
        raise ValueError('incomplete or wrong checkpoint manifest')
    names = [item['file'] for item in manifest['results']]
    if len(set(names)) != 60:
        raise ValueError('duplicate manifest paths')
    def verify(item):
        name = item['file']
        path = root / name
        if (Path(name).is_absolute() or '..' in Path(name).parts
                or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve())
                or any(parent.is_symlink() for parent in path.parents if parent != root.parent)):
            raise ValueError('unsafe or duplicate manifest path')
        st = path.stat()
        if st.st_size != item['size'] or st.st_mode & 0o022 or st.st_uid != os.getuid():
            raise ValueError(f'checkpoint size/permissions/owner mismatch: {name}')
        if item.get('sha256'):
            h = hashlib.sha256()
            expected = item['sha256']
        elif item.get('git_blob'):
            h = hashlib.sha1(b'blob ' + str(st.st_size).encode() + b'\0')
            expected = item['git_blob']
        else:
            raise ValueError(f'missing integrity hash: {name}')
        with path.open('rb') as src:
            while block := src.read(8 * 1024 * 1024):
                h.update(block)
        if h.hexdigest() != expected or path.stat().st_mtime_ns != st.st_mtime_ns:
            raise ValueError(f'checkpoint integrity failed: {name}')
    # Four bounded readers avoid a serial 160-GiB hash missing the three-minute
    # preflight lead time. Hash blocks release the GIL; memory is <=32 MiB.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify, manifest['results']))
    actual = {p.name for p in root.glob('*.safetensors')}
    if actual != {n for n in names if n.endswith('.safetensors')}:
        raise ValueError('unexpected or missing tensor file')
    return {'files_rehashed': len(names), 'revision': REVISION, 'manifest_sha256': MANIFEST_SHA}


def node_check(worker: bool) -> dict:
    if command(['uname', '-r']) != '6.17.0-1032-nvidia':
        raise ValueError('kernel differs from approved kernel')
    for image, expected in ((IMAGE, IMAGE_ID), ('dsv41-exl3-nfs:local', NFS_ID)):
        if command(['docker', 'image', 'inspect', '-f', '{{.Id}}', image]) != expected:
            raise ValueError(f'image digest mismatch: {image}')
    if 'nvidia.com/gpu=all' not in command(['nvidia-ctk', 'cdi', 'list']):
        raise ValueError('NVIDIA CDI device unavailable')
    port = Path('/sys/class/infiniband/rocep1s0f0/ports/1')
    if 'ACTIVE' not in (port / 'state').read_text():
        raise ValueError('RoCE fabric inactive')
    suffix = 'c0a8:640b' if worker else 'c0a8:640a'
    gids = [p for p in (port / 'gids').iterdir()
            if p.read_text().strip().endswith(':ffff:' + suffix)
            and (port / 'gid_attrs/types' / p.name).read_text().strip() == 'RoCE v2']
    if not gids:
        raise ValueError('no approved fabric RoCE v2 GID')
    if shutil.disk_usage('/').free < 15 * 1024**3:
        raise ValueError('root storage below 15 GiB')
    mem = {l.split(':')[0]: int(l.split()[1]) / 1024**2
           for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith(('MemAvailable:', 'MemTotal:'))}
    info = {'node': 'worker' if worker else 'head', 'memory_gib': mem,
            'memory_boot_safe': mem['MemAvailable'] - .74 * mem['MemTotal'] - 4 >= 15,
            'gids': [p.name for p in gids], 'runtime_image': IMAGE_ID, 'nfs_image': NFS_ID}
    # Check NFS dependencies without modules, privileged containers or mounts.
    command(['modinfo', 'nfsd' if worker else 'nfs'])
    if worker:
        info['weights'] = verify_weights()
    else:
        import yaml
        info['gateway_yaml_version'] = yaml.__version__
    return info


if __name__ == '__main__':
    try:
        print(json.dumps(node_check('--worker' in sys.argv)))
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)
