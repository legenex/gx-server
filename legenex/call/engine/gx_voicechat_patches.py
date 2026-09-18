"""GB10 runtime patches for NeMo NemotronLabs VoiceChat (native PyTorch engine).

Every patch here is explicit, narrowly scoped, logged when applied, and can
be switched off with an environment variable. None of them changes the
model's weights, prompts or sampling; they change HOW the same computation
is carried out so the model fits and keeps up on a 128 GB unified-memory
GB10.

1. ``GX_VC_MMAP_LOAD`` (default on): ``safetensors.torch.load_file`` returns
   zero-copy views over a private (copy-on-write) file mapping. Upstream loads
   the 44 GB fp32 checkpoint into anonymous memory twice (LLM part, then TTS
   part) on top of the fp32 model skeleton; on unified memory that peaks far
   above what the 30 GiB reserve allows. File-backed pages are page cache and
   are reclaimable.

2. ``GX_VC_EARLY_CAST`` (default on): the first ``NemotronVoiceChat.to(cuda)``
   casts the LLM, its embedding and its heads to the compute dtype (bf16) on
   the CPU first, so the fp32 copies are never duplicated on the GPU side.
   Upstream casts the same modules right after the move; the result is
   identical (perception and TTS stay fp32, as upstream keeps them).

3. ``GX_VC_HYBRID_CACHE`` (default on): incremental decoding for the
   Nemotron-H backbone. Upstream's streaming context disables the LLM cache
   for Nemotron ("requires NemotronHHybridDynamicCache which is not yet
   supported") and re-runs the whole conversation through the 9B backbone at
   every 80 ms frame, which cannot be real time. The backbone's remote code
   (nvidia/NVIDIA-Nemotron-Nano-9B-v2 @6533e8de) has three defects that make
   its own cache unusable:
     * ``NemotronHBlock.forward`` never passes the cache to attention layers,
     * ``NemotronHModel.forward`` always starts ``cache_position`` at 0, so the
       Mamba mixer never takes its single-step path,
     * ``HybridMambaAttentionDynamicCache`` calls ``.device`` on a list and has
       no ``conv_kernel_size``.
   This patch supplies a correct hybrid cache (attention KV + Mamba conv/SSM
   state), fixes the block to hand it to attention layers and advances
   ``cache_position`` by the number of tokens already cached. Decoding is then
   one token per frame, exactly what the model computes without a cache
   (verified by ``gx_call_engine.py --selftest-cache``, which compares logits of
   the cached and uncached paths on the same inputs).

4. ``GX_VC_CPU_CONV_NO_ONEDNN`` (default on): oneDNN's aarch64 JIT miscompiles
   the depthwise conv1d in the EAR-TTS codec's ConvNeXt blocks on this CPU.
   ``DuplexEARTTS.__init__`` calls ``get_codec_silence_frame()``, which runs the
   codec encoder BEFORE the model is moved to the GPU, so that conv runs on the
   CPU and the JIT aborts the whole load with::

       bad err=15 in Xbyak::Error
       RuntimeError: illegal immediate parameter (range error)

   Disabling the oneDNN path sends those convolutions to ATen's own aarch64
   kernels instead. It costs nothing that matters: the only CPU convolutions in
   this engine are the one-off silence frame at construction time -- every
   per-call convolution runs on the GPU -- and the arithmetic is unchanged.
"""

from __future__ import annotations

import json
import logging
import mmap
import os
import struct
import sys
from typing import Any

import torch

log = logging.getLogger("gx_call.patches")

_DTYPES = {
    "F64": torch.float64, "F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16,
    "I64": torch.int64, "I32": torch.int32, "I16": torch.int16, "I8": torch.int8,
    "U8": torch.uint8, "BOOL": torch.bool,
}
APPLIED: dict[str, bool] = {}


def _enabled(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "no", "off")


# ------------------------------------------------------------ 1. mmap load --
def mmap_load_file(filename: str | os.PathLike, device: str | int = "cpu") -> dict[str, torch.Tensor]:
    """Drop-in for ``safetensors.torch.load_file`` returning zero-copy views."""
    with open(filename, "rb") as fh:
        (hlen,) = struct.unpack("<Q", fh.read(8))
        if hlen <= 0 or hlen > 100 * 1024 * 1024:
            raise ValueError(f"{filename}: implausible safetensors header length {hlen}")
        header = json.loads(fh.read(hlen))
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_COPY)
    header.pop("__metadata__", None)
    base = 8 + hlen
    out: dict[str, torch.Tensor] = {}
    for name, info in header.items():
        dtype = _DTYPES[info["dtype"]]
        start, end = info["data_offsets"]
        shape = list(info["shape"])
        itemsize = torch.empty((), dtype=dtype).element_size()
        count = (end - start) // itemsize
        if count == 0:
            out[name] = torch.empty(shape, dtype=dtype)
            continue
        tensor = torch.frombuffer(mm, dtype=dtype, count=count, offset=base + start).view(shape)
        out[name] = tensor if str(device) == "cpu" else tensor.to(device)
    return out


