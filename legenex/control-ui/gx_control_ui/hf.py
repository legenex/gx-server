"""Hugging Face Hub client for the Model Manager (D-034).

Hugging Face repositories are untrusted, external data. This client:

* only ever calls the public REST API (`/api/models...`) and `resolve/<sha>`
  for small metadata files (config.json, README.md), with timeouts and size
  caps;
* never executes anything from a repository and never follows a moving
  branch: every lookup resolves the exact commit SHA and later operations
  use that SHA;
* treats model-card text as data (it is returned to the browser as plain
  text and rendered with textContent);
* uses an optional read token from a 0600 file outside Git. The token is
  never returned to the browser or written to a log.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://huggingface.co"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REV_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
MAX_JSON = 32 * 1024 * 1024
MAX_SMALL_FILE = 8 * 1024 * 1024  # some quantised configs list thousands of ignored modules

QUANT_TAGS = {
    "nvfp4": "NVFP4", "fp8": "FP8", "mxfp4": "MXFP4", "mxfp8": "MXFP8", "gguf": "GGUF", "awq": "AWQ",
    "gptq": "GPTQ", "bitsandbytes": "bitsandbytes", "4-bit": "4-bit", "8-bit": "8-bit",
    "compressed-tensors": "compressed-tensors", "exl2": "EXL2", "exl3": "EXL3", "mlx": "MLX",
}
UNCENSORED_TAGS = ("uncensored", "abliterated", "abliteration", "heretic", "nsfw", "crack", "unfiltered")


class HFError(Exception):
    """A Hugging Face failure.

    ``code`` is the machine-readable reason. It matters because "denied" has
    several very different causes that need very different human actions, and
    collapsing them into one message is what sent an earlier pass round in
    circles creating new tokens for a gate that no token can open (B-030):

    ``unauthenticated``   no token was sent, or the token is not accepted
    ``gated_not_granted`` the token IS accepted, but this account has not been
                          granted access to this repository
    ``forbidden``         some other refusal
    ``not_found`` / ``upstream``
    """

    def __init__(self, message: str, status: int = 502, code: str = "upstream",
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.http_status = http_status


def parse_ref(text: str) -> tuple[str, str | None]:
    """`org/name`, `org/name@rev`, or a huggingface.co URL -> (repo, revision)."""
    if not isinstance(text, str):
        raise HFError("enter a repository id or a Hugging Face URL", 400)
    value = text.strip()
    if len(value) > 400:
        raise HFError("reference is too long", 400)
    revision = None
    if value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        if host not in ("huggingface.co", "www.huggingface.co", "hf.co"):
            raise HFError("only huggingface.co URLs are accepted", 400)
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] in ("models",):
            parts = parts[1:]
        if len(parts) < 2 or parts[0] in ("datasets", "spaces"):
            raise HFError("the URL does not point at a model repository", 400)
        repo = f"{parts[0]}/{parts[1]}"
        if len(parts) >= 4 and parts[2] in ("tree", "blob", "resolve", "commit"):
            revision = urllib.parse.unquote(parts[3])
    else:
        repo, _, rev = value.partition("@")
        revision = rev or None
    if not REPO_RE.match(repo) or ".." in repo:
        raise HFError(f"'{repo}' is not a valid repository id (expected owner/name)", 400)
    if revision is not None and (not REV_RE.match(revision) or ".." in revision):
        raise HFError("invalid revision", 400)
    return repo, revision


class HFClient:
    def __init__(self, token_file: Path, cache_seconds: float = 600) -> None:
        self.token_file = Path(token_file)
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ token
    def token(self) -> str | None:
        try:
            st = self.token_file.stat()
        except FileNotFoundError:
            return os.environ.get("HF_TOKEN") or None
        if st.st_mode & 0o077:
            return None
        value = self.token_file.read_text(encoding="utf-8").strip()
        return value or None

    def token_state(self) -> dict:
        """What the UI shows about the stored token. Never includes the token."""
        tok = self.token()
        if not tok:
            # A file that exists but is group/world readable is refused by token().
            try:
                st = self.token_file.stat()
            except FileNotFoundError:
                return {"configured": False}
            if st.st_mode & 0o077:
                return {"configured": True, "valid": False, "code": "bad_permissions",
                        "error": f"{self.token_file} must be mode 0600 "
                                 f"(it is {oct(st.st_mode & 0o777)}); it is ignored until that is fixed"}
            return {"configured": False}
        try:
            who = self._get_json("/api/whoami-v2", auth=True, cache=False)
        except HFError as exc:
            return {"configured": True, "valid": False, "code": exc.code, "error": str(exc)}
        at = (who.get("auth") or {}).get("accessToken") or {}
        fine = at.get("fineGrained") or {}
        role = at.get("role")
        return {
            "configured": True,
            "valid": True,
            "user": who.get("name"),
            "account_type": who.get("type"),
            "type": role,
            "token_name": at.get("displayName"),
            "created_at": at.get("createdAt"),
            # The permission that governs gated repositories. `read`/`write`
            # tokens carry it implicitly; a fine-grained token must be given it.
            "gated_repos": bool(fine.get("canReadGatedRepos")) if role == "fineGrained" else role in ("read", "write"),
        }

    def set_token(self, value: str) -> dict:
        value = (value or "").strip()
        if not re.fullmatch(r"hf_[A-Za-z0-9]{20,100}", value):
            raise HFError("that does not look like a Hugging Face access token (hf_...)", 400)
        self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.token_file.parent, 0o700)
        tmp = self.token_file.with_name(".token.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        os.replace(tmp, self.token_file)
        with self._lock:
            self._cache.clear()
        state = self.token_state()
        if not state.get("valid"):
            self.token_file.unlink(missing_ok=True)
            raise HFError(f"Hugging Face rejected the token: {state.get('error')}", 400)
        return state

    def clear_token(self) -> None:
        self.token_file.unlink(missing_ok=True)
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------ transport
    def _get(self, path: str, *, auth: bool = True, limit: int = MAX_JSON, timeout: float = 30) -> bytes:
        req = urllib.request.Request(API + path, headers={"User-Agent": "gx-control-ui/model-manager"})
        tok = self.token() if auth else None
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            # Hugging Face names the reason in its own headers. Keep it: 401 and
            # 403 need opposite actions (supply a token vs. be granted access).
            hf_code = exc.headers.get("X-Error-Code") or ""
            hf_msg = (exc.headers.get("X-Error-Message") or "").strip()[:300]
            if exc.code == 401:
                raise HFError(
                    hf_msg or ("Hugging Face did not accept a token for this request"
                               if tok else "this resource needs a Hugging Face token"),
                    401, code="unauthenticated", http_status=401) from None
            if exc.code == 403:
                gated = hf_code == "GatedRepo" or "authorized list" in hf_msg or "restricted" in hf_msg
                raise HFError(
                    hf_msg or "Hugging Face refused this request for this account",
                    403, code="gated_not_granted" if gated else "forbidden", http_status=403) from None
            if exc.code == 404:
                raise HFError(hf_msg or "not found on Hugging Face", 404,
                              code="not_found", http_status=404) from None
            raise HFError(f"Hugging Face returned HTTP {exc.code}", code="upstream",
                          http_status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HFError(f"Hugging Face unreachable: {getattr(exc, 'reason', exc)}") from None
        if len(data) > limit:
            raise HFError("response too large")
        return data

    def _get_json(self, path: str, *, auth: bool = True, cache: bool = True) -> Any:
        key = f"{auth}:{path}"
        now = time.time()
        if cache:
            with self._lock:
                hit = self._cache.get(key)
                if hit and now - hit[0] < self.cache_seconds:
                    return hit[1]
        try:
            value = json.loads(self._get(path, auth=auth))
        except ValueError:
            raise HFError("Hugging Face returned invalid JSON") from None
        if cache:
            with self._lock:
                self._cache[key] = (now, value)
                if len(self._cache) > 500:
                    self._cache.pop(next(iter(self._cache)))
        return value

    # ------------------------------------------------------------ API
    def search(self, query: str, *, limit: int = 30, sort: str = "downloads") -> list[dict]:
        q = (query or "").strip()
        if not 2 <= len(q) <= 100:
            raise HFError("search text must be 2-100 characters", 400)
        if sort not in ("downloads", "likes", "lastModified", "trendingScore"):
            raise HFError("invalid sort", 400)
        params = urllib.parse.urlencode({"search": q, "limit": max(1, min(100, int(limit))), "sort": sort,
                                         "direction": "-1", "full": "false"})
        rows = self._get_json(f"/api/models?{params}")
        out = []
        for m in rows if isinstance(rows, list) else []:
            tags = m.get("tags") or []
            out.append({
                "repository": m.get("id"), "author": (m.get("id") or "/").split("/")[0],
                "downloads": m.get("downloads"), "likes": m.get("likes"),
                "task": m.get("pipeline_tag"), "library": m.get("library_name"),
                "gated": m.get("gated") or False, "last_modified": m.get("lastModified"),
                "quantization": _quant(tags, []), "uncensored": _uncensored(m.get("id") or "", tags),
                "tags": tags[:20],
            })
        return out

    def _access(self, repo: str, sha: str, files: list[dict], gated: Any) -> dict:
        """Can THIS deployment actually download this repository's files?

        Repository metadata and repository *files* are gated separately: a
        gated repo answers 200 for `/api/models/...` and 403 for
        `resolve/...` until the account has been granted access. Reading the
        metadata therefore proves nothing, which is why this probes a real
        file and reports the exact reason and the exact action (B-030).
        """
        if not gated:
            return {"ok": True, "reason": "public", "message": None, "action": None}
        state = self.token_state()
        probe = next((str(f["path"]) for f in files if str(f["path"]).endswith(".json")), None)
        if probe is None:
            return {"ok": True, "reason": "not_probed", "probed_file": None, "action": None,
                    "message": "this gated repository has no small file to probe; access is unverified"}
        try:
            self._get(f"/{repo}/resolve/{sha}/{urllib.parse.quote(probe)}", limit=MAX_SMALL_FILE)
        except HFError as exc:
            out = {"ok": False, "reason": exc.code, "probed_file": probe,
                   "http_status": exc.http_status, "message": str(exc),
                   "token_user": state.get("user") if state.get("valid") else None}
            if exc.code == "unauthenticated":
                out["action"] = ("Save a Hugging Face access token in Model Manager. "
                                 "A fine-grained token also needs the "
                                 "'Read access to contents of all public gated repos you can access' permission."
                                 if not state.get("configured") else
                                 "The stored token was not accepted. Replace it in Model Manager.")
            elif exc.code == "gated_not_granted":
                who = state.get("user") or "your Hugging Face account"
                out["action"] = (f"The token works and identifies {who}, but that account is not on this "
                                 f"repository's authorized list. Open https://huggingface.co/{repo} in a "
                                 f"browser signed in as {who} and accept the model's terms (or request "
                                 f"access). A new token cannot fix this.")
                if state.get("valid") and state.get("gated_repos") is False:
                    out["action"] = str(out["action"]) + (" The stored token also lacks the gated-repo "
                                                          "permission; grant it as well.")
            else:
                out["action"] = "Hugging Face refused the file. The message above is theirs, verbatim."
            return out
        return {"ok": True, "reason": "granted", "probed_file": probe, "action": None,
                "message": None, "token_user": state.get("user") if state.get("valid") else None}

    def info(self, ref: str) -> dict:
        repo, revision = parse_ref(ref)
        path = f"/api/models/{repo}" + (f"/revision/{urllib.parse.quote(revision, safe='')}" if revision else "")
        data = self._get_json(path + "?blobs=true")
        sha = data.get("sha")
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            raise HFError("Hugging Face did not return a commit SHA")
        files: list[dict[str, Any]] = []
        for s in data.get("siblings") or []:
            name = s.get("rfilename")
            if not isinstance(name, str):
                continue
            lfs = s.get("lfs") or {}
            files.append({"path": name, "size": s.get("size") or 0, "sha256": lfs.get("sha256")})
        total = sum(int(f["size"]) for f in files)
        tags = data.get("tags") or []
        card = data.get("cardData") or {}
        config = self._small_json(repo, sha, "config.json") if any(f["path"] == "config.json" for f in files) else None
        st = data.get("safetensors") or {}
        gguf = data.get("gguf") or {}
        classification = classify(repo, tags, files, config, card)
        gated = data.get("gated") or False
        access = self._access(repo, sha, files, gated)
        accessible = access["ok"]
        access_note = access.get("message")
        return {
            "repository": repo,
            "requested_revision": revision,
            "revision": sha,
            "url": f"{API}/{repo}",
            "author": data.get("author") or repo.split("/")[0],
            "task": data.get("pipeline_tag"),
            "library": data.get("library_name"),
            "tags": tags,
            "licence": card.get("license") or _tag_value(tags, "license:"),
            "base_model": card.get("base_model"),
            "gated": gated,
            "accessible": accessible,
            "access_note": access_note,
            "access": access,
            "private": data.get("private", False),
            "last_modified": data.get("lastModified"),
            "downloads": data.get("downloads"),
            "likes": data.get("likes"),
            "size_bytes": total,
            "file_count": len(files),
            "files": files[:400],
            "parameters": st.get("total") or gguf.get("total"),
            "parameter_dtypes": st.get("parameters"),
            "gguf": {k: gguf.get(k) for k in ("architecture", "context_length", "total") if k in gguf} or None,
            "architecture": (config or {}).get("architectures") or gguf.get("architecture"),
            "model_type": (config or {}).get("model_type"),
            "context": _context(config) or gguf.get("context_length"),
            **classification,
        }

    def _small_json(self, repo: str, sha: str, name: str) -> dict | None:
        try:
            raw = self._get(f"/{repo}/resolve/{sha}/{urllib.parse.quote(name)}", limit=MAX_SMALL_FILE)
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except (HFError, ValueError):
            return None

    def readme(self, repo: str, sha: str) -> str:
        if not REPO_RE.match(repo) or not SHA_RE.match(sha):
            raise HFError("invalid reference", 400)
        try:
            return self._get(f"/{repo}/resolve/{sha}/README.md", limit=MAX_SMALL_FILE).decode("utf-8", "replace")
        except HFError:
            return ""


# ---------------------------------------------------------------- analysis
def _tag_value(tags: list[str], prefix: str) -> str | None:
    for t in tags:
        if isinstance(t, str) and t.startswith(prefix):
            return t[len(prefix):]
    return None


def _quant(tags: list[str], files: list[dict]) -> list[str]:
    found = []
    lower = [str(t).lower() for t in tags]
    for key, label in QUANT_TAGS.items():
        if key in lower and label not in found:
            found.append(label)
    for f in files:
        name = f["path"].lower()
        if name.endswith(".gguf"):
            m = re.search(r"(iq\d_[a-z0-9_]+|q\d_k(?:_[sml])?|q\d_\d|f16|bf16|f32|mxfp4)", name)
            if m and m.group(1).upper() not in found:
                found.append(m.group(1).upper())
    return found


def _uncensored(repo: str, tags: list[str]) -> bool:
    text = (repo + " " + " ".join(map(str, tags))).lower()
    return any(t in text for t in UNCENSORED_TAGS)


def _context(config: dict | None) -> int | None:
    if not config:
        return None
    for source in (config, config.get("text_config") or {}):
        for key in ("max_position_embeddings", "max_seq_len", "n_positions", "seq_length"):
            value = source.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def classify(repo: str, tags: list[str], files: list[dict], config: dict | None, card: dict) -> dict:
    """What kind of artefact this is, and which runtime could serve it."""
    names = [f["path"] for f in files]
    lower = [n.lower() for n in names]
    has_gguf = any(n.endswith(".gguf") for n in lower)
    safetensors = [n for n in lower if n.endswith(".safetensors")]
    tagset = {str(t).lower() for t in tags}
    kind = "checkpoint"
    if "lora" in tagset or any("lora" in n for n in lower) or (card.get("base_model_relation") == "adapter"):
        kind = "lora"
    if any(n.endswith("adapter_config.json") for n in lower):
        kind = "adapter"
    if any(re.search(r"(^|/)vae/|_vae\b|vae\.safetensors", n) for n in lower) and len(safetensors) <= 2 \
            and not config:
        kind = "vae"
    if any("text_encoder" in n for n in lower) and len(safetensors) <= 2 and not config:
        kind = "text_encoder"
    if "controlnet" in tagset or any("controlnet" in n for n in lower):
        kind = "controlnet"
    if not has_gguf and not safetensors and any(n.endswith(".json") and "workflow" in n for n in lower):
        kind = "comfyui_workflow"
    if "refusal-direction" in tagset or (not has_gguf and not safetensors):
        kind = "not_a_checkpoint" if kind == "checkpoint" else kind
    if kind == "checkpoint" and safetensors and len(safetensors) <= 3 and not config and \
            any(t in tagset for t in ("comfyui", "diffusers", "text-to-image", "image-to-video")):
        kind = "comfyui_model"
    # Music models are their own task class (D-036): never a chat or image model.
    music = repo.lower().startswith("ace-step/") or bool(
        tagset & {"text-to-audio", "text-to-music", "music-generation", "music"})
    if music and kind not in ("not_a_checkpoint",):
        kind = "music_model"

    trust_remote = bool(config and (config.get("auto_map") or (config.get("text_config") or {}).get("auto_map")))
    quant = _quant(tags, files)
    q = (config or {}).get("quantization_config") or {}
    if isinstance(q, dict):
        method = q.get("quant_method") or q.get("quant_algo")
        if method and str(method).upper() not in quant:
            quant.append(str(method))
    arch = " ".join((config or {}).get("architectures") or []).lower()
    config_text = json.dumps(config) if config else ""
    moe = any(k in config_text for k in ("num_experts", "n_routed_experts", "num_local_experts"))
    vision = bool(config and ("vision_config" in config or "vision" in arch)) or \
        any(n.startswith("mmproj") or "/mmproj" in n for n in lower) or \
        "image-text-to-text" in tagset

    runtimes: list[str] = []
    aliases: list[str] = []
    if kind == "checkpoint" and has_gguf:
        runtimes.append("llama.cpp (llama-swap)")
        aliases += ["gx-mini"]
    if kind == "checkpoint" and safetensors and config:
        runtimes.append("vLLM (llama-swap)")
        aliases += ["gx-fast", "gx-reason"]
        if "deepseek" in arch or "deepseek_v4" in str((config or {}).get("model_type")):
            runtimes.append("SGLang TP=2 (gx-max)")
            aliases.append("gx-max")
    if kind in ("comfyui_model", "lora", "vae", "text_encoder", "controlnet", "comfyui_workflow"):
        runtimes.append("ComfyUI (media router)")
        aliases += ["gx-image", "gx-video"] if kind != "comfyui_workflow" else []
    warnings = []
    if kind == "music_model":
        runtimes = ["ACE-Step 1.5 (gx-music, gx10-02)"]
        aliases = ["gx-music"]
        warnings.append("Music model: it can only replace a gx-music component. It is staged on gx10-02 and "
                        "switched with the documented gx-music procedure (Docs > Model Manager); the "
                        "Model Manager never assigns it to a text or image alias.")
    if kind == "not_a_checkpoint":
        warnings.append("This repository has no model weights (for example a refusal-direction or patch "
                        "file). It cannot be served on its own.")
    if kind in ("lora", "adapter"):
        warnings.append("This is an adapter, not a complete model. It needs the exact base model it was "
                        f"trained for (card base_model: {card.get('base_model') or 'not stated'}).")
    if trust_remote:
        warnings.append("config.json declares custom code (auto_map): serving it requires "
                        "trust_remote_code. The Model Manager pins the exact revision; review the code first.")
    task = ("music-generation" if kind == "music_model" else
            "media-generation" if kind in ("comfyui_model", "lora", "vae", "text_encoder", "controlnet",
                                           "comfyui_workflow") else
            "chat" if kind in ("checkpoint", "adapter") else "none")
    return {
        "kind": kind,
        "task": task,
        "formats": [f for f, ok in (("gguf", has_gguf), ("safetensors", bool(safetensors))) if ok],
        "quantization": quant,
        "moe": moe,
        "vision": vision,
        "trust_remote_code": trust_remote,
        "uncensored": _uncensored(repo, tags),
        "runtimes": runtimes,
        "candidate_aliases": aliases,
        "warnings": warnings,
    }
