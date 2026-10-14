#!/usr/bin/python3
"""Fixed Landlock policy; delegates enforcement to maintained upstream Landrun."""

import os
import sys
from pathlib import Path


def command(mode):
    if mode not in {"stage", "production"}:
        raise ValueError("Only fixed stage/production instances are supported")
    release = Path(__file__).resolve().parent
    home = Path.home()
    runtime = Path("/run/user/1000/legenex-team-speech-" + mode)
    assert os.getuid() == 1000 and runtime.is_dir()
    key = home / ".config/legenex-team-speech/api-key"
    # eSpeak's native data lookup is limited to 160 bytes. The committed SHA
    # release path exceeds this; use a short alias to the SAME pinned data.
    data = release / "venv/lib/python3.12/site-packages/espeakng_loader/espeak-ng-data"
    alias = runtime / "espeak-ng-data"
    if not (data / "phontab").is_file() or len(str(alias).encode()) >= 128:
        raise RuntimeError("Pinned espeak data or bounded alias is unavailable")
    if alias.is_symlink():
        if alias.readlink() != data:
            raise RuntimeError("Unexpected espeak data alias")
    elif alias.exists():
        raise RuntimeError("Espeak alias must be a service-owned symlink")
    else:
        alias.symlink_to(data, target_is_directory=True)
    args = [
        str(release / "landrun-abi7"),
        "--log-enable-subprocesses",
    ]
    for path in [
        release,
        Path("/usr/bin/python3.12"),
        Path("/usr/lib/aarch64-linux-gnu"),
    ]:
        args += ["--rox", str(path)]
    for path in [
        "/usr/lib/python3.12",
        "/etc/ld.so.cache",
        "/proc/self",
        "/proc/cpuinfo",
        "/proc/meminfo",
        "/sys/devices/system/cpu",
        "/sys/devices/system/node",
        "/dev/urandom",
        "/dev/random",
        "/srv/models/team-speech",
        str(key),
    ]:
        args += ["--ro", path]
    args += ["--rw", str(runtime), "--rw", "/dev/null"]
    for name, value in {
        "SPEECH_MODELS": "/srv/models/team-speech",
        "SPEECH_KEY_FILE": str(key),
        "HF_HUB_OFFLINE": "1",
        "ESPEAK_DATA_PATH": str(alias),
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
        "NUMBA_CACHE_DIR": str(runtime / "numba"),
        "TMPDIR": str(runtime),
        "HOME": str(runtime),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": str(release / "venv/bin") + ":/usr/bin",
    }.items():
        args += ["--env", name + "=" + value]
    args += [
        "--",
        str(release / "venv/bin/python"),
        "-m",
        "uvicorn",
        "server:app",
        "--uds",
        str(runtime / "backend.sock"),
        "--no-access-log",
    ]
    return args


def execute(args):
    # Clear manager-imported credentials before Landrun; its child receives only
    # the explicit --env values above. No parent environment is forwarded.
    os.execve(args[0], args, {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"})


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Only fixed stage/production instances are supported")
    args = command(sys.argv[1])
    os.chdir(Path(__file__).resolve().parent)
    execute(args)


if __name__ == "__main__":
    main()