def _patch_mmap_load() -> None:
    import safetensors.torch as st  # noqa: PLC0415

    if getattr(st.load_file, "_gx_mmap", False):
        return
    mmap_load_file._gx_mmap = True  # type: ignore[attr-defined]
    st.load_file = mmap_load_file
    APPLIED["mmap_load"] = True
    log.info("patch: safetensors.torch.load_file -> zero-copy mmap views")


# ----------------------------------------------------------- 2. early cast --
def _patch_early_cast() -> None:
    from nemo.collections.speechlm2.models.nemotron_voicechat import NemotronVoiceChat  # noqa: PLC0415

    if getattr(NemotronVoiceChat, "_gx_early_cast", False):
        return
    original_to = NemotronVoiceChat.to
    dtype = {"bfloat16": torch.bfloat16, "float16": torch.float16}.get(
        os.environ.get("GX_VC_COMPUTE_DTYPE", "bfloat16"), torch.bfloat16)

    def to(self: Any, *args: Any, **kwargs: Any) -> Any:
        target = args[0] if args else kwargs.get("device")
        is_cuda = isinstance(target, (torch.device, str)) and str(target).startswith("cuda")
        if is_cuda and not getattr(self, "_gx_cast_done", False):
            self._gx_cast_done = True
            stt = self.stt_model
            for name in ("llm", "lm_head", "embed_tokens", "asr_head", "embed_asr_tokens", "function_head"):
                mod = getattr(stt, name, None)
                if isinstance(mod, torch.nn.Module):
                    setattr(stt, name, mod.to(dtype))
            log.info("patch: cast LLM, embeddings and heads to %s before the device move", dtype)
        return original_to(self, *args, **kwargs)

    NemotronVoiceChat.to = to
    NemotronVoiceChat._gx_early_cast = True
    APPLIED["early_cast"] = True


# -------------------------------------------------------- 3. hybrid cache --
class GxHybridCache:
    """Attention KV cache + Mamba2 conv/SSM state for a Nemotron-H backbone.

    Implements exactly the interface the backbone's mixers use:
    ``conv_states``/``ssm_states`` (per layer, updated in place by the
    single-step kernels), ``conv_kernel_size``, ``update_conv_state``,
    ``update_ssm_state``, ``update`` (attention) and ``get_seq_length``.
    ``seen_tokens`` is the number of positions already in the cache.
    """

    is_compileable = False

    def __init__(self, config: Any, batch_size: int, dtype: torch.dtype, device: torch.device) -> None:
        self.pattern = config.hybrid_override_pattern
        self.conv_kernel_size = int(config.conv_kernel)
        self.num_layers = int(config.num_hidden_layers)
        empty = torch.zeros(batch_size, 0, device=device)
        self.conv_states: list[torch.Tensor] = [empty] * self.num_layers
        self.ssm_states: list[torch.Tensor] = [empty] * self.num_layers
        self.key_cache: list[torch.Tensor | None] = [None] * self.num_layers
        self.value_cache: list[torch.Tensor | None] = [None] * self.num_layers
        self.seen_tokens = 0
        self.has_previous_state = False
        self.dtype = dtype
        self.device = device

    # attention
    def update(self, key_states: torch.Tensor, value_states: torch.Tensor, layer_idx: int,
               cache_kwargs: dict | None = None) -> tuple[torch.Tensor, torch.Tensor]:
        k, v = self.key_cache[layer_idx], self.value_cache[layer_idx]
        if k is None:
            self.key_cache[layer_idx], self.value_cache[layer_idx] = key_states, value_states
        else:
            self.key_cache[layer_idx] = torch.cat([k, key_states], dim=2)
            self.value_cache[layer_idx] = torch.cat([v, value_states], dim=2)
        return self.key_cache[layer_idx], self.value_cache[layer_idx]  # type: ignore[return-value]

    def get_seq_length(self, layer_idx: int | None = 0) -> int:
        return self.seen_tokens

    # mamba
    def update_conv_state(self, layer_idx: int, new_conv_state: torch.Tensor, cache_init: bool = False) -> torch.Tensor:
        if cache_init:
            self.conv_states[layer_idx] = new_conv_state.contiguous()
        else:
            state = self.conv_states[layer_idx].roll(shifts=-1, dims=-1)
            state[:, :, -1] = new_conv_state[:, 0, :].to(state.device)
            self.conv_states[layer_idx] = state
        return self.conv_states[layer_idx]

    def update_ssm_state(self, layer_idx: int, new_ssm_state: torch.Tensor) -> torch.Tensor:
        self.ssm_states[layer_idx] = new_ssm_state.contiguous()
        return self.ssm_states[layer_idx]

    def reorder_cache(self, beam_idx: torch.LongTensor) -> None:  # pragma: no cover - no beam search here
        raise NotImplementedError("beam search is not used by the streaming pipeline")


def backbone_module(llm: torch.nn.Module):
    return sys.modules[type(llm).__module__]


