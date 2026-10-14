#!/usr/bin/python3
"""Synthetic tests run inside the real Landlock + systemd staging policy."""

import ctypes
import errno
import json
import os
import signal
import socket
import sys
from pathlib import Path


def denied(operation):
    try:
        operation()
    except OSError as error:
        assert error.errno in {errno.EPERM, errno.EACCES, errno.EAFNOSUPPORT}, error
        return True
    raise AssertionError("Sandbox unexpectedly allowed an operation")


def create(path):
    try:
        path.write_text("synthetic isolation check")
    finally:
        if path.exists():
            path.unlink()


def connect_unix():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("/run/user/1000/speech-controlled-peer.sock")


release = Path(__file__).resolve().parent
runtime = Path("/run/user/1000/legenex-team-speech-stage")
target = int(sys.argv[1])  # Dedicated disposable test process, never an owner task.
libc = ctypes.CDLL(None, use_errno=True)
result = {
    "manager_env_canary_absent": "LEGENEX_SPEECH_ENV_CANARY" not in os.environ,
    "proc_environ_canary_absent": b"LEGENEX_SPEECH_ENV_CANARY="
    not in Path("/proc/self/environ").read_bytes(),
    "private_home_canary_read_denied": denied(
        lambda: (release.parent / "denied-canary").read_bytes()
    ),
    "code_write_denied": denied(lambda: create(release / "write-canary")),
    "venv_write_denied": denied(lambda: create(release / "venv/write-canary")),
    "models_write_denied": denied(
        lambda: create(Path("/srv/models/team-speech/write-canary"))
    ),
    "ipv4_stream_socket_denied": denied(
        lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ),
    "ipv4_udp_socket_denied": denied(
        lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ),
    "ipv6_udp_socket_denied": denied(
        lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    ),
    "outbound_unix_connect_denied": denied(connect_unix),
    "external_signal_denied": denied(lambda: os.kill(target, signal.SIGUSR1)),
}
assert libc.ptrace(16, target, None, None) == -1
assert ctypes.get_errno() == errno.EPERM
result["external_ptrace_denied"] = True
assert (
    len(
        Path("/home/legenex-02/.config/legenex-team-speech/api-key").read_text().strip()
    )
    >= 32
)
result["own_service_key_readable"] = True
create(runtime / "writable-canary")
result["private_runtime_writable"] = True
assert all(result.values())
result["ok"] = True
print(json.dumps(result), flush=True)
