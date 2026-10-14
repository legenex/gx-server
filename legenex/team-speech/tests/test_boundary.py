"""Independent HTTP boundary tests; never load or run an inference model."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def backend(tmp_path, monkeypatch):
    secret = tmp_path / "key"
    secret.write_text("independent-test-secret-" * 3)
    monkeypatch.setenv("SPEECH_KEY_FILE", str(secret))
    monkeypatch.setenv("SPEECH_MODELS", str(tmp_path / "models"))
    spec = importlib.util.spec_from_file_location(
        "reviewed_speech_backend", Path(__file__).parents[1] / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (
        module,
        TestClient(module.app),
        {"Authorization": "Bearer " + secret.read_text()},
    )


def test_unauthenticated_multipart_is_refused_before_parsing(backend):
    module, client, _headers = backend
    response = client.post(
        "/v1/audio/transcriptions",
        content=b"not a multipart body",
        headers={"Content-Type": "multipart/form-data; boundary=missing"},
    )
    assert response.status_code == 401
    assert module.STT is None


def test_large_declared_body_is_refused_before_parsing(backend):
    module, client, headers = backend
    response = client.post(
        "/v1/audio/transcriptions",
        content=b"",
        headers={
            **headers,
            "Content-Length": str(22 * 1024 * 1024),
            "Content-Type": "multipart/form-data; boundary=test",
        },
    )
    assert response.status_code == 413
    assert module.STT is None


def test_chunked_unknown_body_size_is_refused(backend):
    module, client, headers = backend
    response = client.post(
        "/v1/audio/speech",
        content=iter([b"{}"]),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 411
    assert module.TTS is None


def test_readiness_reports_missing_models_without_loading_them(backend):
    module, client, headers = backend
    result = client.get("/health").json()
    assert result["ok"] is True
    assert result["models_ready"] is False
    assert result["busy_seconds"] == 0
    assert module.TTS is None and module.STT is None
    assert client.get("/voices").status_code == 401
    assert client.get("/voices", headers=headers).json()["cloning_supported"] is False


def test_watchdog_targets_only_this_isolated_process(backend, monkeypatch):
    module, _client, _headers = backend
    events = []

    class Timer:
        def __init__(self, seconds, callback, args):
            events.append((seconds, callback, args))

        def start(self):
            events.append("started")

    monkeypatch.setattr(module.threading, "Timer", Timer)
    timer = module.start_deadline()
    assert timer.daemon is True
    assert events == [(300, module.os._exit, (70,)), "started"]
    assert module.BUSY_SINCE is not None
