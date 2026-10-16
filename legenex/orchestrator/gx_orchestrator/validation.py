"""Switch validation, preflight and rollback readiness against the real cluster (D-053).

Everything here talks to real things (the LiteLLM gateway, docker, sysfs,
the worker over SSH) and is injected into `switching.ModelSwitcher`; the unit
tests use fakes. Every remote command is a fixed string; the only variable
parts (container names, image refs, paths) are checked against tight regexes
first. Nothing here starts, stops or kills anything.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import resource_guard as RG
from .profiles import Registry
from .switching import Check

log = logging.getLogger("gx.validate")

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_IMAGE = re.compile(r"^[A-Za-z0-9._/:@-]{1,256}$")
_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,512}$")
_TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}]


def run_on(node: str, script: str, *, ssh_target: str, timeout: float = 30.0) -> "subprocess.CompletedProcess[str] | None":
    cmd = ["bash", "-c", script] if node == "node1" else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target, script]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


class HostFacts:
    """Read-only facts about both nodes."""

    def __init__(self, ssh_target: str, read_mem: "Callable[[str], float | None]") -> None:
        self.ssh = ssh_target
        self.read_mem = read_mem

    def container_running(self, node: str, name: str) -> "bool | None":
        """Tri-state: True / False (docker says so) / None (could not tell)."""
        if not _NAME.match(name):
            return None
        p = run_on(node, f"docker inspect -f '{{{{.State.Running}}}}' {name}", ssh_target=self.ssh)
        if p is None:
            return None
        from .lifecycle import container_state_from  # noqa: PLC0415
        return container_state_from(p.returncode, p.stdout, p.stderr, remote=node != "node1")

    def image_id(self, node: str, image: str) -> str:
        if not _IMAGE.match(image):
            return ""
        p = run_on(node, f"docker image inspect -f '{{{{.Id}}}}' '{image}'", ssh_target=self.ssh)
        return p.stdout.strip() if p and p.returncode == 0 else ""

    def safetensors_count(self, node: str, path: str) -> int:
        if not _PATH.match(path):
            return -1
        p = run_on(node, f"test -d '{path}' && ls '{path}' | grep -c '\\.safetensors$' || echo -1",
                   ssh_target=self.ssh)
        try:
            return int((p.stdout or "-1").strip().splitlines()[-1]) if p else -1
        except ValueError:
            return -1

    def rdma_tx_bytes(self, hca: str = "rocep1s0f0") -> "int | None":
        try:
            words = int(Path(f"/sys/class/infiniband/{hca}/ports/1/counters/port_xmit_data").read_text())
            return words * 4  # the counter is in 4-byte words
        except (OSError, ValueError):
            return None


def _post(url: str, body: dict, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _header(headers: dict, name: str) -> str:
    return headers.get(name) or headers.get(f"llm_provider-{name}") or ""


class GatewayValidator:
    """The post-start checks of a switch, through the real LiteLLM gateway."""

    def __init__(self, *, registry: Registry, gateway_base: str, gateway_key: "str | None",
                 facts: HostFacts, timeout: float = 600.0) -> None:
        self.registry = registry
        self.base = gateway_base.rstrip("/")
        self.key = gateway_key or ""
        self.facts = facts
        self.timeout = timeout

    def __call__(self, name: str, token: str) -> list[Check]:
        dep = self.registry.deployment(name)
        runtime = self.registry.runtime(dep.runtime)
        checks = []
        for node in ("node1", "node2"):
            state = self.facts.container_running(node, dep.containers.get(node, ""))
            checks.append(Check(f"rank_{node}", state is True,
                                f"{dep.containers.get(node, '?')}: "
                                + {True: "running", False: "not running", None: "unknown (probe error)"}[state]))
        if not self.key:
            return checks + [Check("gateway_key", False, "no GX_GATEWAY_KEY/LITELLM_MASTER_KEY in the environment")]
        # gx-fast (thinking off) on BOTH deployments: a thinking reply can spend
        # a small budget before the answer (D-053 A4). gx-high adds a tool call
        # with thinking on.
        alias = "gx-fast"
        hdr = {"Authorization": f"Bearer {self.key}", "X-GX-Switch-Validation": token,
               "X-GX-Project": "model-switch", "X-GX-Agent": "switch-validator"}
        url = f"{self.base}/chat/completions"

        # 1. completion + served identity + fabric
        tx0 = self.facts.rdma_tx_bytes()
        status, headers, raw = _post(url, {"model": alias, "temperature": 0, "max_tokens": 256,
                                           "messages": [{"role": "user", "content": "17*19="}]}, hdr, self.timeout)
        tx1 = self.facts.rdma_tx_bytes()
        content = ""
        try:
            content = json.loads(raw)["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        checks.append(Check("completion", status == 200 and "323" in content, f"HTTP {status}: {content[:80]!r}"))
        got_dep = _header(headers, "x-gx-deployment")
        got_model = _header(headers, "x-gx-served-model")
        checks.append(Check("served_identity", got_dep == name and got_model == runtime.served_model_id,
                            f"deployment={got_dep or 'missing'} served_model={got_model or 'missing'} "
                            f"(want {name} / {runtime.served_model_id})"))
        if tx0 is not None and tx1 is not None:
            checks.append(Check("fabric_rdma", tx1 > tx0, f"rocep1s0f0 tx +{tx1 - tx0} bytes during the completion"))

        # 2. streaming: incremental chunks, a terminating [DONE], no raw tags
        status, _h, raw = _post(url, {"model": alias, "stream": True, "max_tokens": 256, "temperature": 0,
                                      "messages": [{"role": "user", "content": "Count from 1 to 5, comma separated."}]},
                                hdr, self.timeout)
        text, pieces, done = _stream_text(raw)
        checks.append(Check("streaming",
                            status == 200 and pieces >= 2 and done and "1" in text and "5" in text
                            and "<think>" not in text,
                            f"HTTP {status}: {pieces} content chunks, [DONE]={done}: {text[:80]!r}"))

        # 3. structured tool call (thinking off) and 4. tool-result continuation
        msgs = [{"role": "user", "content": "What is the weather in Paris? Use the tool."}]
        ok, detail, call = self._tool_call(url, hdr, alias, msgs, 512)
        checks.append(Check("tool_call", ok, detail))
        if call:
            msgs2 = msgs + [{"role": "assistant", "content": "", "tool_calls": [call]},
                            {"role": "tool", "tool_call_id": call.get("id", "call_0"),
                             "content": json.dumps({"city": "Paris", "temp_c": 21, "sky": "clear"})}]
            status, _h, raw = _post(url, {"model": alias, "tools": _TOOLS, "max_tokens": 512, "messages": msgs2},
                                    hdr, self.timeout)
            try:
                answer = json.loads(raw)["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError, TypeError):
                answer = ""
            checks.append(Check("tool_result_continuation", status == 200 and "21" in answer,
                                f"HTTP {status}: {answer[:100]!r}"))
        else:
            checks.append(Check("tool_result_continuation", False, "skipped: no tool call to continue"))
        # 5. a tool call in a thinking mode (reasoning must not swallow it)
        ok, detail, _call = self._tool_call(url, hdr, "gx-high", msgs, 4096)
        checks.append(Check("tool_call_thinking", ok, detail))
        return checks

    def _tool_call(self, url: str, hdr: dict, alias: str, msgs: list, max_tokens: int):
        status, _h, raw = _post(url, {"model": alias, "tools": _TOOLS, "tool_choice": "required",
                                      "max_tokens": max_tokens, "messages": msgs}, hdr, self.timeout)
        try:
            choice = json.loads(raw)["choices"][0]
            call = (choice["message"].get("tool_calls") or [None])[0]
            args = json.loads(call["function"]["arguments"]) if call else {}
            ok = status == 200 and call["function"]["name"] == "get_weather" and bool(args.get("city"))
            return ok, (f"{alias}: {call['function']['name']}({call['function']['arguments']}) "
                        f"finish={choice.get('finish_reason')}"), call
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return False, f"{alias} HTTP {status}: no parsable tool call ({exc!r})", None


def _stream_text(raw: bytes) -> tuple[str, int, bool]:
    """(content, number of content-bearing chunks, saw [DONE])."""
    text, pieces, done = "", 0, False
    for line in raw.split(b"\n"):
        line = line.strip()
        if line == b"data: [DONE]":
            done = True
        elif line.startswith(b"data: {"):
            try:
                for ch in json.loads(line[6:]).get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        pieces += 1
                        text += piece
            except ValueError:
                continue
    return text, pieces, done


class HostPreflight:
    """Resource admission for the target, run after the previous model stopped."""

    def __init__(self, *, registry: Registry, facts: HostFacts, guard_dir: Path,
                 hostwatch_floor_gib: float = 0.0) -> None:
        self.registry = registry
        self.facts = facts
        self.guard_dir = Path(guard_dir)
        self.floor = hostwatch_floor_gib

    def __call__(self, name: str, token: str = "") -> list[Check]:
        dep = self.registry.deployment(name)
        runtime = self.registry.runtime(dep.runtime)
        checks = []
        for node in ("node1", "node2"):
            hold = RG.maintenance_hold(self.guard_dir, node)
            checks.append(Check(f"maintenance_{node}", hold is None,
                                f"Maintenance hold {hold}" if hold else "no Maintenance hold"))
            need = dep.boot_min_avail_gib.get(node, 0.0)
            # Memory returns gradually after a release: give it up to a minute.
            got = None
            for _ in range(12):
                got = self.facts.read_mem(node)
                if got is not None and got >= need:
                    break
                time.sleep(5)
            checks.append(Check(f"memory_{node}", got is not None and got >= need,
                                f"MemAvailable {got} GiB, boot needs {need} GiB"))
            iid = self.facts.image_id(node, runtime.image)
            checks.append(Check(f"image_{node}", bool(iid), f"{runtime.image} -> {iid or 'missing'}"))
        return checks


def rollback_readiness(registry: Registry, facts: HostFacts, history: list[dict[str, Any]],
                       active: str, active_state: str) -> dict[str, Any]:
    """Per deployment: files preserved / runtime installed / loaded / last test / last restore."""
    out: dict[str, Any] = {}
    for name, dep in registry.deployments.items():
        model = registry.model(dep.model)
        runtime = registry.runtime(dep.runtime)
        # Exact registry fact (D-053 A5): the model's `node`, mapped to rank names.
        worker = str(registry.cluster.get("worker", ""))
        weights_node = "node2" if model.node and model.node == worker else "node1"
        count = facts.safetensors_count(weights_node, model.path)
        images = {n: facts.image_id(n, runtime.image) for n in ("node1", "node2")}
        tested = [h for h in history if h.get("state") == "succeeded" and h.get("target") == name]
        restored = [h for h in history if h.get("state") == "rolled_back" and h.get("previous") == name]
        files_ok = count > 0
        runtime_ok = all(images.values())
        out[name] = {
            "role": dep.role,
            "files_preserved": files_ok,
            "weights": {"node": weights_node, "path": model.path, "safetensors": count},
            "runtime_installed": runtime_ok,
            "images": images,
            "loaded": name == active and active_state == "ready",
            "last_test": tested[-1]["ended"] if tested else dep.tracking.get("last_test"),
            "last_restore": restored[-1]["ended"] if restored else dep.tracking.get("last_restore"),
            "rollback_ready": files_ok and runtime_ok,
        }
    return out
