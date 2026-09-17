"""Wan 2.2 LoRA discovery: the catalogue a caller may choose LoRAs from (D-040).

The router scans ComfyUI's video LoRA roots (mounted READ-ONLY) and describes
every ``.safetensors`` file it finds:

* ``name``: exactly the ``lora_name`` ComfyUI accepts: the path relative to
  the root it was found in, ``/``-separated (ComfyUI merges all roots into one
  flat list; the first root in ComfyUI's search order wins a name collision,
  the other file is reported as ``shadowed``).
* header facts from a BOUNDED read of the safetensors header (8-byte
  little-endian length + JSON): tensor count, key format, rank, hidden size,
  block count, the model family the key names and shapes belong to, and a
  compatibility verdict for Wan 2.2 T2V-A14B (``compatible`` /
  ``incompatible`` / ``unknown``). Tensor data is never read, and no file is
  ever written, renamed or moved.
* a noise-expert classification (``high`` / ``low`` / ``general`` /
  ``unknown``) from the folder (``high_noise/``, ``low_noise/``,
  ``general/``), the file name and the header metadata. Conflicting signals
  make it ``unknown``, never a guess.

Only names from this catalogue ever reach a workflow (see lora_chain.py), so a
caller cannot address an arbitrary file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("gx-media.loras")

#: Wan 2.2 T2V-A14B (and Wan 2.1 14B, same transformer): hidden size and depth.
WAN14_DIM = 5120
WAN14_BLOCKS = 40
WAN14_FFN = 13824
#: Wan 2.2 TI2V-5B and Wan 2.1 1.3B: the same key layout, other sizes.
WAN5_DIM = 3072
WAN1_DIM = 1536

MAX_HEADER_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000
MAX_DEPTH = 8
MAX_META_KEYS = 24
MAX_META_CHARS = 300
#: header metadata fields worth showing (anything else is dropped)
META_KEYS = (
    "ss_output_name", "ss_base_model_version", "ss_network_module", "ss_network_dim", "ss_network_alpha",
    "ss_training_comment", "ss_sd_model_name", "modelspec.title", "modelspec.description",
    "modelspec.architecture", "modelspec.author", "modelspec.license", "modelspec.trigger_phrase",
    "modelspec.tags", "modelspec.date", "base_model", "name", "description", "version", "software",
    "training_info", "trigger_words", "noise_level", "expert",
)
#: metadata fields whose text may name the noise expert
META_NOISE_KEYS = ("ss_output_name", "modelspec.title", "name", "noise_level", "expert")
NOISE_FOLDERS = {"high_noise": "high", "low_noise": "low", "general": "general"}

_BLOCK_RE = re.compile(r"(?:^|[._])blocks[._](\d+)[._]")
_WAN_KEY_RE = re.compile(r"(?:^|[._])blocks[._]\d+[._](?:self_attn|cross_attn|ffn)[._]")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass
class LoraFile:
    """One discovered file. ``public()`` is what the API returns."""

    id: str
    name: str
    root: str
    relpath: str
    host_path: str
    size: int
    mtime: float
    valid: bool = True
    error: str | None = None
    key_format: str | None = None
    tensors: int = 0
    rank: int | None = None
    hidden_dim: int | None = None
    blocks: int | None = None
    family: str = "unknown"
    compatibility: str = "unknown"
    compatibility_reason: str = ""
    noise: str = "unknown"
    noise_source: str | None = None
    noise_reason: str = ""
    pair_key: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    header_sha256: str | None = None
    comfy_visible: bool | None = None
    shadowed_by: str | None = None

    @property
    def usable(self) -> bool:
        return self.valid and self.shadowed_by is None and self.comfy_visible is not False

    def public(self) -> dict:
        folder, _, filename = self.relpath.rpartition("/")
        return {
            "id": self.id, "name": self.name, "filename": filename, "folder": folder, "root": self.root,
            "path": self.host_path, "size": self.size, "mtime": round(self.mtime, 3),
            "valid": self.valid, "error": self.error, "key_format": self.key_format, "tensors": self.tensors,
            "rank": self.rank, "hidden_dim": self.hidden_dim, "blocks": self.blocks, "family": self.family,
            "compatibility": self.compatibility, "compatibility_reason": self.compatibility_reason,
            "noise": self.noise, "noise_source": self.noise_source, "noise_reason": self.noise_reason,
            "pair_key": self.pair_key, "metadata": dict(self.metadata), "header_sha256": self.header_sha256,
            "comfy_visible": self.comfy_visible, "shadowed_by": self.shadowed_by, "usable": self.usable,
        }


@dataclass(frozen=True)
class LoraRoot:
    """A mounted LoRA root: ``label`` (video/shared), where the router reads it,
    and the host path shown to administrators."""

    label: str
    mount: Path
    host: str


def parse_roots(spec: str) -> list[LoraRoot]:
    """``label=mount=host;label=mount=host`` in ComfyUI search order."""
    roots: list[LoraRoot] = []
    for item in (s.strip() for s in spec.split(";")):
        if not item:
            continue
        parts = item.split("=")
        if len(parts) != 3 or not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", parts[0]):
            raise ValueError(f"LoRA root {item!r}: expected label=mount_path=host_path")
        if not parts[1].startswith("/") or not parts[2].startswith("/"):
            raise ValueError(f"LoRA root {item!r}: paths must be absolute")
        roots.append(LoraRoot(parts[0], Path(parts[1]), parts[2].rstrip("/")))
    if len({r.label for r in roots}) != len(roots):
        raise ValueError("LoRA root labels must be unique")
    return roots


# ------------------------------------------------------------------ headers
class HeaderError(Exception):
    pass


def read_header(path: Path, size: int) -> tuple[dict, bytes]:
    """The safetensors JSON header, read with hard bounds. Never reads tensor data."""
    if size < 16:
        raise HeaderError("file is too small to be a safetensors file")
    with path.open("rb") as fh:
        raw_len = fh.read(8)
        if len(raw_len) != 8:
            raise HeaderError("truncated safetensors length prefix")
        (length,) = struct.unpack("<Q", raw_len)
        if length < 2 or length > MAX_HEADER_BYTES:
            raise HeaderError(f"safetensors header length {length} is outside 2..{MAX_HEADER_BYTES} bytes")
        if 8 + length > size:
            raise HeaderError("safetensors header is longer than the file")
        raw = fh.read(length)
    if len(raw) != length:
        raise HeaderError("truncated safetensors header")
    if not raw.lstrip().startswith(b"{"):
        raise HeaderError("safetensors header is not a JSON object")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HeaderError(f"safetensors header is not valid JSON: {type(exc).__name__}") from None
    if not isinstance(header, dict):
        raise HeaderError("safetensors header is not a JSON object")
    data_len = size - 8 - length
    for key, info in header.items():
        if key == "__metadata__":
            continue
        if not isinstance(info, dict) or not isinstance(info.get("shape"), list) \
                or not isinstance(info.get("data_offsets"), list) or len(info["data_offsets"]) != 2:
            raise HeaderError(f"tensor entry {key[:80]!r} is malformed")
        start, end = info["data_offsets"]
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= data_len):
            raise HeaderError(f"tensor {key[:80]!r} points outside the file")
    return header, raw


def _clean_meta(meta: object) -> dict[str, str]:
    if not isinstance(meta, dict):
        return {}
    out: dict[str, str] = {}
    for key in META_KEYS:
        value = meta.get(key)
        if isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str) or not value.strip():
            continue
        text = "".join(ch for ch in value if ch.isprintable() or ch in " ")
        out[key] = text[:MAX_META_CHARS]
        if len(out) >= MAX_META_KEYS:
            break
    return out


def analyse(header: dict) -> dict:
    """Key format, sizes, model family and a Wan 2.2 A14B compatibility verdict."""
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    keys = list(tensors)
    if not keys:
        return {"tensors": 0, "family": "unknown", "compatibility": "incompatible",
                "compatibility_reason": "the file contains no tensors", "key_format": None}
    joined = " ".join(keys[:4000])
    if any(".lora_A." in k or ".lora_B." in k for k in keys):
        key_format = "peft"
    elif any(".lora_down." in k or ".lora_up." in k for k in keys):
        key_format = "kohya"
    elif any(k.endswith((".diff", ".diff_b")) for k in keys):
        key_format = "diff"
    else:
        key_format = "other"
    ranks: dict[int, int] = {}
    dims: dict[int, int] = {}
    for k, info in tensors.items():
        shape = info.get("shape") or []
        if len(shape) == 2 and all(isinstance(x, int) for x in shape):
            a, b = shape
            if (".lora_A." in k or ".lora_down." in k):
                ranks[a] = ranks.get(a, 0) + 1
                dims[b] = dims.get(b, 0) + 1
            elif (".lora_B." in k or ".lora_up." in k):
                dims[a] = dims.get(a, 0) + 1
    rank = max(ranks, key=lambda r: ranks[r]) if ranks else None
    blocks = {int(m.group(1)) for k in keys for m in [_BLOCK_RE.search(k)] if m}
    block_count = max(blocks) + 1 if blocks else None
    wan_layout = any(_WAN_KEY_RE.search(k) for k in keys)
    hidden = None
    for candidate in (WAN14_DIM, WAN5_DIM, WAN1_DIM):
        if dims.get(candidate):
            hidden = candidate
            break
    if hidden is None and dims:
        hidden = max(dims, key=lambda d: dims[d])
    info = {"tensors": len(keys), "key_format": key_format, "rank": rank, "hidden_dim": hidden,
            "blocks": block_count}
    i2v_only = any(("k_img" in k or "v_img" in k or "img_emb" in k) for k in keys)

    def verdict(family: str, compat: str, reason: str) -> dict:
        return {**info, "family": family, "compatibility": compat, "compatibility_reason": reason}

    if "transformer_blocks" in joined and ("img_mlp" in joined or "txt_mlp" in joined):
        return verdict("qwen-image", "incompatible", "Qwen-Image LoRA (transformer_blocks with img/txt MLPs)")
    if "double_blocks" in joined or "single_blocks" in joined:
        return verdict("flux", "incompatible", "FLUX-style LoRA (double/single blocks)")
    if "input_blocks" in joined or "output_blocks" in joined or "lora_te" in joined \
            or "down_blocks" in joined or "up_blocks" in joined:
        return verdict("stable-diffusion", "incompatible", "Stable Diffusion / SDXL UNet LoRA")
    if not wan_layout:
        return verdict("unknown", "unknown", "no Wan transformer block keys were recognised")
    if hidden == WAN14_DIM and (block_count is None or block_count <= WAN14_BLOCKS):
        if i2v_only:
            return verdict("wan-14b-i2v", "unknown",
                           "Wan 14B image-to-video LoRA (image cross-attention keys); text-to-video use is untested")
        return verdict("wan-14b", "compatible",
                       f"Wan 14B layout: hidden size {WAN14_DIM}, {block_count or '?'} of {WAN14_BLOCKS} blocks")
    if hidden == WAN5_DIM:
        return verdict("wan-5b", "incompatible", "Wan 2.2 TI2V-5B LoRA (hidden size 3072); needs the 14B model")
    if hidden == WAN1_DIM:
        return verdict("wan-1.3b", "incompatible", "Wan 2.1 1.3B LoRA (hidden size 1536); needs the 14B model")
    if block_count is not None and block_count > WAN14_BLOCKS:
        return verdict("unknown", "incompatible", f"{block_count} transformer blocks; Wan 14B has {WAN14_BLOCKS}")
    return verdict("wan-unknown", "unknown", f"Wan-style keys with hidden size {hidden}; could not be verified")


# ------------------------------------------------------------------ noise
def _tokens(text: str) -> list[str]:
    """Lower-case word tokens; camelCase is split (``WanHighNoise`` -> wan, high, noise)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [t for t in _TOKEN_SPLIT.split(spaced.lower()) if t]


