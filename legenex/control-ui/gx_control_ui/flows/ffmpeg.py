"""Deterministic media composition for Creative Flows (FFmpeg).

FFmpeg runs in the local ``linuxserver/ffmpeg`` image, exactly like
``MediaTools``: ``docker run --rm --network none`` with memory/CPU limits,
as the invoking user, with every input mounted READ-ONLY as a single file
(``/in/<n>.<ext>``) and one private, writable output directory (``/out``).

Rules:
* Commands are argument arrays. Nothing is ever passed through a shell.
* Inputs are Library asset files resolved from the database; a caller never
  supplies a path.
* No user text is ever placed inside a filter graph. Caption and subtitle text
  is written to a file and referenced by a fixed path (``textfile=``,
  ``subtitles=filename=``). Filter arguments are built only from validated
  numbers and enum values.
* Every builder is a pure function (unit-tested); ``FFmpegRunner`` executes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGE = "linuxserver/ffmpeg:latest"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
MAX_SIDE = 4096
AUDIO_RATE = 48000
VIDEO_CODEC = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
AUDIO_CODEC = ["-c:a", "aac", "-b:a", "192k"]
MP4_FLAGS = ["-movflags", "+faststart"]
CRF = {"high": 18, "standard": 22, "small": 28}
_SAFE_EXT = re.compile(r"^[a-z0-9]{2,5}$")


class ComposeError(Exception):
    """A composition cannot be built or failed; the message is user-facing."""


@dataclass(frozen=True)
class Media:
    """A Library input: its file and what ffprobe knows about it."""

    path: Path
    ext: str
    kind: str  # image | video | audio
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    fps: float | None = None
    asset_id: str | None = None


@dataclass
class FFJob:
    inputs: list[Media]
    args: list[str]  # everything after "ffmpeg"; references /in/<n>.<ext> and /out/...
    outputs: dict[str, str]  # role -> file name in /out ("main" is required)
    kind: str  # result type
    files: dict[str, str] = field(default_factory=dict)  # extra text files mounted under /in/
    describe: str = ""

    def input_name(self, index: int) -> str:
        return f"/in/{index}.{self.inputs[index].ext}"


def _even(n: float) -> int:
    v = max(2, int(round(n)))
    return v - (v % 2)


def _db(value: float) -> str:
    return f"{float(value):.2f}dB"


def _sec(value: float) -> str:
    return f"{max(0.0, float(value)):.3f}"


def _aprep(index: int, label: str, extra: str = "") -> str:
    chain = f"[{index}:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"
    if extra:
        chain += f",{extra}"
    return f"{chain}[{label}]"


def _need(media: Media, what: str) -> float:
    if not media.duration or media.duration <= 0:
        raise ComposeError(f"the {what} has no known duration")
    return float(media.duration)


def _audio_outputs(base: list[str], label: str) -> tuple[list[str], dict[str, str]]:
    """WAV master plus an MP3 for streaming, from one filter graph."""
    graph_tail = f";[{label}]asplit=2[aw][am]"
    args = base[:-1] + [base[-1] + graph_tail,
                        "-map", "[aw]", "-c:a", "pcm_s16le", "-ar", str(AUDIO_RATE), "/out/out.wav",
                        "-map", "[am]", "-c:a", "libmp3lame", "-b:a", "192k", "/out/out.mp3"]
    return args, {"main": "out.wav", "mp3": "out.mp3"}


def _inputs(media: list[Media], loop: set[int] | None = None, still: set[int] | None = None) -> list[str]:
    args: list[str] = []
    for i, m in enumerate(media):
        if loop and i in loop:
            args += ["-stream_loop", "-1"]
        if still and i in still:
            args += ["-loop", "1"]
        args += ["-i", f"/in/{i}.{m.ext}"]
    return args


# ----------------------------------------------------------------- audio
def merge_audio(clips: list[Media], gap: float = 0.0) -> FFJob:
    if not 1 <= len(clips) <= 32:
        raise ComposeError("merge 1 to 32 audio clips")
    parts = []
    labels = ""
    for i in range(len(clips)):
        pad = f"apad=pad_dur={_sec(gap)}" if gap > 0 and i < len(clips) - 1 else ""
        parts.append(_aprep(i, f"a{i}", pad))
        labels += f"[a{i}]"
    graph = ";".join(parts) + f";{labels}concat=n={len(clips)}:v=0:a=1[a]"
    args, outs = _audio_outputs(["-y", *_inputs(clips), "-filter_complex", graph], "a")
    return FFJob(clips, args, outs, "audio", describe=f"merge {len(clips)} clips")


def mix_audio(tracks: list[Media], length: str = "longest", volume_db: float = -8.0) -> FFJob:
    if not 1 <= len(tracks) <= 16:
        raise ComposeError("mix 1 to 16 audio tracks")
    if length not in ("longest", "first", "shortest"):
        raise ComposeError("unknown mix length")
    parts = [_aprep(0, "a0")]
    for i in range(1, len(tracks)):
        parts.append(_aprep(i, f"a{i}", f"volume={_db(volume_db)}"))
    labels = "".join(f"[a{i}]" for i in range(len(tracks)))
    graph = ";".join(parts) + f";{labels}amix=inputs={len(tracks)}:duration={length}:normalize=0[a]"
    args, outs = _audio_outputs(["-y", *_inputs(tracks), "-filter_complex", graph], "a")
    return FFJob(tracks, args, outs, "audio", describe=f"mix {len(tracks)} tracks")


# ----------------------------------------------------- audio onto video
def _video_out(crf: int = 20) -> list[str]:
    return [*VIDEO_CODEC, "-crf", str(crf), *AUDIO_CODEC, *MP4_FLAGS]


def add_audio(video: Media, audio: Media, *, role: str, volume_db: float = 0.0, offset: float = 0.0,
              keep_original: bool = True, fit: str = "video", loop: bool = False,
              fade_out: float = 0.0) -> FFJob:
    """Voice-over, music bed or a sound effect on a video."""
    if video.kind != "video" or audio.kind != "audio":
        raise ComposeError("needs a video and an audio clip")
    vdur = _need(video, "video")
    if role not in ("voice", "music", "sfx"):
        raise ComposeError("unknown audio role")
    if fit not in ("video", "longest"):
        raise ComposeError("unknown length mode")
    delay_ms = int(round(max(0.0, offset) * 1000))
    chain = [f"volume={_db(volume_db)}"]
    if delay_ms:
        chain.append(f"adelay={delay_ms}|{delay_ms}")
    out_dur = vdur
    if role == "voice" and fit == "longest":
        adur = _need(audio, "voice clip")
        out_dur = max(vdur, offset + adur)
    if role == "music":
        chain.append(f"atrim=0:{_sec(out_dur)}")
        if fade_out > 0:
            fade = min(fade_out, out_dur)
            chain.append(f"afade=t=out:st={_sec(out_dur - fade)}:d={_sec(fade)}")
    graph = [_aprep(1, "new", ",".join(chain))]
    if keep_original and video.has_audio:
        graph.append(_aprep(0, "orig"))
        graph.append("[orig][new]amix=inputs=2:duration=longest:normalize=0[a]")
    else:
        graph.append("[new]anull[a]")
    vmap = "0:v:0"
    video_args = ["-c:v", "copy"]
    if out_dur > vdur + 0.05:
        graph.append(f"[0:v]tpad=stop_mode=clone:stop_duration={_sec(out_dur - vdur)},format=yuv420p[v]")
        vmap = "[v]"
        video_args = [*VIDEO_CODEC, "-crf", "20"]
    args = ["-y", *_inputs([video, audio], loop={1} if loop else None),
            "-filter_complex", ";".join(graph), "-map", vmap, "-map", "[a]",
            *video_args, *AUDIO_CODEC, *MP4_FLAGS, "-t", _sec(out_dur), "/out/out.mp4"]
    return FFJob([video, audio], args, {"main": "out.mp4"}, "video", describe=f"add {role} to video")


# ------------------------------------------------------------ one clip
def trim(media: Media, start: float, end: float | None) -> FFJob:
    dur = _need(media, "clip")
    if start >= dur:
        raise ComposeError(f"start {start:g} s is after the end of the clip ({dur:.1f} s)")
    stop = min(end, dur) if end is not None else dur
    if stop <= start:
        raise ComposeError("end must be after start")
    if media.kind == "video":
        args = ["-y", "-i", f"/in/0.{media.ext}", "-ss", _sec(start), "-to", _sec(stop),
                *_video_out(), "/out/out.mp4"]
        return FFJob([media], args, {"main": "out.mp4"}, "video", describe="trim video")
    graph = f"[0:a]atrim={_sec(start)}:{_sec(stop)},asetpts=PTS-STARTPTS,aresample={AUDIO_RATE}[a]"
    args, outs = _audio_outputs(["-y", "-i", f"/in/0.{media.ext}", "-filter_complex", graph], "a")
    return FFJob([media], args, outs, "audio", describe="trim audio")


def fade(media: Media, direction: str, duration: float) -> FFJob:
    if direction not in ("in", "out"):
        raise ComposeError("fade direction must be in or out")
    dur = _need(media, "clip")
    d = min(max(0.05, duration), dur)
    start = 0.0 if direction == "in" else dur - d
    afade = f"afade=t={direction}:st={_sec(start)}:d={_sec(d)}"
    if media.kind == "video":
        vf = f"fade=t={direction}:st={_sec(start)}:d={_sec(d)},format=yuv420p"
        args = ["-y", "-i", f"/in/0.{media.ext}", "-vf", vf]
        if media.has_audio:
            args += ["-af", afade]
        args += [*_video_out(), "/out/out.mp4"]
        return FFJob([media], args, {"main": "out.mp4"}, "video", describe=f"fade {direction}")
    graph = _aprep(0, "a0", afade) + ";[a0]anull[a]"
    args, outs = _audio_outputs(["-y", "-i", f"/in/0.{media.ext}", "-filter_complex", graph], "a")
    return FFJob([media], args, outs, "audio", describe=f"fade {direction}")


def _audio_filter(media: Media, af: str, what: str) -> FFJob:
    if media.kind == "video":
        if not media.has_audio:
            raise ComposeError(f"the video has no audio track to {what}")
        args = ["-y", "-i", f"/in/0.{media.ext}", "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-af", af, *AUDIO_CODEC, *MP4_FLAGS, "/out/out.mp4"]
        return FFJob([media], args, {"main": "out.mp4"}, "video", describe=what)
    graph = _aprep(0, "a0", af) + ";[a0]anull[a]"
    args, outs = _audio_outputs(["-y", "-i", f"/in/0.{media.ext}", "-filter_complex", graph], "a")
    return FFJob([media], args, outs, "audio", describe=what)


def volume(media: Media, gain_db: float) -> FFJob:
    return _audio_filter(media, f"volume={_db(gain_db)}", "change the volume")


def normalize(media: Media, target_lufs: float) -> FFJob:
    if not -30 <= target_lufs <= -9:
        raise ComposeError("target loudness must be between -30 and -9 LUFS")
    return _audio_filter(media, f"loudnorm=I={target_lufs:.1f}:TP=-1.5:LRA=11", "normalise the loudness")


def _fit_filter(width: int, height: int, fit: str) -> str:
    if fit == "contain":
        return (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    if fit == "cover":
        return (f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{height},setsar=1")
    if fit == "stretch":
        return f"scale={width}:{height}:flags=lanczos,setsar=1"
    raise ComposeError("fit must be contain, cover or stretch")


def resize(media: Media, width: int, height: int, fit: str) -> FFJob:
    if not (16 <= width <= MAX_SIDE and 16 <= height <= MAX_SIDE):
        raise ComposeError(f"size must be 16-{MAX_SIDE} px per side")
    if media.kind == "image":
        args = ["-y", "-i", f"/in/0.{media.ext}", "-vf", _fit_filter(width, height, fit), "-frames:v", "1",
                "/out/out.png"]
        return FFJob([media], args, {"main": "out.png"}, "image", describe=f"resize to {width}x{height}")
    w, h = _even(width), _even(height)
    args = ["-y", "-i", f"/in/0.{media.ext}", "-vf", _fit_filter(w, h, fit) + ",format=yuv420p",
            "-map", "0:v:0", "-map", "0:a?", *_video_out(), "/out/out.mp4"]
    return FFJob([media], args, {"main": "out.mp4"}, "video", describe=f"resize to {w}x{h}")


ASPECTS = {"9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5), "16:9": (16, 9)}


def crop_box(width: int, height: int, aspect: str, anchor: str) -> tuple[int, int, int, int]:
    """(w, h, x, y) of the largest crop with ``aspect`` inside width x height."""
    if aspect not in ASPECTS or anchor not in ("center", "start", "end"):
        raise ComposeError("unknown aspect or anchor")
    num, den = ASPECTS[aspect]
    w, h = width, int(width * den / num)
    if h > height:
        h, w = height, int(height * num / den)
    w, h = _even(w), _even(h)
    w, h = min(w, width - width % 2), min(h, height - height % 2)
    free_x, free_y = width - w, height - h
    pick = {"center": 0.5, "start": 0.0, "end": 1.0}[anchor]
    return w, h, int(free_x * pick), int(free_y * pick)


def crop(media: Media, aspect: str, anchor: str) -> FFJob:
    if not media.width or not media.height:
        raise ComposeError("the input size is unknown")
    w, h, x, y = crop_box(media.width, media.height, aspect, anchor)
    vf = f"crop={w}:{h}:{x}:{y},setsar=1"
    if media.kind == "image":
        args = ["-y", "-i", f"/in/0.{media.ext}", "-vf", vf, "-frames:v", "1", "/out/out.png"]
        return FFJob([media], args, {"main": "out.png"}, "image", describe=f"crop {aspect}")
    args = ["-y", "-i", f"/in/0.{media.ext}", "-vf", vf + ",format=yuv420p", "-map", "0:v:0", "-map", "0:a?",
            *_video_out(), "/out/out.mp4"]
    return FFJob([media], args, {"main": "out.mp4"}, "video", describe=f"crop {aspect}")


def upscale_image(media: Media, factor: int) -> FFJob:
    if factor not in (2, 3, 4):
        raise ComposeError("factor must be 2, 3 or 4")
    if not media.width or not media.height:
        raise ComposeError("the image size is unknown")
    if media.width * factor > MAX_SIDE or media.height * factor > MAX_SIDE:
        raise ComposeError(f"{media.width}x{media.height} x{factor} exceeds {MAX_SIDE} px per side")
    vf = f"scale={media.width * factor}:{media.height * factor}:flags=lanczos"
    args = ["-y", "-i", f"/in/0.{media.ext}", "-vf", vf, "-frames:v", "1", "/out/out.png"]
    return FFJob([media], args, {"main": "out.png"}, "image", describe=f"lanczos resize x{factor}")


def last_frame(video: Media) -> FFJob:
    if video.kind != "video":
        raise ComposeError("needs a video")
    args = ["-y", "-sseof", "-0.25", "-i", f"/in/0.{video.ext}", "-frames:v", "1", "-update", "1",
            "/out/out.png"]
    return FFJob([video], args, {"main": "out.png"}, "image", describe="extract the last frame")


# --------------------------------------------------------- multi-clip
def concat(clips: list[Media], size: str = "first", fps: int = 16) -> FFJob:
    if not 1 <= len(clips) <= 32:
        raise ComposeError("join 1 to 32 clips")
    if not 8 <= fps <= 60:
        raise ComposeError("frame rate must be 8-60")
    if size == "first":
        if not clips[0].width or not clips[0].height:
            raise ComposeError("the first clip's size is unknown")
        w, h = _even(clips[0].width), _even(clips[0].height)
    else:
        m = re.fullmatch(r"([0-9]{2,4})x([0-9]{2,4})", size)
        if not m:
            raise ComposeError("size must look like 1280x720")
        w, h = _even(int(m.group(1))), _even(int(m.group(2)))
    with_audio = any(c.has_audio for c in clips)
    parts, labels = [], ""
    for i, c in enumerate(clips):
        if c.kind != "video":
            raise ComposeError("only videos can be joined")
        parts.append(f"[{i}:v]{_fit_filter(w, h, 'contain')},fps={fps},format=yuv420p[v{i}]")
        labels += f"[v{i}]"
        if with_audio:
            if c.has_audio:
                parts.append(_aprep(i, f"a{i}"))
            else:
                parts.append(f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}:"
                             f"duration={_sec(_need(c, 'clip'))}[a{i}]")
            labels += f"[a{i}]"
    parts.append(f"{labels}concat=n={len(clips)}:v=1:a={1 if with_audio else 0}"
                 + ("[v][a]" if with_audio else "[v]"))
    args = ["-y", *_inputs(clips), "-filter_complex", ";".join(parts), "-map", "[v]"]
    if with_audio:
        args += ["-map", "[a]"]
    args += [*VIDEO_CODEC, "-crf", "20"]
    if with_audio:
        args += AUDIO_CODEC
    args += [*MP4_FLAGS, "/out/out.mp4"]
    return FFJob(clips, args, {"main": "out.mp4"}, "video", describe=f"join {len(clips)} clips at {w}x{h}")


POSITIONS = {
    "top-right": ("main_w-overlay_w-{m}", "{m}"), "top-left": ("{m}", "{m}"),
    "bottom-right": ("main_w-overlay_w-{m}", "main_h-overlay_h-{m}"),
    "bottom-left": ("{m}", "main_h-overlay_h-{m}"),
    "center": ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2"),
}


def _enable(start: float, end: float | None) -> str:
    if end is None:
        return f":enable='gte(t,{_sec(start)})'" if start > 0 else ""
    return f":enable='between(t,{_sec(start)},{_sec(end)})'"


def overlay(video: Media, image: Media, position: str, scale_pct: int, opacity: float,
            start: float = 0.0, end: float | None = None) -> FFJob:
    if position not in POSITIONS:
        raise ComposeError("unknown overlay position")
    if not video.width:
        raise ComposeError("the video size is unknown")
    if not 2 <= scale_pct <= 100 or not 0.05 <= opacity <= 1:
        raise ComposeError("overlay size or opacity out of range")
    ow = _even(video.width * scale_pct / 100)
    margin = max(4, int(video.width * 0.03))
    x, y = (p.format(m=margin) for p in POSITIONS[position])
    graph = (f"[1:v]scale={ow}:-2:flags=lanczos,format=rgba,colorchannelmixer=aa={opacity:.3f}[ov];"
             f"[0:v][ov]overlay=x={x}:y={y}:eof_action=repeat{_enable(start, end)},format=yuv420p[v]")
    args = ["-y", *_inputs([video, image]), "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            *_video_out(), "/out/out.mp4"]
    return FFJob([video, image], args, {"main": "out.mp4"}, "video", describe=f"overlay {position}")


def wrap_caption(text: str, width: int, font_px: int) -> str:
    chars = max(8, int(width * 0.9 / max(1.0, font_px * 0.56)))
    lines: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        lines += textwrap.wrap(para, chars) or [""]
    return "\n".join(lines[:6]).strip()


def captions(video: Media, text: str, position: str, font_pct: float, start: float = 0.0,
             end: float | None = None, box: bool = True) -> FFJob:
    if position not in ("bottom", "center", "top"):
        raise ComposeError("unknown caption position")
    if not video.width or not video.height:
        raise ComposeError("the video size is unknown")
    text = text.strip()
    if not text:
        raise ComposeError("the caption is empty")
    font_px = max(10, int(video.height * font_pct / 100))
    margin = int(video.height * 0.07)
    y = {"bottom": f"h-text_h-{margin}", "center": "(h-text_h)/2", "top": str(margin)}[position]
    boxpart = f":box=1:boxcolor=black@0.55:boxborderw={max(6, font_px // 2)}" if box else \
        ":borderw=2:bordercolor=black@0.8"
    vf = (f"drawtext=fontfile={FONT}:textfile=/in/caption.txt:expansion=none:fontcolor=white:fontsize={font_px}"
          f":line_spacing={max(2, font_px // 5)}:x=(w-text_w)/2:y={y}{boxpart}{_enable(start, end)},"
          f"format=yuv420p")
    args = ["-y", "-i", f"/in/0.{video.ext}", "-vf", vf, "-map", "0:v:0", "-map", "0:a?", *_video_out(),
            "/out/out.mp4"]
    return FFJob([video], args, {"main": "out.mp4"}, "video",
                 files={"caption.txt": wrap_caption(text, video.width, font_px)}, describe="captions")


_SRT_TIME = re.compile(r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}")


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def script_to_srt(text: str, duration: float) -> str:
    """Timed subtitles from a plain script (or the text itself if it is SRT)."""
    if _SRT_TIME.search(text):
        return text.replace("\r", "").strip() + "\n"
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    if not sentences:
        raise ComposeError("the subtitle text is empty")
    chunks: list[str] = []
    for s in sentences:
        chunks += textwrap.wrap(s, 84) or [s]
    total = sum(len(c) for c in chunks) or 1
    out, t = [], 0.0
    for i, c in enumerate(chunks, 1):
        span = duration * len(c) / total
        out.append(f"{i}\n{_ts(t)} --> {_ts(min(duration, t + span))}\n{textwrap.fill(c, 42)}\n")
        t += span
    return "\n".join(out)


def subtitles(video: Media, text: str, font_size: int) -> FFJob:
    dur = _need(video, "video")
    if not 8 <= font_size <= 72:
        raise ComposeError("font size must be 8-72")
    style = (f"FontName=DejaVu Sans,FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,"
             "BorderStyle=1,Outline=2,Shadow=0,MarginV=24")
    vf = f"subtitles=filename=/in/subs.srt:fontsdir={FONT_DIR}:force_style='{style}',format=yuv420p"
    args = ["-y", "-i", f"/in/0.{video.ext}", "-vf", vf, "-map", "0:v:0", "-map", "0:a?", *_video_out(),
            "/out/out.mp4"]
    return FFJob([video], args, {"main": "out.mp4"}, "video", files={"subs.srt": script_to_srt(text, dur)},
                 describe="burn subtitles")


EXPORT_PRESETS = {"1080x1920": (1080, 1920), "1920x1080": (1920, 1080), "1080x1080": (1080, 1080),
                  "1280x720": (1280, 720)}


def export(video: Media, audio: Media | None, preset: str, quality: str, fps: int | None) -> FFJob:
    if quality not in CRF:
        raise ComposeError("unknown quality")
    filters = []
    if preset != "source":
        if preset not in EXPORT_PRESETS:
            raise ComposeError("unknown export format")
        w, h = EXPORT_PRESETS[preset]
        filters.append(_fit_filter(w, h, "contain"))
    elif video.width and video.height and (video.width % 2 or video.height % 2):
        filters.append(f"scale={_even(video.width)}:{_even(video.height)}")
    if fps:
        if not 8 <= fps <= 60:
            raise ComposeError("frame rate must be 8-60")
        filters.append(f"fps={fps}")
    filters.append("format=yuv420p")
    media = [video] + ([audio] if audio else [])
    args = ["-y", *_inputs(media), "-vf", ",".join(filters), "-map", "0:v:0"]
    if audio is not None:
        if audio.kind != "audio":
            raise ComposeError("the audio track must be audio")
        args += ["-map", "1:a:0", "-t", _sec(_need(video, "video"))]
    else:
        args += ["-map", "0:a?"]
    args += [*VIDEO_CODEC, "-crf", str(CRF[quality]), "-profile:v", "high", *AUDIO_CODEC, *MP4_FLAGS,
             "/out/out.mp4"]
    return FFJob(media, args, {"main": "out.mp4"}, "video", describe=f"export {preset} {quality}")


# ================================================================ runner
@dataclass
class FFResult:
    files: dict[str, Path]
    workdir: Path
    seconds: float
    command: list[str]


def _probe_json(data: dict[str, Any], path: Path, kind: str, ext: str, asset_id: str | None) -> Media:
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = (data.get("format") or {}).get("duration")
    fps = None
    if video and video.get("avg_frame_rate") and "/" in str(video["avg_frame_rate"]):
        num, _, den = str(video["avg_frame_rate"]).partition("/")
        try:
            fps = float(num) / float(den) if float(den) else None
        except ValueError:
            fps = None
    try:
        duration = float(dur) if dur not in (None, "N/A") else None
    except ValueError:
        duration = None
    return Media(path=path, ext=ext, kind=kind, duration=duration,
                 width=int(video["width"]) if video and video.get("width") else None,
                 height=int(video["height"]) if video and video.get("height") else None,
                 has_audio=audio is not None, fps=fps, asset_id=asset_id)


class FFmpegRunner:
    """Runs FFJobs in the local ffmpeg image (bounded, cancellable)."""

    def __init__(self, tmp_root: Path, *, image: str = IMAGE, max_parallel: int = 2, memory: str = "2g",
                 cpus: str = "4", enabled: bool = True,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self.tmp_root = Path(tmp_root)
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.enabled = enabled
        self._popen = popen
        self._slots = threading.BoundedSemaphore(max_parallel)
        self._uid = f"{os.getuid()}:{os.getgid()}"

    def _mounts(self, job: FFJob, workdir: Path) -> list[str]:
        args: list[str] = []
        for i, m in enumerate(job.inputs):
            if not _SAFE_EXT.fullmatch(m.ext) or not m.path.is_file():
                raise ComposeError("an input file is missing from the Library")
            args += ["-v", f"{m.path.resolve()}:/in/{i}.{m.ext}:ro"]
        for name in job.files:
            if not re.fullmatch(r"[a-z]{1,16}\.(txt|srt)", name):
                raise ComposeError("invalid helper file name")
            args += ["-v", f"{(workdir / 'in' / name).resolve()}:/in/{name}:ro"]
        args += ["-v", f"{(workdir / 'out').resolve()}:/out"]
        return args

    def command(self, job: FFJob, workdir: Path, name: str, entrypoint: str = "ffmpeg") -> list[str]:
        return ["docker", "run", "--rm", "--name", name, "--network", "none", "--memory", self.memory,
                "--cpus", self.cpus, "--pids-limit", "256", "--security-opt", "no-new-privileges",
                "--cap-drop", "ALL", "--user", self._uid, *self._mounts(job, workdir),
                "--entrypoint", entrypoint, self.image, "-hide_banner", "-loglevel", "error", "-nostdin",
                *job.args]

    def run(self, job: FFJob, *, cancel: threading.Event | None = None, timeout: float = 1800,
            log: Callable[[str], None] | None = None) -> FFResult:
        if not self.enabled:
            raise ComposeError("media composition is disabled in offline mode")
        workdir = Path(tempfile.mkdtemp(prefix="flow-ff-", dir=self.tmp_root))
        (workdir / "in").mkdir()
        (workdir / "out").mkdir()
        for fname, content in job.files.items():
            (workdir / "in" / fname).write_text(content, encoding="utf-8")
        name = f"gx-flow-ffmpeg-{secrets.token_hex(6)}"
        cmd = self.command(job, workdir, name)
        acquired = False
        t0 = time.time()
        try:
            while not acquired:
                if cancel is not None and cancel.is_set():
                    raise ComposeError("cancelled")
                acquired = self._slots.acquire(timeout=1.0)
            if log:
                log(f"ffmpeg: {job.describe}")
            proc = self._popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            err = b""
            while True:
                try:
                    _, err = proc.communicate(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if (cancel is not None and cancel.is_set()) or time.time() - t0 > timeout:
                        subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
                        proc.kill()
                        proc.communicate(timeout=30)
                        raise ComposeError("cancelled" if cancel is not None and cancel.is_set()
                                           else f"FFmpeg did not finish within {int(timeout)} s") from None
            if proc.returncode != 0:
                detail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
                raise ComposeError("FFmpeg failed: " + " / ".join(detail)[:400])
            files = {}
            for role, fname in job.outputs.items():
                path = workdir / "out" / fname
                if not path.is_file() or path.stat().st_size < 64:
                    raise ComposeError(f"FFmpeg produced no {role} output")
                files[role] = path
            return FFResult(files, workdir, round(time.time() - t0, 2), cmd)
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        finally:
            if acquired:
                self._slots.release()

    def probe(self, path: Path, kind: str, ext: str, asset_id: str | None = None) -> Media:
        if not self.enabled:
            return Media(path=path, ext=ext, kind=kind, asset_id=asset_id)
        job = FFJob([Media(path=path, ext=ext, kind=kind)], [], {"main": ""}, kind)
        workdir = Path(tempfile.mkdtemp(prefix="flow-probe-", dir=self.tmp_root))
        (workdir / "out").mkdir()
        try:
            cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "512m", "--cpus", "1",
                   "--user", self._uid, *self._mounts(job, workdir), "--entrypoint", "ffprobe", self.image,
                   "-v", "error", "-show_entries",
                   "stream=codec_type,width,height,avg_frame_rate:format=duration", "-of", "json",
                   f"/in/0.{ext}"]
            res = subprocess.run(cmd, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
            if res.returncode != 0:
                raise ComposeError("ffprobe could not read an input file")
            return _probe_json(json.loads(res.stdout or b"{}"), path, kind, ext, asset_id)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
