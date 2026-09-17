"""gx-image model selection and edit planning (Build V3, workstream IMG).

`gx-image` stays ONE public alias; inside it a caller may pick a model:

    qwen-image-2512        Qwen-Image-2512 text-to-image (the default generator)
    qwen-image-edit-2511   Qwen-Image-Edit-2511 instruction edits (the default editor)
    visionmaster-pro-v3    "VisionmasterPro_V3", an SDXL (NoobAI eps) checkpoint:
                           text-to-image, image-to-image and masked inpainting

An edit is PLANNED here, from (model, edit mode, strength, mask), into a
vetted template plus the parameters that genuinely differ per mode: the
instruction template, the reference-latent method, the denoise and the
mask handling. Pure: no I/O, unit-tested.

Why Qwen edits always run a full denoise (measured 2026-09-17, see
coordination/build-v3/img.md): Qwen-Image-Edit-2511 is conditioned on the
source through its reference latent. The latent it samples FROM is only a
canvas. A partial denoise of the VAE-encoded source on the 4-step Lightning
schedule kept the source almost pixel for pixel (the "edit returns the
original" bug), because the Playground sent strength 0.6 and the router
bound strength 1:1 to KSampler.denoise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ValidationError

QWEN_GENERATE = "qwen-image-2512"
QWEN_EDIT = "qwen-image-edit-2511"
VISIONMASTER = "visionmaster-pro-v3"

#: Qwen-Image-2512 template per quality (unchanged public behaviour)
QWEN_GENERATE_WORKFLOWS = {
    "standard": "qwen-image-2512-uncensored",
    "fast": "qwen-image-2512-lightning",
    "hd": "qwen-image-2512-quality",
}
QWEN_EDIT_WORKFLOW = "qwen-image-edit-2511"
QWEN_EDIT_MASKED_WORKFLOW = "qwen-image-edit-2511-masked"
QWEN_EDIT_TRANSFORM_WORKFLOW = "qwen-image-edit-2511-transform"
SDXL_GENERATE_WORKFLOW = "sdxl-visionmaster-pro-v3"
SDXL_IMG2IMG_WORKFLOW = "sdxl-visionmaster-pro-v3-img2img"
SDXL_INPAINT_WORKFLOW = "sdxl-visionmaster-pro-v3-inpaint"

#: sizes each generator is trained for (the UI offers exactly these)
QWEN_SIZES = ("1328x1328", "1024x1024", "1328x800", "800x1328", "1664x928", "928x1664", "768x768", "512x512")
SDXL_SIZES = ("1024x1024", "832x1216", "1216x832", "896x1152", "1152x896", "768x1344", "1344x768")
#: SDXL is trained at about one megapixel; larger canvases duplicate subjects
SDXL_MAX_PIXELS = 1_600_000

#: NoobAI-family checkpoints are trained with quality tags; the router appends
#: them (recorded in the response as prompt_suffix) unless the caller opts out.
SDXL_QUALITY_SUFFIX = "masterpiece, best quality, amazing quality, very aesthetic, absurdres, newest"
SDXL_NEGATIVE_DEFAULT = ("worst quality, low quality, normal quality, lowres, bad anatomy, bad hands, "
                         "extra fingers, missing fingers, jpeg artifacts, signature, watermark, text, blurry")


@dataclass(frozen=True)
class EditMode:
    id: str
    label: str
    description: str
    #: Qwen instruction template; "{instruction}" is the caller's text
    qwen_template: str
    #: Qwen reference-latent use: "index_timestep_zero" (source anchors the result) | "none"
    qwen_reference: str
    #: Qwen denoise range mapped from strength (lo == hi: strength does not apply)
    qwen_denoise: tuple[float, float]
    #: SDXL denoise range mapped from strength
    sdxl_denoise: tuple[float, float]
    #: SDXL needs a mask for this mode (it has no instruction following)
    sdxl_needs_mask: bool
    #: mask grow in pixels at the working size (masked templates)
    mask_grow: int = 8

    def public(self, model: str) -> dict:
        if model == VISIONMASTER:
            lo, hi = self.sdxl_denoise
            needs_mask = self.sdxl_needs_mask
        else:
            lo, hi = self.qwen_denoise
            needs_mask = False
        return {"id": self.id, "label": self.label, "description": self.description,
                "strength_applies": hi > lo, "denoise_range": [lo, hi], "requires_mask": needs_mask,
                "preserves_source_latent": model == QWEN_EDIT and self.qwen_reference != "none"}


_KEEP = ("Keep everything else in the image exactly as it is: the same people with the same faces and identity, "
         "the same pose, framing, lighting and background.")

EDIT_MODES: dict[str, EditMode] = {m.id: m for m in (
    EditMode("instruct", "Instruction (as typed)",
             "Sends your instruction unchanged. Compatible with the plain OpenAI edits API.",
             "{instruction}", "index_timestep_zero", (1.0, 1.0), (0.45, 0.9), False),
    EditMode("change", "Change / replace",
             "Changes or replaces one thing and keeps the rest of the picture.",
             "{instruction}. Change only what this instruction asks for. " + _KEEP,
             "index_timestep_zero", (1.0, 1.0), (0.6, 1.0), True),
    EditMode("add", "Add",
             "Adds something new to the scene with matching light, perspective and scale.",
             "Add the following to the image: {instruction}. Place it naturally with matching lighting, "
             "perspective and scale. " + _KEEP,
             "index_timestep_zero", (1.0, 1.0), (0.6, 1.0), True),
    EditMode("remove", "Remove",
             "Removes something and fills the gap so it blends with its surroundings.",
             "Remove the following from the image: {instruction}. Fill the area it occupied so it blends "
             "seamlessly with its surroundings. " + _KEEP,
             "index_timestep_zero", (1.0, 1.0), (0.8, 1.0), True, 16),
    EditMode("restyle", "Restyle",
             "Redraws the whole picture in a new style; the composition stays.",
             "Redraw the entire image in this style: {instruction}. Keep the same subject, pose and "
             "composition, but apply the new style to every part of the image.",
             "index_timestep_zero", (1.0, 1.0), (0.45, 0.85), False),
    EditMode("background", "Background",
             "Replaces the background and keeps the subject.",
             "Replace the background with: {instruction}. Keep the main subject exactly as they are: the same "
             "identity, face, pose, clothing and position. Match the lighting on the subject to the new "
             "background.",
             "index_timestep_zero", (1.0, 1.0), (0.75, 1.0), True, 4),
    EditMode("subject", "Subject",
             "Changes the main subject (clothing, appearance, pose); the background stays.",
             "Change the main subject as follows: {instruction}. Keep the background, the camera angle and the "
             "composition unchanged.",
             "index_timestep_zero", (1.0, 1.0), (0.6, 1.0), True),
    EditMode("transform", "Full transformation",
             "Large changes: the source guides the result but does not pin it. Strength sets how far it may go.",
             "Transform this image: {instruction}.",
             "none", (0.8, 1.0), (0.7, 1.0), False),
)}

#: The Images page offers these (instruct is the API default only)
UI_EDIT_MODES = ("change", "add", "remove", "restyle", "background", "subject", "transform")


@dataclass(frozen=True)
class ImageModel:
    id: str
    label: str
    family: str
    operations: tuple[str, ...]
    description: str
    sizes: tuple[str, ...] = ()
    default_size: str = ""
    defaults: dict = field(default_factory=dict)
    masks: bool = False
    edit_modes: tuple[str, ...] = ()
    negative_prompt: bool = True
    qualities: tuple[str, ...] = ()

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "family": self.family, "operations": list(self.operations),
                "description": self.description, "sizes": list(self.sizes), "default_size": self.default_size,
                "defaults": dict(self.defaults), "masks": self.masks, "negative_prompt": self.negative_prompt,
                "qualities": list(self.qualities),
                "edit_modes": [EDIT_MODES[m].public(self.id) for m in self.edit_modes]}


IMAGE_MODELS: dict[str, ImageModel] = {m.id: m for m in (
    ImageModel(QWEN_GENERATE, "Qwen Image 2512", "qwen-image", ("generate",),
               "Qwen-Image-2512: strong prompt following and text rendering.",
               QWEN_SIZES, "1328x1328", {"steps": 4, "cfg": 1.0, "quality": "standard"},
               qualities=("fast", "standard", "hd")),
    ImageModel(QWEN_EDIT, "Qwen Image Edit 2511", "qwen-image", ("edit", "variation"),
               "Qwen-Image-Edit-2511: instruction edits that keep identity and composition.",
               (), "", {"steps": 4, "cfg": 1.0, "edit_mode": "change", "edit_quality": "fast"},
               masks=True, edit_modes=UI_EDIT_MODES, negative_prompt=True, qualities=("fast", "quality")),
    ImageModel(VISIONMASTER, "VisionmasterPro_V3", "sdxl", ("generate", "edit"),
               "SDXL (NoobAI eps) photoreal checkpoint: text-to-image, image-to-image and masked inpainting. "
               "Describe the result you want; it does not follow edit instructions.",
               SDXL_SIZES, "832x1216", {"steps": 28, "cfg": 5.0, "sampler_name": "euler_ancestral",
                                        "scheduler": "normal", "edit_mode": "restyle"},
               masks=True, edit_modes=UI_EDIT_MODES, negative_prompt=True),
)}

GENERATE_DEFAULT = QWEN_GENERATE
EDIT_DEFAULT = QWEN_EDIT


def resolve(value: object, operation: str) -> ImageModel:
    """The caller's `image_model` (or the default) for `operation`."""
    if value is None or value == "":
        return IMAGE_MODELS[GENERATE_DEFAULT if operation == "generate" else EDIT_DEFAULT]
    if not isinstance(value, str) or value not in IMAGE_MODELS:
        raise ValidationError(f"image_model must be one of: {', '.join(IMAGE_MODELS)}", param="image_model")
    model = IMAGE_MODELS[value]
    if operation not in model.operations:
        can = [m.id for m in IMAGE_MODELS.values() if operation in m.operations]
        raise ValidationError(f"{model.label} cannot {operation}; use one of: {', '.join(can)}",
                              param="image_model")
    return model