def patch_backbone(llm: torch.nn.Module) -> None:
    """Fix the remote-code backbone classes (idempotent)."""
    mod = backbone_module(llm)
    block_cls = mod.NemotronHBlock
    model_cls = type(llm)
    if not getattr(block_cls, "_gx_cache_fix", False):
        def block_forward(self, hidden_states, cache_params=None, cache_position=None, attention_mask=None):
            with torch.cuda.stream(torch.cuda.default_stream(hidden_states.device)):
                residual = hidden_states
                hidden_states = self.norm(hidden_states.to(dtype=self.norm.weight.dtype))
                if self.residual_in_fp32:
                    residual = residual.to(torch.float32)
                if self.block_type == "mamba":
                    hidden_states = self.mixer(hidden_states, cache_params=cache_params,
                                               cache_position=cache_position)
                elif self.block_type == "attention":
                    # upstream omits past_key_value here; with a cache the layer must see it
                    hidden_states = self.mixer(hidden_states, past_key_value=cache_params,
                                               cache_position=cache_position)[0]
                elif self.block_type == "mlp":
                    hidden_states = self.mixer(hidden_states)
                else:
                    raise ValueError(f"Invalid block_type: {self.block_type}")
                return residual + hidden_states

        block_cls.forward = block_forward
        block_cls._gx_cache_fix = True
        log.info("patch: NemotronHBlock passes the cache to attention layers")
    if not getattr(model_cls, "_gx_cache_position", False):
        original_forward = model_cls.forward

        def model_forward(self, *args, cache_params=None, cache_position=None, **kwargs):
            if isinstance(cache_params, GxHybridCache) and cache_position is None:
                embeds = kwargs.get("inputs_embeds")
                seq_len = embeds.shape[1] if embeds is not None else kwargs["input_ids"].shape[1]
                start = cache_params.seen_tokens
                device = embeds.device if embeds is not None else kwargs["input_ids"].device
                cache_position = torch.arange(start, start + seq_len, device=device)
                out = original_forward(self, *args, cache_params=cache_params, cache_position=cache_position,
                                       **kwargs)
                cache_params.seen_tokens = start + seq_len
                cache_params.has_previous_state = True
                return out
            return original_forward(self, *args, cache_params=cache_params, cache_position=cache_position, **kwargs)

        model_cls.forward = model_forward
        model_cls._gx_cache_position = True
        log.info("patch: NemotronHModel advances cache_position from the hybrid cache")


def new_hybrid_cache(s2s_model: Any) -> GxHybridCache:
    stt = s2s_model.model.stt_model
    llm = stt.llm
    patch_backbone(llm)
    dtype = getattr(s2s_model, "dtype", torch.bfloat16)
    return GxHybridCache(llm.config, 1, dtype, getattr(s2s_model, "device", torch.device("cuda")))


def _patch_hybrid_cache() -> None:
    from nemo.collections.speechlm2.inference.streaming.state import s2s_context_manager as cm  # noqa: PLC0415

    klass = cm.S2SContextManager
    if getattr(klass, "_gx_hybrid", False):
        return
    original_create = klass._create_context

    def _create_context(self):
        ctx = original_create(self)
        stt = getattr(self.s2s_model.model, "stt_model", None)
        if (ctx.dynamic_cache is None and stt is not None and getattr(stt, "llm", None) is not None
                and "Nemotron" in str(stt.cfg.get("pretrained_llm", ""))):
            ctx.dynamic_cache = new_hybrid_cache(self.s2s_model)
        return ctx

    klass._create_context = _create_context
    klass._gx_hybrid = True
    APPLIED["hybrid_cache"] = True
    log.info("patch: streaming contexts use GxHybridCache for the Nemotron-H backbone")


# --------------------------------------------------- 4. CPU conv on aarch64 --
def _patch_cpu_conv() -> None:
    """Route CPU convolutions away from oneDNN's broken aarch64 JIT.

    Only meaningful on aarch64; anywhere else this is a no-op so the image
    stays portable.
    """
    import platform

    if platform.machine() not in ("aarch64", "arm64"):
        APPLIED["cpu_conv_no_onednn"] = False
        return
    backend = getattr(torch.backends, "mkldnn", None)
    if backend is None or not backend.is_available():
        APPLIED["cpu_conv_no_onednn"] = False
        return
    backend.enabled = False
    APPLIED["cpu_conv_no_onednn"] = True
    log.info("patch: oneDNN disabled for CPU ops (aarch64 depthwise-conv1d JIT bug)")


def apply_all() -> dict[str, bool]:
    if _enabled("GX_VC_MMAP_LOAD"):
        _patch_mmap_load()
    if _enabled("GX_VC_EARLY_CAST"):
        _patch_early_cast()
    if _enabled("GX_VC_HYBRID_CACHE"):
        _patch_hybrid_cache()
    if _enabled("GX_VC_CPU_CONV_NO_ONEDNN"):
        _patch_cpu_conv()
    return dict(APPLIED)