_HIGH_TOKENS = ("high", "hn", "highnoise")
_LOW_TOKENS = ("low", "ln", "lownoise")


def _noise_in_text(text: str) -> str | None:
    """'high' / 'low' when a name-like text carries exactly one noise marker,
    'ambiguous' when it carries both, else None."""
    tokens = _tokens(text)
    high = any(t in _HIGH_TOKENS for t in tokens)
    low = any(t in _LOW_TOKENS for t in tokens)
    if high and low:
        return "ambiguous"
    if high:
        return "high"
    if low:
        return "low"
    return None


def pair_stem(stem: str) -> str:
    """The file name without its noise marker, normalised (separators, case)."""
    tokens = _tokens(stem)
    out: list[str] = []
    skip_noise = False
    for t in tokens:
        if t in _HIGH_TOKENS or t in _LOW_TOKENS:
            skip_noise = True
            continue
        if skip_noise and t == "noise":
            skip_noise = False
            continue
        skip_noise = False
        out.append(t)
    return "-".join(out)


def classify_noise(relpath: str, metadata: dict[str, str]) -> tuple[str, str | None, str, str]:
    """(noise, source, reason, pair_key) for one file."""
    parts = relpath.split("/")
    folders = [p.lower() for p in parts[:-1]]
    stem = parts[-1].rsplit(".", 1)[0]
    folder_noise = None
    for f in folders:
        if f in NOISE_FOLDERS:
            folder_noise = NOISE_FOLDERS[f]
    name_noise = _noise_in_text(stem)
    meta_noise = None
    for key in META_NOISE_KEYS:
        if key in metadata:
            found = _noise_in_text(metadata[key])
            if found:
                meta_noise = found
                break
    parent = "/".join(p for p in parts[:-1] if p.lower() not in NOISE_FOLDERS)
    pair_key = f"{parent.lower()}::{pair_stem(stem)}"
    signals = {k: v for k, v in (("folder", folder_noise), ("filename", name_noise), ("metadata", meta_noise))
               if v is not None}
    if "ambiguous" in signals.values():
        where = next(k for k, v in signals.items() if v == "ambiguous")
        return "unknown", None, f"the {where} names both high and low noise", pair_key
    distinct = set(signals.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{k} says {v}" for k, v in signals.items())
        return "unknown", None, f"conflicting noise markers ({detail})", pair_key
    if not signals:
        return "unknown", None, "no high/low noise marker in folder, file name or metadata", pair_key
    noise = distinct.pop()
    source = next(iter(signals))
    reason = {"high": "high-noise expert", "low": "low-noise expert",
              "general": "general LoRA (works on either expert)"}[noise]
    return noise, source, f"{reason} ({', '.join(signals)})", pair_key


# ------------------------------------------------------------------ catalogue
def file_id(root: str, relpath: str) -> str:
    return hashlib.sha256(f"{root}:{relpath}".encode()).hexdigest()[:16]


class LoraCatalog:
    """Thread-safe, rescannable catalogue. Header facts are cached by
    (path, size, mtime), so a rescan only reads new or changed files."""

    def __init__(self, roots: list[LoraRoot]) -> None:
        self.roots = roots
        self._lock = threading.Lock()
        self._files: dict[str, LoraFile] = {}
        self._by_name: dict[str, LoraFile] = {}
        self._cache: dict[tuple[str, int, float], dict] = {}
        self.scanned_at: float | None = None
        self.comfy_checked_at: float | None = None
        self.comfy_error: str | None = None
        self.loader_available: bool | None = None
        self.problems: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.roots)

    def _walk(self, root: LoraRoot, problems: list[str]) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        base = root.mount
        if not base.is_dir():
            problems.append(f"LoRA root {root.label} ({root.host}) is not mounted")
            return found
        real_roots = [r.mount.resolve() for r in self.roots]
        for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
            rel_dir = os.path.relpath(dirpath, base)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and depth < MAX_DEPTH)
            real_dir = Path(dirpath).resolve()
            if not any(real_dir == r or r in real_dir.parents for r in real_roots):
                problems.append(f"{root.label}/{rel_dir}: symlinked outside the approved LoRA roots; skipped")
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                if name.startswith(".") or not name.lower().endswith(".safetensors"):
                    continue
                rel = name if rel_dir == "." else f"{rel_dir.replace(os.sep, '/')}/{name}"
                found.append((rel, Path(dirpath) / name))
                if len(found) >= MAX_FILES:
                    problems.append(f"more than {MAX_FILES} LoRA files under {root.label}; the rest are ignored")
                    return found
        return found

    def _describe(self, root: LoraRoot, rel: str, path: Path) -> LoraFile:
        entry = LoraFile(id=file_id(root.label, rel), name=rel, root=root.label, relpath=rel,
                         host_path=f"{root.host}/{rel}", size=0, mtime=0.0)
        try:
            real = path.resolve(strict=True)
            if not any(real == r.mount.resolve() or r.mount.resolve() in real.parents for r in self.roots):
                raise HeaderError("symlink points outside the approved LoRA roots")
            st = real.stat()
            entry.size, entry.mtime = st.st_size, st.st_mtime
            cache_key = (str(real), st.st_size, st.st_mtime)
            facts = self._cache.get(cache_key)
            if facts is None:
                header, raw = read_header(real, st.st_size)
                facts = {"analysis": analyse(header), "metadata": _clean_meta(header.get("__metadata__")),
                         "header_sha256": hashlib.sha256(raw).hexdigest()}
                self._cache[cache_key] = facts
            analysis = facts["analysis"]
            entry.key_format = analysis.get("key_format")
            entry.tensors = int(analysis.get("tensors") or 0)
            entry.rank = analysis.get("rank")
            entry.hidden_dim = analysis.get("hidden_dim")
            entry.blocks = analysis.get("blocks")
            entry.family = analysis["family"]
            entry.compatibility = analysis["compatibility"]
            entry.compatibility_reason = analysis["compatibility_reason"]
            entry.metadata = dict(facts["metadata"])
            entry.header_sha256 = facts["header_sha256"]
        except (OSError, HeaderError) as exc:
            entry.valid = False
            entry.error = str(exc) if isinstance(exc, HeaderError) else f"cannot read the file: {exc.strerror}"
            entry.compatibility = "incompatible"
            entry.compatibility_reason = "not a readable safetensors file"
        entry.noise, entry.noise_source, entry.noise_reason, entry.pair_key = classify_noise(rel, entry.metadata)
        return entry

    def rescan(self, comfy_names: list[str] | None = None, comfy_error: str | None = None) -> dict:
        """Walk every root again; ``comfy_names`` is ComfyUI's current lora list
        (None when ComfyUI could not be asked)."""
        started = time.monotonic()
        problems: list[str] = []
        files: dict[str, LoraFile] = {}
        by_name: dict[str, LoraFile] = {}
        live_keys: set[tuple[str, int, float]] = set()
        for root in self.roots:
            for rel, path in self._walk(root, problems):
                entry = self._describe(root, rel, path)
                try:
                    st = path.resolve().stat()
                    live_keys.add((str(path.resolve()), st.st_size, st.st_mtime))
                except OSError:
                    pass
                if entry.name in by_name:
                    entry.shadowed_by = by_name[entry.name].id
                else:
                    by_name[entry.name] = entry
                files[entry.id] = entry
        if comfy_names is not None:
            visible = set(comfy_names)
            for entry in files.values():
                entry.comfy_visible = entry.name in visible
        with self._lock:
            self._files = files
            self._by_name = by_name
            self._cache = {k: v for k, v in self._cache.items() if k in live_keys}
            self.scanned_at = time.time()
            self.problems = problems
            if comfy_names is not None or comfy_error is not None:
                self.comfy_checked_at = time.time()
                self.comfy_error = comfy_error
        log.info("LoRA rescan: %d file(s) in %.2fs%s", len(files), time.monotonic() - started,
                 f"; {len(problems)} problem(s)" if problems else "")
        return self.public()

    def get(self, name: str) -> LoraFile | None:
        with self._lock:
            return self._by_name.get(name)

    def files(self) -> list[LoraFile]:
        with self._lock:
            return sorted(self._files.values(), key=lambda f: (f.root, f.relpath))

    def public(self) -> dict:
        with self._lock:
            files = sorted(self._files.values(), key=lambda f: (f.root, f.relpath))
            return {
                "object": "list",
                "data": [f.public() for f in files],
                "roots": [{"label": r.label, "path": r.host} for r in self.roots],
                "scanned_at": self.scanned_at,
                "comfy": {"checked_at": self.comfy_checked_at, "error": self.comfy_error,
                          "lora_loader_available": self.loader_available},
                "problems": list(self.problems),
                "limits": {"max_files": MAX_FILES, "max_header_bytes": MAX_HEADER_BYTES},
            }