def model_for_workflow(workflow: str) -> str | None:
    """Which gx-image model a template serves (for callers that name a workflow)."""
    if workflow in QWEN_GENERATE_WORKFLOWS.values():
        return QWEN_GENERATE
    if workflow.startswith("qwen-image-edit-2511"):
        return QWEN_EDIT
    if workflow.startswith("sdxl-visionmaster-pro-v3"):
        return VISIONMASTER
    return None


@dataclass(frozen=True)
class EditPlan:
    workflow: str
    mode: str
    prompt: str
    denoise: float
    params: dict
    #: strength actually applied (None when the mode ignores it)
    strength: float | None
    masked: bool

    def public(self) -> dict:
        return {"edit_mode": self.mode, "denoise": self.denoise, "strength_applied": self.strength,
                "masked": self.masked, "workflow": self.workflow}


def _scale(rng: tuple[float, float], strength: float) -> float:
    lo, hi = rng
    return round(lo + (hi - lo) * max(0.0, min(1.0, strength)), 3)


def plan_edit(model: ImageModel, mode_id: str | None, instruction: str, strength: float | None,
              *, has_mask: bool, quality: str | None = None) -> EditPlan:
    """Turn an edit request into a template and the parameters that differ per mode."""
    mode_id = mode_id or ("instruct" if model.id == QWEN_EDIT else "restyle")
    mode = EDIT_MODES.get(mode_id)
    if mode is None:
        raise ValidationError(f"edit_mode must be one of: {', '.join(EDIT_MODES)}", param="edit_mode")
    if model.id == VISIONMASTER:
        if mode.id == "instruct":
            mode = EDIT_MODES["restyle"]
        if mode.sdxl_needs_mask and not has_mask:
            raise ValidationError(
                f"VisionmasterPro_V3 does not follow edit instructions, so '{mode.label}' needs a mask: paint "
                "or draw the area to change, or use Restyle / Full transformation, or pick Qwen Image Edit 2511.",
                param="mask")
        if quality not in (None, "", "fast", "standard"):
            raise ValidationError("edit_quality applies to Qwen Image Edit 2511 only", param="edit_quality")
        applied = 0.6 if strength is None else strength
        lo, hi = mode.sdxl_denoise
        denoise = _scale(mode.sdxl_denoise, applied)
        params = {"denoise": denoise}
        if has_mask:
            params["mask_grow"] = mode.mask_grow
        return EditPlan(SDXL_INPAINT_WORKFLOW if has_mask else SDXL_IMG2IMG_WORKFLOW, mode.id, instruction,
                        denoise, params, applied if hi > lo else None, has_mask)
    if model.id != QWEN_EDIT:  # pragma: no cover - resolve() already refused it
        raise ValidationError(f"{model.label} cannot edit", param="image_model")
    if has_mask and mode.qwen_reference == "none":
        raise ValidationError("Full transformation changes the whole image; clear the mask or pick another mode",
                              param="mask")
    lo, hi = mode.qwen_denoise
    applies = hi > lo
    applied = (1.0 if strength is None else strength) if applies else None
    denoise = _scale(mode.qwen_denoise, applied) if applies else hi
    params: dict = {"denoise": denoise}
    if mode.qwen_reference == "none":
        workflow = QWEN_EDIT_TRANSFORM_WORKFLOW
    else:
        workflow = QWEN_EDIT_MASKED_WORKFLOW if has_mask else QWEN_EDIT_WORKFLOW
        params["reference_method"] = mode.qwen_reference
    if has_mask:
        params["mask_grow"] = mode.mask_grow
    if quality == "quality":
        # true-CFG path of the base model: no Lightning distill, real negative prompt
        params.update({"lightning_strength": 0.0, "steps": 20, "cfg": 4.0})
    elif quality not in (None, "", "fast"):
        raise ValidationError("edit_quality must be 'fast' or 'quality'", param="edit_quality")
    prompt = mode.qwen_template.format(instruction=instruction.rstrip(" .")) if mode.id != "instruct" \
        else instruction
    return EditPlan(workflow, mode.id, prompt, denoise, params, applied, has_mask)


def plan_variation(strength: float | None, prompt: str) -> EditPlan:
    """A variation is a Qwen edit whose strength sets how far it may drift.

    Low strength keeps the reference latent (same scene, new details); from
    0.5 up the reference latent is dropped and the result is re-imagined from
    the source's description (measured: coordination/build-v3/img.md)."""
    applied = 0.75 if strength is None else strength
    if applied >= 0.5:
        denoise = _scale((0.85, 1.0), (applied - 0.5) / 0.5)
        return EditPlan(QWEN_EDIT_TRANSFORM_WORKFLOW, "variation", prompt, denoise, {"denoise": denoise},
                        applied, False)
    return EditPlan(QWEN_EDIT_WORKFLOW, "variation", prompt, 1.0,
                    {"denoise": 1.0, "reference_method": "index_timestep_zero"}, applied, False)


def options() -> dict:
    """The model list for GET /v1/image-models (and the Control UI's media options)."""
    return {"default_generate": GENERATE_DEFAULT, "default_edit": EDIT_DEFAULT,
            "models": [m.public() for m in IMAGE_MODELS.values()],
            "edit_modes": [{"id": m.id, "label": m.label, "description": m.description} for m in EDIT_MODES.values()
                           if m.id in UI_EDIT_MODES]}
