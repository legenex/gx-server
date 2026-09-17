"""MiniCPM-o 4.5 runtime for gx-live (runs inside the engine container, owns the GPU).

Built on the model's own remote code (openbmb/MiniCPM-o-4_5 @ 503e754,
Apache-2.0). ``speak()`` is an adaptation of ``MiniCPMO.streaming_generate``
from that code with three changes needed for a live assistant:

* the text of every LLM chunk is inspected **before** it is handed to the
  TTS, so a ``<tool_call>`` is never spoken (the call is collected with
  sampling switched to greedy and no forbidden punctuation);
* a cancel flag is checked between LLM chunks and audio chunks, which is
  what makes barge-in interruption fast; an interrupted round is still
  registered for the sliding context window with its real KV length;
* audio and caption deltas are delivered through a callback as soon as
  each ~1 s waveform chunk is decoded.

Measured on GB10 (probe 2, coordination/build-v3/liv.md): the system prompt
below (voice prompt + tool block + explicit rule, "v3") produced the right
tool call for 4/4 tool questions and none for a plain question.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np

log = logging.getLogger("gx_live_engine.model")

OUT_RATE = 24000
IN_RATE = 16000
MAX_IMAGE_SIDE = 1024
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.S)

VOICE_PREFIX = {"en": "Clone the voice in the provided audio prompt.",
                "zh": "模仿音频样本的音色并生成新的内容。"}
VOICE_SUFFIX = {
    "en": ("Please assist users while maintaining this voice style. Please answer the user's questions seriously "
           "and in a high quality. Please chat with the user in a highly human-like and oral style. "
           "You are GX Live, a realtime voice and vision assistant running on the user's own GX cluster. "
           "When a camera image is provided, describe only what you actually see in it."),
    "zh": ("你的任务是用这种声音模式来当一个助手。请认真、高质量地回复用户的问题。请用高自然度的方式和用户聊天。"
           "你是 GX Live，运行在用户自己的 GX 集群上的实时语音和视觉助手。"),
}
TOOL_RULE = ("You can call tools. When the user asks for the current time or date, asks you to hand a task to "
             "gx-fast, gx-reason or gx-auto, asks for a long or difficult piece of writing, code or maths, asks "
             "to search their library, or asks you to read a web page, answer with ONLY the <tool_call> block "
             "and no other words. After a <tool_response>, answer the user briefly and naturally using the result. "
             "Never call a tool that is not listed.")
ASR_PROMPT = {"en": "Please listen to the audio snippet carefully and transcribe the content.\n",
              "zh": "请仔细听这段音频片段，并将其内容逐字记录。\n"}


@dataclass
class SpeakResult:
    status: str = "completed"          # completed | interrupted | tool | failed
    text: str = ""
    tool_call: dict | None = None
    tool_error: str | None = None
    first_text_s: float | None = None
    first_audio_s: float | None = None
    audio_samples: int = 0
    tokens: int = 0
    extra: dict = field(default_factory=dict)


class ModelRuntime:
    def __init__(self, model_dir: str | Path) -> None:
        self.model_dir = Path(model_dir)
        self.model: Any = None
        self.tok: Any = None
        self.ref_audio: np.ndarray | None = None
        self.lock = threading.RLock()
        self.loaded = False
        self.load_seconds: float | None = None
        self.error: str | None = None

    # ------------------------------------------------------------ loading --
    def load(self) -> None:
        import librosa
        import torch
        from transformers import AutoModel

        from . import compat

        compat.install()
        t0 = time.time()
        torch.backends.cuda.matmul.allow_tf32 = True
        self.torch = torch
        model = AutoModel.from_pretrained(
            str(self.model_dir), trust_remote_code=True, attn_implementation="sdpa", torch_dtype=torch.bfloat16,
            init_vision=True, init_audio=True, init_tts=True, device_map="cuda")
        model.eval()
        model.init_tts()
        model.prepare_processor(None, None)
        self.model = model
        self.tok = model.processor.tokenizer
        mod = sys.modules[type(model).__module__]
        utils = sys.modules[mod.__name__.rsplit(".", 1)[0] + ".utils"]
        self._ChunkGen = utils.ChunkPrefillChunkGenerate
        self._TTSGen = utils.TTSStreamingGenerator
        self._tts_params = utils.TTSSamplingParams()
        self._clone = utils.torch_clone_recursive
        self._gen_logits = mod.gen_logits
        self.ref_audio, _ = librosa.load(str(self.model_dir / "assets" / "system_ref_audio.wav"), sr=IN_RATE,
                                         mono=True)
        self.tool_call_id = self.tok.convert_tokens_to_ids("<tool_call>")
        self.warmup()
        self.load_seconds = round(time.time() - t0, 1)
        self.loaded = True
        log.info("model ready in %.1f s (allocated %.2f GiB)", self.load_seconds, self.gpu()["allocated_gib"])

    def warmup(self) -> None:
        """One short spoken turn so the first real answer does not pay for kernel set-up."""
        silence = np.zeros(IN_RATE, dtype=np.float32)
        self.start_session("warmup", {"language": "en", "instructions": ""}, [])
        self.prefill_audio("warmup", silence, None)
        self.speak("warmup", audio_out=True, max_tokens=12, cancel=threading.Event(),
                   on_audio=lambda *_: None, on_text=lambda *_: None)
        self.model.reset_session(reset_token2wav_cache=True)
        self.torch.cuda.empty_cache()

    def gpu(self) -> dict:
        if self.model is None:
            return {"allocated_gib": 0.0, "reserved_gib": 0.0}
        t = self.torch
        return {"allocated_gib": round(t.cuda.memory_allocated() / 2**30, 2),
                "reserved_gib": round(t.cuda.memory_reserved() / 2**30, 2)}

    # ------------------------------------------------------------ prompts --
    def tool_block(self, tools: list[dict]) -> str:
        if not tools:
            return ""
        text = self.tok.apply_chat_template([{"role": "user", "content": "x"}], tools=tools, tokenize=False,
                                            add_generation_prompt=False, enable_thinking=False)
        return text.split("<|im_start|>system\n", 1)[1].split("<|im_end|>")[0]

    def system_message(self, config: dict, tools: list[dict]) -> dict:
        lang = config.get("language", "en")
        suffix = VOICE_SUFFIX.get(lang, VOICE_SUFFIX["en"])
        extra = (config.get("instructions") or "").strip()
        if extra:
            suffix += "\n\nAdditional instructions from the operator:\n" + extra
        block = self.tool_block(tools)
        if block:
            suffix += "\n\n" + block + "\n\n" + TOOL_RULE
        return {"role": "system", "content": [VOICE_PREFIX.get(lang, VOICE_PREFIX["en"]), self.ref_audio, suffix]}

    # ------------------------------------------------------------ session --
    def start_session(self, sid: str, config: dict, tools: list[dict]) -> float:
        with self.lock:
            t0 = time.time()
            m = self.model
            m.reset_session(reset_token2wav_cache=True)
            m.init_token2wav_cache(self.ref_audio)
            m.streaming_prefill(session_id=sid, msgs=[self.system_message(config, tools)], omni_mode=False,
                                is_last_chunk=True)
            return time.time() - t0

    def end_session(self) -> None:
        with self.lock:
            self.model.reset_session(reset_token2wav_cache=True)
            self.torch.cuda.empty_cache()

    @staticmethod
    def decode_jpeg(data: bytes) -> Any:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_SIDE:
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        return img

    def prefill_audio(self, sid: str, audio: np.ndarray, frame: Any) -> float:
        """Prefill one utterance in 1 s chunks (the first carries the camera frame)."""
        with self.lock:
            t0 = time.time()
            n = max(1, (len(audio) + IN_RATE - 1) // IN_RATE)
            for i in range(n):
                chunk = audio[i * IN_RATE:(i + 1) * IN_RATE]
                if len(chunk) < IN_RATE:
                    chunk = np.concatenate([chunk, np.zeros(IN_RATE - len(chunk), dtype=np.float32)])
                content = [frame, chunk] if (frame is not None and i == 0) else [chunk]
                self.model.streaming_prefill(session_id=sid, msgs=[{"role": "user", "content": content}],
                                             omni_mode=frame is not None, is_last_chunk=i == n - 1)
            return time.time() - t0

    def prefill_text(self, sid: str, text: str, frame: Any) -> float:
        with self.lock:
            t0 = time.time()
            content = [frame, text] if frame is not None else [text]
            self.model.streaming_prefill(session_id=sid, msgs=[{"role": "user", "content": content}],
                                         omni_mode=False, is_last_chunk=True)
            return time.time() - t0

    def prefill_tool_response(self, sid: str, name: str, content: str) -> float:
        body = json.dumps({"name": name, "content": content}, ensure_ascii=False)
        return self.prefill_text(sid, f"<tool_response>\n{body}\n</tool_response>", None)

    # ---------------------------------------------------------------- ASR --
    def transcribe(self, audio: np.ndarray, language: str = "en") -> str:
        """ASR through a separate chat() call; the streaming session is untouched."""
        with self.lock:
            out = self.model.chat(msgs=[{"role": "user", "content": [ASR_PROMPT.get(language, ASR_PROMPT["en"]),
                                                                      audio]}],
                                  do_sample=False, max_new_tokens=160, use_tts_template=True, generate_audio=False)
        return str(out or "").strip()

    # -------------------------------------------------------------- speak --
    def speak(self, sid: str, *, audio_out: bool, max_tokens: int, cancel: threading.Event,
              on_audio: Callable[[np.ndarray, str], None], on_text: Callable[[str], None],
              temperature: float = 0.7, length_penalty: float = 1.1) -> SpeakResult:
        """Generate one assistant turn. ``on_audio(pcm_float, caption_delta)`` is called per
        ~1 s chunk (audio mode); ``on_text(delta)`` per text chunk (text mode)."""
        torch = self.torch
        m = self.model
        tok = self.tok
        res = SpeakResult()
        t0 = time.time()
        with self.lock, torch.inference_mode():
            m.new_user_msg = True
            m.llm_generated = True
            m.llm_generate_completed = False
            m.audio_past_key_values = None
            if hasattr(m, "_streaming_generated_token_ids"):
                del m._streaming_generated_token_ids
            cache = m._ensure_dynamic_cache()
            cache_before = m._get_kv_cache_length(cache)
            round_id = m._pending_round_id
            m.init_streaming_processor()

            bos = "<|im_end|>\n<|im_start|>assistant\n" + m.think_str.replace("\\n", "\n") + "<|tts_bos|>"
            bos_ids = torch.tensor(tok.encode(bos), dtype=torch.long, device=m.device).unsqueeze(0)
            embeds = m.llm.get_input_embeddings()(bos_ids)
            speech_gen = self._ChunkGen(model=m.llm, tokenizer=tok, terminators=["<|tts_eos|>", "<|im_end|>", "</s>"])
            tool_gen = self._ChunkGen(model=m.llm, tokenizer=tok, terminators=["<|tts_eos|>", "<|im_end|>", "</s>"])
            tool_gen.forbidden_token_ids = []
            generated = torch.empty((1, 0), dtype=torch.long, device=m.device)
            text_ids: list[int] = []
            spoken_ids: list[int] = []
            tts = None
            p = self._tts_params
            if audio_out:
                warpers, processors = self._gen_logits(num_code=m.tts.config.num_audio_tokens,
                                                       repetition_penalty=p.repetition_penalty, top_p=p.top_p,
                                                       top_k=p.top_k)
                tts = self._TTSGen(model=m.tts, temperature=p.temperature,
                                   eos_token=torch.tensor([m.tts.config.num_audio_tokens - 1], dtype=torch.long,
                                                          device=m.tts.device),
                                   chunk_size=25, tts_last_turn_tokens=m.tts_last_turn_tokens,
                                   logits_processors=processors, logits_warpers=warpers)
                m.tts.audio_tokenizer.stream_cache = self._clone(m.token2wav_cache["flow_cache_base"])
                m.tts.audio_tokenizer.hift_cache_dict = self._clone(m.token2wav_cache["hift_cache_base"])
            wav_buffer: list[int] = [4218] * 3     # 0.12 s of silence tokens, as upstream
            pre_lookahead, chunk_tokens = 3, 25
            spoken_text_len = 0
            mode = "undecided"
            finished = False
            chunk_size = 10
            num_chunks = (max_tokens + chunk_size - 1) // chunk_size

            def emit_audio(tokens: list[int], last: bool) -> None:
                nonlocal spoken_text_len
                wave = m.tts.audio_tokenizer.stream(tokens, prompt_wav=None, last_chunk=last, return_waveform=True)
                wave = np.asarray(wave, dtype=np.float32).reshape(-1)
                if res.first_audio_s is None:
                    res.first_audio_s = time.time() - t0
                current = tok.decode(spoken_ids)
                safe = len(current)
                if not last:
                    while safe > 0 and current[safe - 1] == "�":
                        safe -= 1
                delta = current[spoken_text_len:safe]
                spoken_text_len = safe
                res.audio_samples += len(wave)
                on_audio(wave, delta)

            def feed_tts(ids: torch.Tensor, hidden: torch.Tensor, text_finished: bool) -> None:
                if tts.spk_emb is None:
                    tts.spk_emb = torch.empty((embeds.shape[0], 0, embeds.shape[2]), dtype=embeds.dtype,
                                              device=embeds.device)
                cond = m.tts.emb_text(ids)
                hid = m.tts.projector_semantic(hidden)
                if m.tts.config.normalize_projected_hidden:
                    hid = torch.nn.functional.normalize(hid, p=2, dim=-1)
                chunks = tts.generate_with_buffer(condition=cond + hid, text_finished=text_finished)
                for audio_chunk, _is_last in chunks:
                    if cancel.is_set():
                        return
                    wav_buffer.extend(audio_chunk.reshape(-1).tolist())
                    if len(wav_buffer) >= chunk_tokens + pre_lookahead:
                        emit_audio(wav_buffer[:chunk_tokens + pre_lookahead], False)
                        del wav_buffer[:chunk_tokens]

            try:
                for idx in range(num_chunks):
                    if cancel.is_set():
                        res.status = "interrupted"
                        break
                    first = idx == 0
                    gen = tool_gen if mode == "tool" else speech_gen
                    out = gen.chunk_generate(
                        inputs_embeds=embeds, past_key_values=m.llm_past_key_values, is_first_generate_chunk=first,
                        return_hidden_states=True, chunk_size=chunk_size + (1 if first else 0),
                        do_sample=mode != "tool", temperature=temperature, top_p=0.8, top_k=100,
                        repetition_penalty=1.02 if mode != "tool" else 1.0,
                        length_penalty=length_penalty if mode != "tool" else 1.0, all_input_ids=generated)
                    if out.chunk_token_ids is None:
                        break
                    if first:
                        ids = out.chunk_token_ids if out.finished else out.chunk_token_ids[:, :-1]
                    elif out.finished:
                        ids = torch.cat([generated[:, -1:], out.chunk_token_ids], dim=1)
                    else:
                        ids = torch.cat([generated[:, -1:], out.chunk_token_ids[:, :-1]], dim=1)
                    hidden = out.last_hidden_states
                    generated = torch.cat([generated, out.chunk_token_ids], dim=1)
                    embeds = out.current_inputs_embeds
                    m.llm_past_key_values = out.past_key_values
                    finished = bool(out.finished)
                    id_list = ids[0].tolist()
                    text_ids.extend(id_list)
                    if res.first_text_s is None:
                        res.first_text_s = time.time() - t0
                    if mode == "undecided":
                        head = tok.decode(text_ids).lstrip()
                        if self.tool_call_id in text_ids[:3] or head.startswith("<tool_call>"):
                            mode = "tool"
                        else:
                            mode = "speak"
                    if mode == "speak" and self.tool_call_id in id_list:
                        # a tool call after some spoken words: speak the words, collect the call
                        cut = id_list.index(self.tool_call_id)
                        if cut and audio_out:
                            spoken_ids.extend(id_list[:cut])
                            feed_tts(ids[:, :cut], hidden[:, :cut], True)
                        elif cut:
                            spoken_ids.extend(id_list[:cut])
                            on_text(tok.decode(id_list[:cut]))
                        mode = "tool"
                    elif mode == "speak":
                        spoken_ids.extend(id_list)
                        if audio_out:
                            feed_tts(ids, hidden, finished)
                        else:
                            on_text(tok.decode(id_list))
                    if mode == "tool" and "</tool_call>" in tok.decode(text_ids):
                        break
                    if finished:
                        if audio_out and tts is not None:
                            m.tts_last_turn_tokens = tts.tts_last_turn_tokens
                        break
                if audio_out and not cancel.is_set() and spoken_ids:
                    if tts is not None and tts._token_buffer:  # noqa: SLF001 - upstream flush, as streaming_generate
                        wav_buffer.extend(torch.cat(tts._token_buffer, dim=1).reshape(-1).tolist())  # noqa: SLF001
                        tts._token_buffer = []  # noqa: SLF001
                    emit_audio(wav_buffer, True)
                if cancel.is_set() and res.status != "interrupted":
                    res.status = "interrupted"
            except Exception:
                log.exception("generation failed")
                res.status = "failed"
            finally:
                res.tokens = len(text_ids)
                res.text = tok.decode(text_ids).replace("<|tts_eos|>", "").strip()
                m.llm_generate_completed = finished and res.status == "completed" and mode != "tool"
                if mode == "tool":
                    res.status = "tool" if res.status == "completed" else res.status
                    call_text = tok.decode(text_ids)
                    res.text = tok.decode(spoken_ids).strip()
                    res.tool_call, res.tool_error = parse_tool_call(call_text)
                m._finalize_round(round_id=round_id, cache_before=cache_before, assistant_input_ids=None)
        return res


def parse_tool_call(text: str) -> tuple[dict | None, str | None]:
    match = TOOL_CALL_RE.search(text)
    if not match:
        return None, "the model started a tool call but did not finish it"
    try:
        body = json.loads(match.group(1))
    except ValueError:
        return None, "the model produced a tool call that is not valid JSON"
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        return None, "the model produced a tool call without a name"
    args = body.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None, "the tool call arguments are not valid JSON"
    return {"name": body["name"], "arguments": args}, None
