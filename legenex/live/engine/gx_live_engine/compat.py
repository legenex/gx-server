"""Runtime shims for the GB10 torch stack.

torchaudio 2.11 (paired with the GB10-proven torch 2.14) delegates
``torchaudio.load``/``torchaudio.save`` to TorchCodec, which has no build for
this platform. MiniCPM-o's token2wav (``minicpmo-utils``) calls both when it
prepares the reference voice. Both are replaced by soundfile-based
equivalents with the same return shapes: ``load`` -> (float32 tensor
[channels, frames], sample_rate).
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import soundfile as sf
import torch
import torchaudio


def _load(uri: Any, frame_offset: int = 0, num_frames: int = -1, normalize: bool = True,
          channels_first: bool = True, format: str | None = None, buffer_size: int = 4096,  # noqa: A002
          backend: str | None = None) -> tuple[torch.Tensor, int]:
    data, rate = sf.read(uri, dtype="float32", always_2d=True, start=frame_offset,
                         frames=num_frames if num_frames and num_frames > 0 else -1)
    tensor = torch.from_numpy(np.ascontiguousarray(data.T if channels_first else data))
    return tensor, int(rate)


def _save(uri: Any, src: torch.Tensor, sample_rate: int, channels_first: bool = True,
          format: str | None = None, encoding: str | None = None, bits_per_sample: int | None = None,  # noqa: A002
          buffer_size: int = 4096, backend: str | None = None, compression: Any = None) -> None:
    arr = src.detach().to(torch.float32).cpu().numpy()
    if arr.ndim == 1:
        arr = arr[None, :]
    if channels_first:
        arr = arr.T
    fmt = (format or ("WAV" if isinstance(uri, io.IOBase) else None))
    sf.write(uri, arr, int(sample_rate), format=fmt.upper() if fmt else None, subtype="PCM_16")


def install() -> None:
    torchaudio.load = _load  # type: ignore[assignment]
    torchaudio.save = _save  # type: ignore[assignment]
