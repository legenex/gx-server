"""The Creative Flows node catalogue (D-040, FLO).

One registry for the backend (validation, execution, authorisation) and the
React canvas (palette, node cards, inspector, typed handles). The browser
fetches it from ``GET /api/flows/catalog``; nothing about node types is
hard-coded in the frontend.

Port types::

    text   plain text (prompts, scripts, lyrics)
    json   structured data (objects / lists)
    image  a Library image asset
    video  a Library video asset
    audio  a Library audio asset
    voice  a saved gx-voice voice
    lora   a Wan 2.2 LoRA preset reference (WAN)
    any    utility pass-through; resolves to the type connected to the
           node's first ``any`` input (``same_as``)

An edge is valid only when the source port's resolved type is in the target
port's ``types``. There are no implicit conversions: text -> json needs the
Structured Output node, json -> text needs Select.

A node type is ``available`` only when a real backend path exists. Nodes
without one stay in the catalogue with ``available: false`` and the reason,
so the canvas can show them honestly; the engine refuses to run them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PORT_TYPES = ("text", "json", "image", "video", "audio", "voice", "lora")
MEDIA_TYPES = ("image", "video", "audio")
CATEGORIES = (
    ("text", "Text & AI"), ("image", "Image"), ("video", "Video"), ("voice", "Voice"), ("music", "Music"),
    ("sound", "Sound"), ("compose", "Composition"), ("utility", "Utility"),
)
LLM_MODELS = (("gx-auto", "gx-auto (routed)"), ("gx-fast", "gx-fast"), ("gx-reason", "gx-reason"),
              ("gx-mini", "gx-mini"), ("gx-max", "gx-max (only when already running)"))
IMAGE_SIZES = (("1328x1328", "Square 1:1 (1328)"), ("1024x1024", "Square 1:1 (1024)"),
               ("1664x928", "Landscape 16:9"), ("928x1664", "Portrait 9:16"),
               ("1328x800", "Landscape 5:3"), ("800x1328", "Portrait 3:5"), ("768x768", "Small 1:1"))
VIDEO_SIZES = (("832x480", "Landscape 16:9 (832x480)"), ("480x832", "Portrait 9:16 (480x832)"),
               ("640x640", "Square (640)"), ("704x704", "Square (704)"), ("512x512", "Small square"))
#: ACE-Step vocal languages (ISO codes).
LANGUAGES = (("en", "English"), ("de", "German"), ("fr", "French"), ("es", "Spanish"),
             ("it", "Italian"), ("pt", "Portuguese"), ("zh", "Chinese"), ("ja", "Japanese"), ("ko", "Korean"),
             ("ru", "Russian"))
#: gx-voice (Qwen3-TTS) language names, as VOI's job body expects them.
VOICE_LANGUAGES = (("auto", "Auto"), ("english", "English"), ("german", "German"), ("french", "French"),
                   ("spanish", "Spanish"), ("italian", "Italian"), ("portuguese", "Portuguese"),
                   ("chinese", "Chinese"), ("japanese", "Japanese"), ("korean", "Korean"), ("russian", "Russian"))
SECRET_NAME = r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$"
HEADER_NAME = r"^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$"


@dataclass(frozen=True)
class Port:
    id: str
    label: str
    types: tuple[str, ...]
    required: bool = False
    multiple: bool = False
    same_as: str | None = None  # outputs only: resolve "any" from this input

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["types"] = list(self.types)
        return d


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    kind: str  # text | textarea | select | number | boolean | asset | seed | keyvalue | tags | headers
    default: Any = None
    required: bool = False
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()
    source: str | None = None  # dynamic options: image_models | lora_presets | voices
    min: float | None = None
    max: float | None = None
    step: float | None = None
    integer: bool = False
    max_length: int | None = None
    asset_type: str | None = None
    pattern: str | None = None
    placeholder: str = ""
    card: bool = False  # shown on the node card (the rest lives in the Inspector)
    fills: str | None = None  # this field is used when the named input is not connected

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["options"] = [{"value": v, "label": lab} for v, lab in self.options]
        return {k: v for k, v in d.items() if v not in (None, "", False) or k in ("default", "required")}


@dataclass(frozen=True)
class NodeType:
    type: str
    label: str
    category: str
    description: str
    service: str  # llm | image | video | voice | music | ffmpeg | http | local
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    fields: tuple[Field, ...] = ()
    version: int = 1
    aliases: tuple[str, ...] = ()  # gateway aliases a run of this node uses (API key authorisation)
    cacheable: bool = True
    available: bool = True
    unavailable_reason: str = ""
    output_node: bool = False
    backend: str = ""  # what really executes this node (shown in the Inspector)
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def public(self) -> dict[str, Any]:
        return {
            "type": self.type, "label": self.label, "category": self.category, "description": self.description,
            "service": self.service, "version": self.version, "aliases": list(self.aliases),
            "inputs": [p.public() for p in self.inputs], "outputs": [p.public() for p in self.outputs],
            "fields": [f.public() for f in self.fields], "cacheable": self.cacheable,
            "available": self.available, "unavailable_reason": self.unavailable_reason,
            "output_node": self.output_node, "backend": self.backend, "keywords": list(self.keywords),
        }

    def input(self, port_id: str) -> Port | None:
        return next((p for p in self.inputs if p.id == port_id), None)

    def output(self, port_id: str) -> Port | None:
        return next((p for p in self.outputs if p.id == port_id), None)

    def field(self, field_id: str) -> Field | None:
        return next((f for f in self.fields if f.id == field_id), None)


# ----------------------------------------------------------------- helpers
def _in(pid: str, label: str, *types: str, required: bool = False, multiple: bool = False) -> Port:
    return Port(pid, label, tuple(types), required, multiple)


def _out(pid: str, label: str, typ: str, same_as: str | None = None) -> Port:
    return Port(pid, label, (typ,), same_as=same_as)


def _prompt(label: str = "Prompt", *, card: bool = True, fills: str | None = "prompt", required: bool = False,
            placeholder: str = "", max_length: int = 4000) -> Field:
    return Field("prompt", label, "textarea", "", required=required, card=card, fills=fills,
                 max_length=max_length, placeholder=placeholder,
                 help="Used when nothing is connected to the prompt input. {{name}} inserts a flow variable.")


def _num(fid: str, label: str, default: float | None, lo: float, hi: float, *, step: float = 1,
         integer: bool = False, card: bool = False, help: str = "") -> Field:  # noqa: A002
    return Field(fid, label, "number", default, min=lo, max=hi, step=step, integer=integer, card=card, help=help)


def _select(fid: str, label: str, options: tuple[tuple[str, str], ...], default: str | None, *,
            card: bool = False, source: str | None = None, help: str = "", required: bool = False) -> Field:  # noqa: A002
    return Field(fid, label, "select", default, options=options, card=card, source=source, help=help,
                 required=required)


def _text(fid: str, label: str, default: str = "", *, card: bool = False, max_length: int = 200,
          help: str = "", placeholder: str = "", pattern: str | None = None, required: bool = False) -> Field:  # noqa: A002
    return Field(fid, label, "text", default, card=card, max_length=max_length, help=help,
                 placeholder=placeholder, pattern=pattern, required=required)


def _area(fid: str, label: str, default: str = "", *, card: bool = False, max_length: int = 8000,
          help: str = "", placeholder: str = "", fills: str | None = None, required: bool = False) -> Field:  # noqa: A002
    return Field(fid, label, "textarea", default, card=card, max_length=max_length, help=help,
                 placeholder=placeholder, fills=fills, required=required)


def _bool(fid: str, label: str, default: bool, *, card: bool = False, help: str = "") -> Field:  # noqa: A002
    return Field(fid, label, "boolean", default, card=card, help=help)


def _asset(asset_type: str, label: str = "Library asset") -> Field:
    return Field("asset_id", label, "asset", None, required=True, asset_type=asset_type, card=True,
                 pattern=r"^a_[0-9a-f]{24}$", help="Pick an item from the Library (or upload one).")


SEED = Field("seed", "Seed", "seed", None, min=0, max=2**53 - 1, integer=True,
             help="Empty = random. A fixed seed plus unchanged settings reproduces the result.")
TITLE = _text("title", "Title", help="Library title for the result (optional).")
LLM = _select("model", "Model", LLM_MODELS, "gx-auto", card=True,
              help="Runs through the LiteLLM gateway on the cluster. gx-max is used only if it is already running.")
MEDIA_IO = ("video", "audio")
VISUAL_IO = ("image", "video")
FADE_DURATION = _num("duration", "Fade length (s)", 1.0, 0.1, 30, step=0.1, card=True)


def _llm_node(t: str, label: str, desc: str, inputs: tuple[Port, ...], outputs: tuple[Port, ...],
              fields: tuple[Field, ...], keywords: tuple[str, ...] = ()) -> NodeType:
    return NodeType(t, label, "text", desc, "llm", inputs, outputs, (LLM, *fields),
                    aliases=("gx-auto",), backend="LiteLLM gateway (gx-auto / gx-fast / gx-reason / gx-mini)",
                    keywords=keywords)


# ----------------------------------------------------------------- catalogue
_NODES: list[NodeType] = [
    # ============================================================ TEXT / AI
    NodeType("text.input", "Text", "text", "A block of text you type (a brief, a script, lyrics).", "local",
             outputs=(_out("text", "Text", "text"),),
             fields=(_area("text", "Text", card=True, max_length=20000, required=True),),
             backend="stored in the flow", keywords=("note", "brief")),
    NodeType("text.prompt", "Prompt", "text",
             "A prompt template. {{name}} inserts a flow variable and {{input}} the connected text.", "local",
             inputs=(_in("context", "Context", "text", multiple=True),),
             outputs=(_out("text", "Prompt", "text"),),
             fields=(_area("template", "Prompt", card=True, max_length=8000, required=True,
                           placeholder="Cinematic photo of {{subject}}, golden hour"),),
             backend="template rendering on gx10-01", keywords=("image prompt", "template")),
    _llm_node("ai.llm", "LLM Prompt", "Ask a text model. Connected text is appended to the instruction.",
              (_in("input", "Input", "text", multiple=True),), (_out("text", "Answer", "text"),),
              (_area("instruction", "Instruction", card=True, max_length=8000, required=True),
               _area("system", "System prompt", max_length=4000),
               _num("temperature", "Temperature", 0.7, 0, 2, step=0.05),
               _num("max_tokens", "Max tokens", 1024, 16, 8192, integer=True)),
              ("chat", "gpt", "ask")),
    _llm_node("ai.script_writer", "Script Writer",
              "Writes a timed script: narration for the voice and one visual prompt per scene.",
              (_in("brief", "Brief", "text", multiple=True),),
              (_out("narration", "Narration", "text"), _out("visuals", "Scene visuals", "text"),
               _out("script", "Script (JSON)", "json")),
              (_area("brief", "Brief", card=True, max_length=4000, fills="brief",
                     placeholder="30-second ad for ..."),
               _select("format", "Format", (("video_ad", "Video ad"), ("voiceover", "Voiceover"),
                                            ("social", "Social post"), ("music_video", "Music video")), "video_ad"),
               _num("duration", "Duration (s)", 30, 5, 300, integer=True, card=True),
               _num("scenes", "Scenes", 1, 1, 8, integer=True,
                    help="One visual prompt per scene; downstream image nodes run once per scene."),
               _text("tone", "Tone", "trustworthy, warm", max_length=120),
               _text("audience", "Audience", max_length=160),
               _text("cta", "Call to action", max_length=200)),
              ("copy", "narration", "ad")),
    _llm_node("ai.scene_prompts", "Scene Prompts",
              "Turns a brief, lyrics or a script into N visual scene prompts (one item per scene).",
              (_in("source", "Source", "text", required=True, multiple=True),),
              (_out("prompts", "Scene prompts", "text"),),
              (_num("count", "Scenes", 4, 1, 8, integer=True, card=True),
               _text("style", "Visual style", "cinematic, 35mm, natural light", max_length=200, card=True)),
              ("storyboard", "shots")),
    _llm_node("ai.prompt_enhancer", "Prompt Enhancer",
              "Rewrites a short idea into a detailed prompt for an image, video, music or voice model.",
              (_in("text", "Idea", "text", required=True),), (_out("text", "Enhanced prompt", "text"),),
              (_select("target", "Target", (("image", "Image"), ("video", "Video"), ("music", "Music"),
                                            ("voice", "Voice style")), "image", card=True),
               _text("style", "Style hints", max_length=200)),
              ("improve", "rewrite")),
    _llm_node("ai.structured", "JSON / Structured Output",
              "Extracts structured data from text with a strict JSON schema.",
              (_in("text", "Text", "text", required=True, multiple=True),), (_out("json", "JSON", "json"),),
              (_area("instruction", "Instruction", "Extract the fields.", card=True, max_length=4000),
               Field("schema", "Fields", "keyvalue", [{"key": "headline", "value": "string"}], required=True,
                     help="Field name -> string, number, boolean or list.", max_length=32)),
              ("extract", "schema", "convert")),
    NodeType("text.variables", "Variables", "text",
             "Named values. The flow's variables plus these are available as {{name}} in prompts.", "local",
             outputs=(_out("json", "Variables", "json"),),
             fields=(Field("values", "Values", "keyvalue", [], card=True, max_length=64),),
             backend="stored in the flow"),
    NodeType("text.combine", "Combine Text", "text", "Joins several texts, in connection order.", "local",
             inputs=(_in("parts", "Parts", "text", required=True, multiple=True),),
             outputs=(_out("text", "Text", "text"),),
             fields=(_text("separator", "Separator", "\\n\\n", max_length=20, card=True,
                           help="\\n is a line break."),),
             backend="gx10-01", keywords=("join", "merge", "concat")),
    # ================================================================ IMAGE
    NodeType("image.upload", "Upload Image", "image", "An image from the Library or a new upload.", "local",
             outputs=(_out("image", "Image", "image"),), fields=(_asset("image", "Image"),),
             backend="Media Library", keywords=("input", "file")),
    NodeType("image.reference", "Reference Image", "image", "A style or composition reference from the Library.",
             "local", outputs=(_out("image", "Reference", "image"),),
             fields=(_asset("image", "Reference image"), _text("note", "Note", max_length=200)),
             backend="Media Library"),
    NodeType("image.generate", "Generate Image", "image", "Text to image with gx-image.", "image",
             inputs=(_in("prompt", "Prompt", "text"),), outputs=(_out("image", "Image", "image"),),
             fields=(_prompt(placeholder="A trustworthy woman beside a rear-ended BMW, cinematic"),
                     _select("image_model", "Model", (("", "Default model"),), "", card=True,
                             source="image_models:generate",
                             help="Qwen Image 2512 (default) or VisionmasterPro_V3 (SDXL)."),
                     _select("size", "Aspect ratio", IMAGE_SIZES, "", card=True, source="image_sizes",
                             help="Empty = the model's default size. Sizes depend on the model."),
                     _select("quality", "Quality", (("", "Model default"), ("fast", "Fast"),
                                                    ("standard", "Standard"), ("hd", "HD")), "", card=True,
                             help="Only models with quality presets use this."),
                     _area("negative_prompt", "Negative prompt", max_length=2000),
                     _num("steps", "Steps", None, 1, 100, integer=True, help="Empty = model default."),
                     _num("guidance", "Guidance (CFG)", None, 0, 20, step=0.1, help="Empty = model default."),
                     _num("count", "Images", 1, 1, 4, integer=True), SEED, TITLE),
             aliases=("gx-image",), backend="gx-image (ComfyUI on gx10-02 via the media queue)",
             keywords=("t2i", "picture", "photo", "visionmaster", "sdxl")),
    NodeType("image.edit", "Image Edit", "image", "Instruction edit of an image.", "image",
             inputs=(_in("image", "Image", "image", required=True), _in("prompt", "Instruction", "text")),
             outputs=(_out("image", "Image", "image"),),
             fields=(_prompt("Instruction", placeholder="Make it night time"),
                     _select("image_model", "Model", (("", "Default edit model"),), "",
                             source="image_models:edit"),
                     _select("edit_mode", "Edit mode", (("", "Model default"),), "", card=True,
                             source="edit_modes", help="What kind of change to make."),
                     _select("edit_quality", "Edit quality", (("", "Model default"), ("fast", "Fast"),
                                                              ("quality", "Quality")), ""),
                     _num("strength", "Strength", None, 0, 1, step=0.05,
                          help="Only 'Full transformation' uses it. Empty = model default."),
                     _area("negative_prompt", "Negative prompt", max_length=2000),
                     _num("steps", "Steps", None, 1, 50, integer=True), SEED, TITLE),
             aliases=("gx-image",), backend="gx-image edit (Qwen-Image-Edit-2511 by default, ComfyUI on gx10-02)",
             keywords=("inpaint", "change", "restyle")),
    NodeType("image.img2img", "Image-to-Image", "image", "A new image that follows a source image and a prompt.",
             "image", inputs=(_in("image", "Source", "image", required=True), _in("prompt", "Prompt", "text")),
             outputs=(_out("image", "Image", "image"),),
             fields=(_prompt(),
                     _select("image_model", "Model", (("", "Default model"),), "", source="image_models:variation"),
                     _num("strength", "Strength", 0.5, 0.05, 1, step=0.05, card=True), SEED, TITLE),
             aliases=("gx-image",), backend="gx-image variation (ComfyUI on gx10-02)", keywords=("variation",)),
    NodeType("image.character", "Character Reference", "image",
             "A new scene with the same character, using the reference image as identity.", "image",
             inputs=(_in("character", "Character", "image", required=True), _in("prompt", "Scene", "text")),
             outputs=(_out("image", "Image", "image"),),
             fields=(_prompt("Scene", placeholder="standing in a sunny car park, smiling"), SEED, TITLE),
             aliases=("gx-image",), backend="gx-image instruction edit (Qwen-Image-Edit-2511, 'Change' mode) with "
             "an identity-preserving instruction", keywords=("consistent", "person")),
    NodeType("image.upscale", "Upscale Image (resize)", "image",
             "Enlarges an image with a Lanczos resize. This is NOT AI super-resolution: no upscale model is "
             "installed on the cluster.", "ffmpeg",
             inputs=(_in("image", "Image", "image", required=True),), outputs=(_out("image", "Image", "image"),),
             fields=(_select("factor", "Factor", (("2", "2x"), ("3", "3x"), ("4", "4x")), "2", card=True),),
             backend="FFmpeg lanczos resize on gx10-01 (deterministic, max 4096 px)", keywords=("enlarge",)),
    NodeType("image.remove_bg", "Background Removal", "image", "Cut out the subject.", "image",
             inputs=(_in("image", "Image", "image", required=True),), outputs=(_out("image", "Image", "image"),),
             available=False, unavailable_reason="No background-removal model (e.g. BiRefNet/RMBG) is installed "
             "in ComfyUI on gx10-02, so this node cannot run. It is listed so templates can show where it would go.",
             aliases=("gx-image",), backend="none installed", keywords=("cutout", "matte")),
    NodeType("image.output", "Image Output", "image", "Marks the final image(s) of the flow.", "local",
             inputs=(_in("image", "Image", "image", required=True, multiple=True),),
             fields=(TITLE, _bool("favourite", "Add to favourites", False)), output_node=True,
             backend="Media Library"),
    # ================================================================ VIDEO
    NodeType("video.generate", "Generate Video", "video",
             "Text to video, or image to video when a start image is connected (Wan 2.2). LoRA presets apply to "
             "text to video only.", "video",
             inputs=(_in("prompt", "Prompt", "text"), _in("image", "Start image", "image"),
                     _in("lora", "LoRA preset", "lora")),
             outputs=(_out("video", "Video", "video"),),
             fields=(_prompt(placeholder="Slow dolly shot, rain, neon reflections"),
                     _select("size", "Resolution", VIDEO_SIZES, "832x480", card=True),
                     _num("seconds", "Duration (s)", 3, 0.5, 10, step=0.5, card=True),
                     _num("fps", "Frame rate", 16, 8, 24, integer=True),
                     _select("lora_preset", "LoRA preset", (("", "None"),), "", source="lora_presets"),
                     _area("negative_prompt", "Negative prompt", max_length=2000), SEED, TITLE),
             aliases=("gx-video",), backend="gx-video (Wan 2.2 A14B, ComfyUI on gx10-02)", keywords=("clip", "wan")),
    NodeType("video.t2v", "Text-to-Video", "video", "Wan 2.2 text to video.", "video",
             inputs=(_in("prompt", "Prompt", "text"), _in("lora", "LoRA preset", "lora")),
             outputs=(_out("video", "Video", "video"),),
             fields=(_prompt(), _select("size", "Resolution", VIDEO_SIZES, "832x480", card=True),
                     _num("seconds", "Duration (s)", 3, 0.5, 10, step=0.5, card=True),
                     _num("fps", "Frame rate", 16, 8, 24, integer=True),
                     _select("lora_preset", "LoRA preset", (("", "None"),), "", source="lora_presets"),
                     _area("negative_prompt", "Negative prompt", max_length=2000), SEED, TITLE),
             aliases=("gx-video",), backend="gx-video t2v (ComfyUI on gx10-02)"),
    NodeType("video.i2v", "Image-to-Video", "video", "Animates a start image (Wan 2.2 i2v).", "video",
             inputs=(_in("image", "Start image", "image", required=True), _in("prompt", "Motion prompt", "text")),
             outputs=(_out("video", "Video", "video"),),
             fields=(_prompt("Motion prompt", placeholder="the camera slowly pushes in"),
                     _select("size", "Resolution", VIDEO_SIZES, "832x480", card=True),
                     _num("seconds", "Duration (s)", 3, 0.5, 10, step=0.5, card=True),
                     _num("fps", "Frame rate", 16, 8, 24, integer=True),
                     SEED, TITLE),
             aliases=("gx-video",), backend="gx-video i2v (ComfyUI on gx10-02)", keywords=("animate",)),
    NodeType("video.extend", "Extend Video", "video",
             "Continues a clip: its last frame starts a new image-to-video shot, then both are joined.", "video",
             inputs=(_in("video", "Video", "video", required=True), _in("prompt", "Continuation", "text")),
             outputs=(_out("video", "Video", "video"),),
             fields=(_prompt("Continuation prompt"), _num("seconds", "Added seconds", 3, 0.5, 10, step=0.5,
                                                          card=True), SEED, TITLE),
             aliases=("gx-video",), backend="FFmpeg last frame -> gx-video i2v -> FFmpeg concatenation",
             keywords=("continue", "longer")),
    NodeType("video.input", "Video Input", "video", "A video from the Library or a new upload.", "local",
             outputs=(_out("video", "Video", "video"),), fields=(_asset("video", "Video"),),
             backend="Media Library"),
    NodeType("video.lora", "Wan LoRA", "video",
             "References a saved Wan 2.2 LoRA preset by id (no model files are copied).", "local",
             outputs=(_out("lora", "LoRA preset", "lora"),),
             fields=(_select("preset_id", "Preset", (("", "Choose a preset"),), "", card=True,
                             source="lora_presets", required=True),
                     _num("strength_scale", "Strength scale", 1.0, 0, 2, step=0.05,
                          help="Multiplies the preset's strengths.")),
             backend="Wan LoRA library (gx10-01 database, files on gx10-02)", keywords=("style", "adapter")),
    NodeType("video.output", "Video Output", "video", "Marks the final video(s) of the flow.", "local",
             inputs=(_in("video", "Video", "video", required=True, multiple=True),),
             fields=(TITLE, _bool("favourite", "Add to favourites", False)), output_node=True,
             backend="Media Library"),
    # ================================================================ VOICE
    NodeType("voice.tts", "Text-to-Speech", "voice", "Speaks a script with a saved or designed voice.", "voice",
             inputs=(_in("text", "Script", "text"), _in("voice", "Voice", "voice")),
             outputs=(_out("audio", "Speech", "audio"),),
             fields=(_area("text", "Script", card=True, max_length=5000, fills="text"),
                     Field("voice_id", "Voice", "select", "", required=True, card=True, source="voices",
                           fills="voice", help="Used when no voice is connected."),
                     _text("style", "Style / emotion", max_length=300, card=True,
                           placeholder="warm, reassuring, confident"),
                     _select("language", "Language", VOICE_LANGUAGES, "auto"), TITLE),
             aliases=("gx-voice",), backend="gx-voice (Qwen3-TTS 1.7B on gx10-02)", keywords=("tts", "narration")),
    NodeType("voice.design", "Voice Design", "voice", "Creates a new voice from a description.", "voice",
             outputs=(_out("voice", "Voice", "voice"), _out("preview", "Preview", "audio")),
             fields=(_area("description", "Voice description", card=True, max_length=1000, required=True,
                           placeholder="A trustworthy female voice in her thirties, calm and clear"),
                     _area("sample_text", "Preview text", "Hello, this is how I sound.", max_length=500),
                     _text("name", "Voice name", card=True, max_length=80),
                     _select("language", "Language", VOICE_LANGUAGES, "auto")),
             aliases=("gx-voice",), backend="gx-voice VoiceDesign (Qwen3-TTS 1.7B)"),
    NodeType("voice.clone", "Voice Clone", "voice", "Creates a voice from a reference recording.", "voice",
             inputs=(_in("reference", "Reference audio", "audio", required=True),),
             outputs=(_out("voice", "Voice", "voice"),),
             fields=(_text("name", "Voice name", card=True, max_length=80, required=True),
                     _area("reference_text", "Transcript of the reference", max_length=2000),
                     _bool("consent", "I have the speaker's consent", False, card=True)),
             aliases=("gx-voice",), backend="gx-voice Base (Qwen3-TTS 1.7B voice cloning)"),
    NodeType("voice.saved", "Saved Voice", "voice", "A voice from the Voice library.", "local",
             outputs=(_out("voice", "Voice", "voice"),),
             fields=(_select("voice_id", "Voice", (("", "Choose a voice"),), "", card=True, source="voices",
                             required=True),),
             backend="gx-voice voice library"),
    NodeType("voice.dialogue", "Dialogue", "voice",
             "Multi-speaker speech. Script lines look like 'NAME: text'; each name maps to a voice.", "voice",
             inputs=(_in("script", "Script", "text"),),
             outputs=(_out("audio", "Dialogue", "audio"),),
             fields=(_area("script", "Script", card=True, max_length=8000, fills="script",
                           placeholder="ANNA: Hi!\nBEN: Hello Anna."),
                     Field("speakers", "Speakers", "keyvalue", [], max_length=8, source="voices",
                           help="Speaker name -> saved voice id."),
                     _num("pause_ms", "Pause between lines (ms)", 350, 0, 5000, integer=True),
                     _select("language", "Language", VOICE_LANGUAGES, "auto"), TITLE),
             aliases=("gx-voice",), backend="gx-voice dialogue (one segment per line)",
             keywords=("conversation", "multi speaker")),
    NodeType("voice.output", "Voice Output", "voice", "Marks the final speech of the flow.", "local",
             inputs=(_in("audio", "Audio", "audio", required=True, multiple=True),),
             fields=(TITLE, _bool("favourite", "Add to favourites", False)), output_node=True,
             backend="Media Library"),
    # ================================================================ MUSIC
    NodeType("music.generate", "Generate Music", "music", "A full song or instrumental with gx-music.", "music",
             inputs=(_in("description", "Description", "text"), _in("lyrics", "Lyrics", "text")),
             outputs=(_out("audio", "Music", "audio"),),
             fields=(_area("description", "Song description", card=True, max_length=2000, fills="description",
                           placeholder="subtle, hopeful corporate background music"),
                     Field("style_tags", "Style tags", "tags", [], card=True, max_length=12),
                     _area("style_prompt", "Style prompt", max_length=1000),
                     _area("lyrics", "Lyrics", max_length=6000, fills="lyrics"),
                     _num("duration", "Duration (s)", 30, 10, 240, integer=True, card=True),
                     _bool("instrumental", "Instrumental", True, card=True),
                     _select("vocal_language", "Vocal language", LANGUAGES, "en"),
                     _num("bpm", "BPM", None, 40, 220, integer=True), SEED, TITLE),
             aliases=("gx-music",), backend="gx-music (ACE-Step 1.5 XL on gx10-02)", keywords=("song", "soundtrack")),
    NodeType("music.prompt", "Music from Prompt", "music", "Music from a connected text prompt.", "music",
             inputs=(_in("prompt", "Prompt", "text", required=True),), outputs=(_out("audio", "Music", "audio"),),
             fields=(Field("style_tags", "Style tags", "tags", [], card=True, max_length=12),
                     _num("duration", "Duration (s)", 30, 10, 240, integer=True, card=True),
                     _bool("instrumental", "Instrumental", True, card=True), SEED, TITLE),
             aliases=("gx-music",), backend="gx-music (ACE-Step 1.5 XL)"),
    NodeType("music.lyrics", "Music from Lyrics", "music", "A sung song from connected lyrics.", "music",
             inputs=(_in("lyrics", "Lyrics", "text", required=True),), outputs=(_out("audio", "Song", "audio"),),
             fields=(_area("description", "Song description", card=True, max_length=2000),
                     Field("style_tags", "Style tags", "tags", [], max_length=12),
                     _num("duration", "Duration (s)", 60, 10, 240, integer=True, card=True),
                     _select("vocal_language", "Vocal language", LANGUAGES, "en"), SEED, TITLE),
             aliases=("gx-music",), backend="gx-music (ACE-Step 1.5 XL)", keywords=("sing", "vocals")),
    NodeType("music.instrumental", "Instrumental", "music", "Instrumental music without vocals.", "music",
             inputs=(_in("description", "Description", "text"),), outputs=(_out("audio", "Music", "audio"),),
             fields=(_area("description", "Description", card=True, max_length=2000, fills="description"),
                     Field("style_tags", "Style tags", "tags", [], max_length=12),
                     _num("duration", "Duration (s)", 30, 10, 240, integer=True, card=True), SEED, TITLE),
             aliases=("gx-music",), backend="gx-music (ACE-Step 1.5 XL, instrumental)", keywords=("background",)),
    NodeType("music.output", "Music Output", "music", "Marks the final music of the flow.", "local",
             inputs=(_in("audio", "Audio", "audio", required=True, multiple=True),),
             fields=(TITLE, _bool("favourite", "Add to favourites", False)), output_node=True,
             backend="Media Library"),
    # ================================================================ SOUND
    NodeType("sound.sfx", "Generate SFX", "sound", "Sound effects from a description.", "music",
             outputs=(_out("audio", "Sound", "audio"),),
             fields=(_area("description", "Description", card=True, max_length=500),),
             available=False, unavailable_reason="No sound-effect generation model is installed on the cluster. "
             "ACE-Step is a music model and does not produce reliable one-shot effects, so this node is not faked. "
             "Use Audio Upload with a recorded effect instead.", backend="none installed", keywords=("foley",)),
    NodeType("sound.ambient", "Ambient Sound", "sound",
             "Ambient background texture rendered by the music model as an instrumental (ACE-Step). It is "
             "music-like ambience, not field-recorded sound.", "music",
             outputs=(_out("audio", "Ambience", "audio"),),
             fields=(_area("description", "Ambience", "calm airy ambient pad, soft texture, no drums", card=True,
                           max_length=1000),
                     _num("duration", "Duration (s)", 30, 10, 240, integer=True, card=True), SEED, TITLE),
             aliases=("gx-music",), backend="gx-music (ACE-Step 1.5 XL, instrumental ambient prompt)",
             keywords=("atmosphere", "pad", "room tone")),
    NodeType("sound.upload", "Audio Upload", "sound", "Audio from the Library or a new upload.", "local",
             outputs=(_out("audio", "Audio", "audio"),), fields=(_asset("audio", "Audio"),),
             backend="Media Library", keywords=("input", "sfx file")),
    NodeType("sound.output", "Audio Output", "sound", "Marks the final audio of the flow.", "local",
             inputs=(_in("audio", "Audio", "audio", required=True, multiple=True),),
             fields=(TITLE, _bool("favourite", "Add to favourites", False)), output_node=True,
             backend="Media Library"),
    # ========================================================== COMPOSITION
    NodeType("compose.merge_audio", "Merge Audio", "compose", "Plays audio clips one after another.", "ffmpeg",
             inputs=(_in("audio", "Clips", "audio", required=True, multiple=True),),
             outputs=(_out("audio", "Audio", "audio"),),
             fields=(_num("gap", "Gap (s)", 0, 0, 10, step=0.1, card=True), TITLE),
             backend="FFmpeg on gx10-01", keywords=("join", "sequence")),
    NodeType("compose.mix_audio", "Mix Audio", "compose", "Mixes audio clips on top of each other.", "ffmpeg",
             inputs=(_in("audio", "Tracks", "audio", required=True, multiple=True),),
             outputs=(_out("audio", "Mix", "audio"),),
             fields=(_select("length", "Length", (("longest", "Longest track"), ("first", "First track"),
                                                  ("shortest", "Shortest track")), "longest", card=True),
                     _num("volume_db", "Volume of added tracks (dB)", -8, -40, 12, step=0.5), TITLE),
             backend="FFmpeg amix on gx10-01", keywords=("layer",)),
    NodeType("compose.add_voice", "Add Voice to Video", "compose", "Puts a voice-over on a video.", "ffmpeg",
             inputs=(_in("video", "Video", "video", required=True), _in("audio", "Voice", "audio", required=True)),
             outputs=(_out("video", "Video", "video"),),
             fields=(_num("volume_db", "Voice level (dB)", 0, -40, 12, step=0.5, card=True),
                     _num("offset", "Start at (s)", 0, 0, 600, step=0.1),
                     _bool("keep_original", "Keep the video's own audio", True),
                     _select("fit", "Length", (("video", "Keep video length"), ("longest", "Extend to the voice "
                                                                                          "(freeze last frame)")),
                             "longest", card=True), TITLE),
             backend="FFmpeg on gx10-01", keywords=("voiceover", "narration")),
    NodeType("compose.add_music", "Add Music to Video", "compose", "A music bed under a video.", "ffmpeg",
             inputs=(_in("video", "Video", "video", required=True), _in("audio", "Music", "audio", required=True)),
             outputs=(_out("video", "Video", "video"),),
             fields=(_num("volume_db", "Music level (dB)", -14, -40, 12, step=0.5, card=True),
                     _num("fade_out", "Fade out (s)", 1.5, 0, 30, step=0.1),
                     _bool("loop", "Loop music to the video length", True),
                     _bool("keep_original", "Keep the video's own audio", True), TITLE),
             backend="FFmpeg on gx10-01", keywords=("soundtrack", "bed")),
    NodeType("compose.add_sfx", "Add SFX to Video", "compose", "Places a sound effect at a time in a video.",
             "ffmpeg",
             inputs=(_in("video", "Video", "video", required=True), _in("audio", "Effect", "audio", required=True)),
             outputs=(_out("video", "Video", "video"),),
             fields=(_num("offset", "At (s)", 0, 0, 600, step=0.1, card=True),
                     _num("volume_db", "Level (dB)", 0, -40, 12, step=0.5, card=True), TITLE),
             backend="FFmpeg on gx10-01"),
    NodeType("compose.trim", "Trim", "compose", "Keeps a time range of a video or audio clip.", "ffmpeg",
             inputs=(_in("media", "Clip", *MEDIA_IO, required=True),),
             outputs=(_out("media", "Clip", "any", same_as="media"),),
             fields=(_num("start", "Start (s)", 0, 0, 3600, step=0.1, card=True),
                     _num("end", "End (s)", None, 0.1, 3600, step=0.1, card=True, help="Empty = to the end."),
                     TITLE),
             backend="FFmpeg on gx10-01", keywords=("cut",)),
    NodeType("compose.fade_in", "Fade In", "compose", "Fades a clip in from black / silence.", "ffmpeg",
             inputs=(_in("media", "Clip", *MEDIA_IO, required=True),),
             outputs=(_out("media", "Clip", "any", same_as="media"),), fields=(FADE_DURATION, TITLE),
             backend="FFmpeg on gx10-01"),
    NodeType("compose.fade_out", "Fade Out", "compose", "Fades a clip out to black / silence.", "ffmpeg",
             inputs=(_in("media", "Clip", *MEDIA_IO, required=True),),
             outputs=(_out("media", "Clip", "any", same_as="media"),), fields=(FADE_DURATION, TITLE),
             backend="FFmpeg on gx10-01"),
    NodeType("compose.volume", "Volume", "compose", "Changes the loudness of a clip.", "ffmpeg",
             inputs=(_in("media", "Clip", *MEDIA_IO, required=True),),
             outputs=(_out("media", "Clip", "any", same_as="media"),),
             fields=(_num("gain_db", "Gain (dB)", 0, -40, 20, step=0.5, card=True), TITLE),
             backend="FFmpeg on gx10-01", keywords=("gain", "louder")),
    NodeType("compose.normalize", "Normalize", "compose", "EBU R128 loudness normalisation.", "ffmpeg",
             inputs=(_in("media", "Clip", *MEDIA_IO, required=True),),
             outputs=(_out("media", "Clip", "any", same_as="media"),),
             fields=(_num("target_lufs", "Target (LUFS)", -16, -30, -9, step=0.5, card=True), TITLE),
             backend="FFmpeg loudnorm on gx10-01", keywords=("loudness",)),
    NodeType("compose.resize", "Resize", "compose", "Scales an image or video.", "ffmpeg",
             inputs=(_in("media", "Image or video", *VISUAL_IO, required=True),),
             outputs=(_out("media", "Result", "any", same_as="media"),),
             fields=(_num("width", "Width", 1280, 16, 4096, integer=True, card=True),
                     _num("height", "Height", 720, 16, 4096, integer=True, card=True),
                     _select("fit", "Fit", (("contain", "Fit inside (letterbox)"), ("cover", "Fill (crop)"),
                                            ("stretch", "Stretch")), "contain", card=True), TITLE),
             backend="FFmpeg on gx10-01", keywords=("scale",)),
    NodeType("compose.crop", "Crop", "compose", "Crops an image or video to an aspect ratio.", "ffmpeg",
             inputs=(_in("media", "Image or video", *VISUAL_IO, required=True),),
             outputs=(_out("media", "Result", "any", same_as="media"),),
             fields=(_select("aspect", "Aspect", (("9:16", "9:16 (Reels, Stories)"), ("1:1", "1:1"),
                                                  ("4:5", "4:5 (Feed)"), ("16:9", "16:9")), "9:16", card=True),
                     _select("anchor", "Keep", (("center", "Centre"), ("start", "Left / top"),
                                                ("end", "Right / bottom")), "center"), TITLE),
             backend="FFmpeg on gx10-01", keywords=("aspect", "reframe")),
    NodeType("compose.concat", "Concatenate Clips", "compose", "Joins videos in connection order.", "ffmpeg",
             inputs=(_in("video", "Clips", "video", required=True, multiple=True),),
             outputs=(_out("video", "Video", "video"),),
             fields=(_select("size", "Output size", (("first", "Same as first clip"),) + VIDEO_SIZES +
                             (("1280x720", "1280x720"), ("720x1280", "720x1280"), ("1920x1080", "1920x1080")),
                             "first", card=True),
                     _num("fps", "Frame rate", 16, 8, 60, integer=True), TITLE),
             backend="FFmpeg on gx10-01", keywords=("join", "sequence", "stitch")),
    NodeType("compose.overlay", "Overlay", "compose", "Places an image (logo, product) over a video.", "ffmpeg",
             inputs=(_in("video", "Video", "video", required=True), _in("image", "Overlay", "image", required=True)),
             outputs=(_out("video", "Video", "video"),),
             fields=(_select("position", "Position", (("top-right", "Top right"), ("top-left", "Top left"),
                                                      ("bottom-right", "Bottom right"),
                                                      ("bottom-left", "Bottom left"), ("center", "Centre")),
                             "top-right", card=True),
                     _num("scale", "Size (% of video width)", 20, 2, 100, integer=True, card=True),
                     _num("opacity", "Opacity", 1, 0.05, 1, step=0.05),
                     _num("start", "From (s)", 0, 0, 3600, step=0.1),
                     _num("end", "Until (s)", None, 0, 3600, step=0.1), TITLE),
             backend="FFmpeg on gx10-01", keywords=("logo", "watermark")),
    NodeType("compose.captions", "Captions", "compose",
             "Draws a caption or call-to-action text on a video (title card / lower third).", "ffmpeg",
             inputs=(_in("video", "Video", "video", required=True), _in("text", "Text", "text")),
             outputs=(_out("video", "Video", "video"),),
             fields=(_area("text", "Text", card=True, max_length=300, fills="text"),
                     _select("position", "Position", (("bottom", "Bottom"), ("center", "Centre"), ("top", "Top")),
                             "bottom", card=True),
                     _num("font_size", "Font size (% of height)", 6, 2, 20, step=0.5),
                     _num("start", "From (s)", 0, 0, 3600, step=0.1),
                     _num("end", "Until (s)", None, 0, 3600, step=0.1, help="Empty = to the end."),
                     _num("last_seconds", "Only the last N seconds", None, 0.5, 60, step=0.5,
                          help="Shows the text at the end of the video (overrides From/Until)."),
                     _bool("box", "Background box", True), TITLE),
             backend="FFmpeg drawtext on gx10-01", keywords=("cta", "title", "text")),
    NodeType("compose.subtitles", "Burn Subtitles", "compose",
             "Burns timed subtitles into a video. Connected script text is split into sentences and spread "
             "over the video's duration (or use SRT text).", "ffmpeg",
             inputs=(_in("video", "Video", "video", required=True), _in("text", "Script or SRT", "text")),
             outputs=(_out("video", "Video", "video"),),
             fields=(_area("text", "Script or SRT", max_length=20000, fills="text", card=True),
                     _num("font_size", "Font size", 22, 8, 72, integer=True), TITLE),
             backend="FFmpeg subtitles filter on gx10-01", keywords=("srt", "captions")),
    NodeType("compose.export", "Export Video", "compose",
             "Final render: H.264/AAC MP4 with fast start, optional audio track.", "ffmpeg",
             inputs=(_in("video", "Video", "video", required=True), _in("audio", "Audio track", "audio")),
             outputs=(_out("video", "Video", "video"),),
             fields=(_select("preset", "Format", (("source", "Keep size"), ("1080x1920", "Vertical 1080x1920"),
                                                  ("1920x1080", "Landscape 1920x1080"),
                                                  ("1080x1080", "Square 1080x1080"),
                                                  ("1280x720", "Landscape 720p")), "source", card=True),
                     _select("quality", "Quality", (("high", "High (CRF 18)"), ("standard", "Standard (CRF 22)"),
                                                    ("small", "Small (CRF 28)")), "standard", card=True),
                     _num("fps", "Frame rate", None, 8, 60, integer=True, help="Empty = keep."),
                     TITLE, _bool("favourite", "Add to favourites", False)),
             output_node=True, backend="FFmpeg libx264 on gx10-01", keywords=("render", "final", "mp4")),
    # ============================================================== UTILITY
    NodeType("util.delay", "Delay", "utility", "Waits before passing its input on.", "local",
             inputs=(_in("value", "Value", *PORT_TYPES, required=True),),
             outputs=(_out("value", "Value", "any", same_as="value"),),
             fields=(_num("seconds", "Wait (s)", 5, 0, 300, integer=True, card=True),),
             cacheable=False, backend="gx10-01"),
    NodeType("util.conditional", "Conditional", "utility",
             "Sends its input to 'Yes' or 'No'. The branch that is not taken is skipped.", "local",
             inputs=(_in("value", "Value", *PORT_TYPES, required=True),),
             outputs=(_out("yes", "Yes", "any", same_as="value"), _out("no", "No", "any", same_as="value")),
             fields=(_select("test", "Condition", (("not_empty", "Is not empty"), ("contains", "Text contains"),
                                                   ("equals", "Text equals"), ("longer", "Longer than (characters)"),
                                                   ("count_at_least", "Has at least N items")),
                             "not_empty", card=True),
                     _text("argument", "Value", card=True, max_length=200),
                     _bool("case_sensitive", "Case sensitive", False)),
             backend="gx10-01", keywords=("if", "branch")),
    NodeType("util.router", "Router", "utility",
             "Routes its input to the first matching route (text contains a keyword); others are skipped.",
             "local",
             inputs=(_in("value", "Value", *PORT_TYPES, required=True),),
             outputs=(_out("route1", "Route 1", "any", same_as="value"),
                      _out("route2", "Route 2", "any", same_as="value"),
                      _out("route3", "Route 3", "any", same_as="value"),
                      _out("fallback", "Otherwise", "any", same_as="value")),
             fields=(_text("route1", "Route 1 keyword", card=True, max_length=80),
                     _text("route2", "Route 2 keyword", card=True, max_length=80),
                     _text("route3", "Route 3 keyword", max_length=80),
                     _select("match_on", "Match on", (("value", "The input text"),
                                                      ("variable", "A flow variable")), "value"),
                     _text("variable", "Variable name", max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")),
             backend="gx10-01", keywords=("switch",)),
    NodeType("util.select", "Select", "utility",
             "Picks one field of JSON (or one item of a list) as text: the explicit JSON-to-text conversion.",
             "local",
             inputs=(_in("json", "JSON", "json", required=True),), outputs=(_out("text", "Text", "text"),),
             fields=(_text("path", "Path", "headline", card=True, max_length=200,
                           pattern=r"^[A-Za-z0-9_\-.\[\]]{1,200}$",
                           help="e.g. scenes[0].visual_prompt"),
                     _bool("each", "One item per list entry", False)),
             backend="gx10-01", keywords=("convert", "extract", "pick")),
    NodeType("util.batch", "Batch", "utility",
             "Repeats a text N times (optionally with numbered variations); downstream nodes run once per item.",
             "local",
             inputs=(_in("text", "Text", "text", required=True),), outputs=(_out("text", "Items", "text"),),
             fields=(_num("count", "Count", 3, 1, 8, integer=True, card=True),
                     _text("suffix", "Variation suffix", ", variation {{n}}", max_length=120,
                           help="Appended to each item; {{n}} is the item number. Empty = identical items.")),
             backend="gx10-01", keywords=("repeat", "multiple")),
    NodeType("util.iterator", "Iterator", "utility",
             "Splits text into lines (or a JSON list into items); downstream nodes run once per item.", "local",
             inputs=(_in("value", "List", "text", "json", required=True),), outputs=(_out("text", "Items", "text"),),
             fields=(_select("split", "Split on", (("lines", "Lines"), ("paragraphs", "Blank lines"),
                                                   ("json", "JSON list items")), "lines", card=True),
                     _num("limit", "Max items", 8, 1, 16, integer=True)),
             backend="gx10-01", keywords=("each", "loop", "map")),
    NodeType("util.file_input", "File Input", "utility", "Any Library item (image, video or audio).", "local",
             outputs=(_out("file", "File", "any"),),
             fields=(_select("asset_type", "Kind", (("image", "Image"), ("video", "Video"), ("audio", "Audio")),
                             "image", card=True, required=True),
                     Field("asset_id", "Library item", "asset", None, required=True, card=True,
                           pattern=r"^a_[0-9a-f]{24}$", help="Must match the kind above."),),
             backend="Media Library", keywords=("asset",)),
    NodeType("util.file_output", "File Output", "utility", "Marks any media result as a final output.", "local",
             inputs=(_in("file", "File", *MEDIA_TYPES, required=True, multiple=True),),
             fields=(TITLE, _bool("favourite", "Add to favourites", False)), output_node=True,
             backend="Media Library"),
    NodeType("util.webhook", "Webhook", "utility",
             "POSTs a JSON summary of its input to an external URL (public addresses only).", "http",
             inputs=(_in("value", "Payload", *PORT_TYPES, required=True, multiple=True),),
             outputs=(_out("response", "Response", "json"),),
             fields=(_text("url", "URL", card=True, max_length=2048, required=True,
                           placeholder="https://hooks.example.com/..."),
                     Field("headers", "Headers", "headers", [], max_length=8,
                           help="Header -> stored secret name (Settings > Flow secrets). Values are never shown."),
                     _num("timeout", "Timeout (s)", 15, 1, 60, integer=True)),
             cacheable=False, backend="netguard (SSRF-safe) from gx10-01", keywords=("notify", "http")),
    NodeType("util.api_request", "API Request", "utility",
             "Calls an external HTTP API (public addresses only) and returns the JSON response.", "http",
             inputs=(_in("body", "Body", "json", "text"),), outputs=(_out("response", "Response", "json"),),
             fields=(_select("method", "Method", (("GET", "GET"), ("POST", "POST"), ("PUT", "PUT"),
                                                  ("PATCH", "PATCH"), ("DELETE", "DELETE")), "GET", card=True),
                     _text("url", "URL", card=True, max_length=2048, required=True),
                     Field("headers", "Headers", "headers", [], max_length=8,
                           help="Header -> stored secret name. Values are never shown."),
                     _num("timeout", "Timeout (s)", 15, 1, 60, integer=True)),
             cacheable=False, backend="netguard (SSRF-safe) from gx10-01", keywords=("http", "rest", "fetch")),
]

NODES: dict[str, NodeType] = {n.type: n for n in _NODES}
FIELD_KINDS = frozenset({"text", "textarea", "select", "number", "boolean", "asset", "seed", "keyvalue", "tags",
                         "headers"})


def _self_check() -> None:
    """Catalogue invariants (run at import; a broken catalogue must not start)."""
    cats = {c for c, _ in CATEGORIES}
    for n in _NODES:
        assert n.category in cats, n.type  # noqa: S101
        assert len({p.id for p in n.inputs}) == len(n.inputs), n.type  # noqa: S101
        assert len({p.id for p in n.outputs}) == len(n.outputs), n.type  # noqa: S101
        assert len({f.id for f in n.fields}) == len(n.fields), n.type  # noqa: S101
        for p in n.inputs:
            assert all(t in PORT_TYPES for t in p.types), (n.type, p.id)  # noqa: S101
        for p in n.outputs:
            assert len(p.types) == 1 and (p.types[0] in PORT_TYPES or (p.types[0] == "any")), (n.type, p.id)  # noqa: S101
        for f in n.fields:
            assert f.kind in FIELD_KINDS, (n.type, f.id)  # noqa: S101
            if f.fills:
                assert n.input(f.fills) is not None, (n.type, f.id)  # noqa: S101


_self_check()


def catalog_public(live: dict[str, str] | None = None) -> dict[str, Any]:
    """The catalogue as served to the canvas. ``live`` maps a node type to a
    runtime unavailability reason (e.g. gx-voice not installed yet)."""
    live = live or {}
    nodes = []
    for n in _NODES:
        d = n.public()
        if d["available"] and n.type in live:
            d["available"] = False
            d["unavailable_reason"] = live[n.type]
        nodes.append(d)
    return {"version": 1, "port_types": list(PORT_TYPES),
            "categories": [{"id": c, "label": lab} for c, lab in CATEGORIES], "nodes": nodes}


def get(node_type: str) -> NodeType:
    try:
        return NODES[node_type]
    except KeyError:
        raise KeyError(f"unknown node type {node_type!r}") from None
