#!/usr/bin/env python3
"""Apply the DFlash2 compatibility overlay into the live vLLM site-packages.

Idempotent. Safe to run on every gx-reason start. Does not touch checkpoints.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

OVERLAY_DIR = Path(__file__).resolve().parent
MARKER = "GX_CLUSTER_DFLASH2_OVERLAY"


def _site_packages() -> Path:
    import vllm

    return Path(vllm.__file__).resolve().parent.parent


def _patch_qwen3_dflash(path: Path) -> None:
    text = path.read_text()
    if MARKER in text and "decoder_layer_cls = DFlashQwen3DecoderLayer" in text:
        print(f"overlay: {path.name} already patched", flush=True)
        return

    if "decoder_layer_cls = DFlashQwen3DecoderLayer" not in text:
        text = text.replace(
            "@support_torch_compile\nclass DFlashQwen3Model(nn.Module):\n"
            "    hf_to_vllm_mapper = WeightsMapper(",
            "@support_torch_compile\nclass DFlashQwen3Model(nn.Module):\n"
            f"    # {MARKER}: restore decoder-layer class indirection for DFlash2\n"
            "    decoder_layer_cls = DFlashQwen3DecoderLayer\n"
            "    hf_to_vllm_mapper = WeightsMapper(",
            1,
        )

    # Parent must instantiate through decoder_layer_cls, not the hardcoded class.
    old_layers = (
        "self.layers = nn.ModuleList(\n"
        "            [\n"
        "                DFlashQwen3DecoderLayer("
    )
    new_layers = (
        "self.layers = nn.ModuleList(\n"
        "            [\n"
        "                self.decoder_layer_cls("
    )
    if old_layers not in text:
        # Fallback for whitespace variants.
        text = re.sub(
            r"self\.layers = nn\.ModuleList\(\s*\[\s*"
            r"DFlashQwen3DecoderLayer\(",
            "self.layers = nn.ModuleList(\n            [\n"
            "                self.decoder_layer_cls(",
            text,
            count=1,
        )
    else:
        text = text.replace(old_layers, new_layers, 1)

    if "model_cls = DFlashQwen3Model" not in text:
        text = text.replace(
            "class DFlashQwen3ForCausalLM(Qwen3ForCausalLM):\n"
            "    def __init__(self, *, vllm_config: VllmConfig, prefix: str = \"\"):",
            "class DFlashQwen3ForCausalLM(Qwen3ForCausalLM):\n"
            f"    # {MARKER}: allow DFlash2 to override the draft model class\n"
            "    model_cls = DFlashQwen3Model\n\n"
            "    def __init__(self, *, vllm_config: VllmConfig, prefix: str = \"\"):",
            1,
        )

    old_model = (
        "self.model = DFlashQwen3Model(\n"
        "            vllm_config=vllm_config,\n"
        "            prefix=maybe_prefix(prefix, \"model\"),\n"
        "            start_layer_id=target_layer_num,\n"
        "        )"
    )
    new_model = (
        "self.model = self.model_cls(\n"
        "            vllm_config=vllm_config,\n"
        "            prefix=maybe_prefix(prefix, \"model\"),\n"
        "            start_layer_id=target_layer_num,\n"
        "        )"
    )
    if old_model not in text:
        text = re.sub(
            r"self\.model = DFlashQwen3Model\(\s*"
            r"vllm_config=vllm_config,\s*"
            r"prefix=maybe_prefix\(prefix, \"model\"\),\s*"
            r"start_layer_id=target_layer_num,\s*"
            r"\)",
            new_model,
            text,
            count=1,
        )
    else:
        text = text.replace(old_model, new_model, 1)

    if "decoder_layer_cls = DFlashQwen3DecoderLayer" not in text:
        raise SystemExit("overlay: failed to inject decoder_layer_cls into qwen3_dflash.py")
    if "self.decoder_layer_cls(" not in text:
        raise SystemExit("overlay: failed to retarget layer construction")
    if "self.model = self.model_cls(" not in text:
        raise SystemExit("overlay: failed to retarget model construction")

    path.write_text(text)
    print(f"overlay: patched {path}", flush=True)


def _install_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"overlay: installed {dst}", flush=True)


def _patch_registry(path: Path) -> None:
    text = path.read_text()
    if "DFlash2DraftModel" in text and "qwen3_dflash2" in text:
        print(f"overlay: {path.name} already has DFlash2DraftModel", flush=True)
        return
    needle = '    "DFlashDraftModel": ("qwen3_dflash", "DFlashQwen3ForCausalLM"),\n'
    if needle not in text:
        raise SystemExit("overlay: DFlashDraftModel registry entry not found")
    insert = (
        needle
        + f'    # {MARKER}\n'
        + '    "DFlash2DraftModel": ("qwen3_dflash2", "DFlash2Qwen3ForCausalLM"),\n'
    )
    path.write_text(text.replace(needle, insert, 1))
    print(f"overlay: patched registry {path}", flush=True)


def _patch_spec_init(path: Path) -> None:
    text = path.read_text()
    if "DFlash2Speculator" in text and "DFlash2DraftModel" in text:
        print(f"overlay: {path.name} already routes DFlash2", flush=True)
        return
    old = '''    if speculative_config.method == "dflash":
        from vllm.v1.worker.gpu.spec_decode.dflash.speculator import (
            DFlashSpeculator,
        )

        return DFlashSpeculator(vllm_config, device)
'''
    new = f'''    if speculative_config.method == "dflash":
        # {MARKER}: route DFlash2 draft architecture to the DFlash2 worker
        draft_archs = getattr(
            speculative_config.draft_model_config, "architectures", None
        ) or []
        if "DFlash2DraftModel" in draft_archs:
            from vllm.v1.worker.gpu.spec_decode.dflash2.speculator import (
                DFlash2Speculator,
            )

            return DFlash2Speculator(vllm_config, device)
        from vllm.v1.worker.gpu.spec_decode.dflash.speculator import (
            DFlashSpeculator,
        )

        return DFlashSpeculator(vllm_config, device)
'''
    if old not in text:
        raise SystemExit("overlay: unexpected spec_decode/__init__.py contents")
    path.write_text(text.replace(old, new, 1))
    print(f"overlay: patched speculator router {path}", flush=True)


def _verify(sp: Path) -> None:
    import importlib

    # Force reload after file writes.
    for mod in list(sys.modules):
        if "qwen3_dflash" in mod or "dflash2" in mod:
            del sys.modules[mod]

    from vllm.model_executor.models import qwen3_dflash as d1
    from vllm.model_executor.models import qwen3_dflash2 as d2

    if d1.DFlashQwen3Model.decoder_layer_cls is not d1.DFlashQwen3DecoderLayer:
        raise SystemExit("overlay verify: parent decoder_layer_cls wrong")
    if d2.DFlash2Qwen3Model.decoder_layer_cls is not d2.DFlash2Qwen3DecoderLayer:
        raise SystemExit("overlay verify: DFlash2 decoder_layer_cls wrong")
    if d2.DFlash2Qwen3ForCausalLM.model_cls is not d2.DFlash2Qwen3Model:
        raise SystemExit("overlay verify: DFlash2 model_cls wrong")

    # Prove construction path would use DFlash2 layers (class attribute only).
    print(
        "overlay verify: DFlash2Qwen3Model.decoder_layer_cls="
        f"{d2.DFlash2Qwen3Model.decoder_layer_cls.__name__}",
        flush=True,
    )
    print(
        "overlay verify: DFlashQwen3Model.decoder_layer_cls="
        f"{d1.DFlashQwen3Model.decoder_layer_cls.__name__}",
        flush=True,
    )

    from vllm.model_executor.models.registry import _SPECULATIVE_DECODING_MODELS

    entry = _SPECULATIVE_DECODING_MODELS.get("DFlash2DraftModel")
    if entry != ("qwen3_dflash2", "DFlash2Qwen3ForCausalLM"):
        raise SystemExit(f"overlay verify: bad registry entry {entry!r}")
    print("overlay verify: registry DFlash2DraftModel OK", flush=True)


def main() -> int:
    sp = _site_packages()
    vllm_root = sp / "vllm"
    print(f"overlay: site-packages={sp}", flush=True)
    print(f"overlay: vllm={os.environ.get('VLLM_VERSION', 'unknown')}", flush=True)

    try:
        import vllm

        print(f"overlay: vllm.__version__={getattr(vllm, '__version__', '?')}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"overlay: could not import vllm yet: {exc}", flush=True)

    _patch_qwen3_dflash(vllm_root / "model_executor/models/qwen3_dflash.py")
    _install_file(
        OVERLAY_DIR / "qwen3_dflash2.py",
        vllm_root / "model_executor/models/qwen3_dflash2.py",
    )
    _patch_registry(vllm_root / "model_executor/models/registry.py")

    dflash2_pkg = vllm_root / "v1/worker/gpu/spec_decode/dflash2"
    dflash2_pkg.mkdir(parents=True, exist_ok=True)
    (dflash2_pkg / "__init__.py").write_text(
        f'# {MARKER}\nfrom .speculator import DFlash2Speculator\n'
    )
    _install_file(OVERLAY_DIR / "dflash2_speculator.py", dflash2_pkg / "speculator.py")
    _patch_spec_init(vllm_root / "v1/worker/gpu/spec_decode/__init__.py")

    _verify(sp)
    print("overlay: applied successfully", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
