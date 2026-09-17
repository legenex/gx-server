"""Built-in flow templates (real, runnable graphs) and the shared auto-layout.

Every template is validated by ``schema.validate_document`` and must be
ready to run (``schema.readiness`` empty) — the unit tests enforce both.
Templates only use node types that have a real backend.
"""

from __future__ import annotations

from typing import Any

from .graph import Graph

COL_W = 340
ROW_H = 230


def auto_layout(doc: dict[str, Any]) -> dict[str, Any]:
    """Layered left-to-right layout by longest path from a source node."""
    ids = [n["id"] for n in doc["nodes"]]
    g = Graph(ids, [(e["source"], e["target"]) for e in doc["edges"]])
    depth: dict[str, int] = {}
    for n in g.topo_order():
        depth[n] = max((depth[p] + 1 for p in g.pred[n]), default=0)
    rows: dict[int, int] = {}
    for node in doc["nodes"]:
        d = depth[node["id"]]
        node["position"] = {"x": float(d * COL_W), "y": float(rows.get(d, 0) * ROW_H)}
        rows[d] = rows.get(d, 0) + 1
    return doc


def _graph(name: str, description: str, nodes: list[tuple[str, str, str, dict[str, Any]]],
           edges: list[tuple[str, str, str, str]], variables: dict[str, str] | None = None) -> dict[str, Any]:
    doc = {
        "schema": 1, "name": name, "description": description, "variables": variables or {},
        "viewport": {"x": 40.0, "y": 40.0, "zoom": 0.7},
        "nodes": [{"id": nid, "type": ntype, "label": label, "notes": "", "position": {"x": 0.0, "y": 0.0},
                   "config": config, "disabled": False, "locked": False} for nid, ntype, label, config in nodes],
        "edges": [{"id": f"e{i + 1}", "source": s, "source_port": sp, "target": t, "target_port": tp}
                  for i, (s, sp, t, tp) in enumerate(edges)],
    }
    return auto_layout(doc)


FEMALE_VOICE = "preset:serena"
MALE_VOICE = "preset:aiden"


def _mva() -> dict[str, Any]:
    return _graph(
        "MVA video ad", "Script -> voice-over; script -> scene prompts -> images -> clips; voice, clips and "
        "music -> final vertical video with a call to action.",
        [
            ("brief", "text.input", "Brief", {"text": (
                "A 20-second Meta video ad for a motor-vehicle-accident claims service. A woman whose BMW was "
                "rear-ended at a traffic light gets calm, fast, friendly help with her claim.")}),
            ("script", "ai.script_writer", "Script Writer", {
                "model": "gx-auto", "format": "video_ad", "duration": 20, "scenes": 4,
                "tone": "trustworthy, calm, reassuring", "audience": "drivers after a car accident",
                "cta": "Call today for a free claim review."}),
            ("voice", "voice.tts", "Voice-over", {
                "voice_id": FEMALE_VOICE, "style": "warm, trustworthy, calm, clear American English",
                "language": "english"}),
            ("imgprompt", "ai.prompt_enhancer", "Image Prompt", {
                "model": "gx-auto", "target": "image",
                "style": "cinematic photo, 35mm, natural light, shallow depth of field, vertical framing"}),
            ("images", "image.generate", "Generate Image", {"size": "928x1664", "quality": "standard"}),
            ("clips", "video.i2v", "Image-to-Video", {
                "prompt": "slow cinematic camera push-in, subtle natural motion", "size": "480x832",
                "seconds": 5, "fps": 16}),
            ("join", "compose.concat", "Join clips", {"size": "480x832", "fps": 16}),
            ("music", "music.instrumental", "Background music", {
                "description": "subtle, hopeful, reassuring background music with soft piano and warm strings",
                "style_tags": ["cinematic", "piano", "ambient"], "duration": 30}),
            ("addvoice", "compose.add_voice", "Add voice", {"volume_db": 0, "fit": "longest"}),
            ("addmusic", "compose.add_music", "Add music", {"volume_db": -18, "fade_out": 1.5, "loop": True}),
            ("cta", "util.select", "CTA text", {"path": "cta"}),
            ("captions", "compose.captions", "Call to action", {"position": "bottom", "last_seconds": 4,
                                                                "font_size": 5}),
            ("export", "compose.export", "Final video", {"preset": "1080x1920", "quality": "standard"}),
        ],
        [
            ("brief", "text", "script", "brief"),
            ("script", "narration", "voice", "text"),
            ("script", "visuals", "imgprompt", "text"),
            ("imgprompt", "text", "images", "prompt"),
            ("images", "image", "clips", "image"),
            ("clips", "video", "join", "video"),
            ("join", "video", "addvoice", "video"),
            ("voice", "audio", "addvoice", "audio"),
            ("addvoice", "video", "addmusic", "video"),
            ("music", "audio", "addmusic", "audio"),
            ("script", "script", "cta", "json"),
            ("addmusic", "video", "captions", "video"),
            ("cta", "text", "captions", "text"),
            ("captions", "video", "export", "video"),
        ])


def _talking() -> dict[str, Any]:
    return _graph(
        "Talking character ad", "Character image -> animated clip; script -> voice; clip + voice -> final "
        "render. There is no lip-sync model on the cluster: the character moves naturally while the voice plays.",
        [
            ("character", "image.generate", "Character image", {
                "prompt": ("Friendly woman in her thirties, business casual, looking into the camera, soft studio "
                           "light, bright modern office background, photorealistic portrait"),
                "size": "928x1664"}),
            ("animate", "video.i2v", "Character video", {
                "prompt": "the woman talks to the camera with natural head movement and small hand gestures",
                "size": "480x832", "seconds": 5, "fps": 16}),
            ("script", "text.input", "Script", {"text": (
                "Hi! Looking for a smarter way to plan your week? Our app does it for you in seconds. "
                "Try it free today.")}),
            ("voice", "voice.tts", "Voice", {"voice_id": FEMALE_VOICE, "style": "friendly, upbeat, clear",
                                             "language": "english"}),
            ("mix", "compose.add_voice", "Add voice", {"fit": "longest"}),
            ("export", "compose.export", "Final render", {"preset": "1080x1920", "quality": "standard"}),
        ],
        [
            ("character", "image", "animate", "image"),
            ("script", "text", "voice", "text"),
            ("animate", "video", "mix", "video"),
            ("voice", "audio", "mix", "audio"),
            ("mix", "video", "export", "video"),
        ])


def _social() -> dict[str, Any]:
    return _graph(
        "Social ad pack", "Campaign prompt -> script -> three square images (kept as outputs) -> clips; "
        "voice-over and music -> one square video.",
        [
            ("campaign", "text.input", "Campaign prompt", {"text": (
                "Launch campaign for a reusable stainless-steel water bottle that keeps drinks cold for 24 hours. "
                "Audience: active young adults.")}),
            ("script", "ai.script_writer", "Script Writer", {
                "format": "social", "duration": 15, "scenes": 3, "tone": "energetic, positive",
                "cta": "Shop now."}),
            ("images", "image.generate", "Images", {"size": "1328x1328", "quality": "standard"}),
            ("stills", "image.output", "Image set", {}),
            ("voice", "voice.tts", "Voiceover", {"voice_id": MALE_VOICE, "style": "energetic, upbeat",
                                                 "language": "english"}),
            ("music", "music.instrumental", "Music", {
                "description": "upbeat, modern, positive pop instrumental", "style_tags": ["pop", "upbeat"],
                "duration": 20}),
            ("clips", "video.i2v", "Clips", {"prompt": "dynamic product shot, gentle camera orbit",
                                             "size": "640x640", "seconds": 5}),
            ("join", "compose.concat", "Join", {"size": "640x640", "fps": 16}),
            ("addvoice", "compose.add_voice", "Add voiceover", {"fit": "longest"}),
            ("addmusic", "compose.add_music", "Add music", {"volume_db": -16}),
            ("export", "compose.export", "Video", {"preset": "1080x1080"}),
        ],
        [
            ("campaign", "text", "script", "brief"),
            ("script", "visuals", "images", "prompt"),
            ("images", "image", "stills", "image"),
            ("script", "narration", "voice", "text"),
            ("images", "image", "clips", "image"),
            ("clips", "video", "join", "video"),
            ("join", "video", "addvoice", "video"),
            ("voice", "audio", "addvoice", "audio"),
            ("addvoice", "video", "addmusic", "video"),
            ("music", "audio", "addmusic", "audio"),
            ("addmusic", "video", "export", "video"),
        ])


def _voiceover() -> dict[str, Any]:
    return _graph(
        "Voiceover", "Script -> designed voice -> text-to-speech -> loudness normalisation -> audio output. "
        "Swap Voice Design for a Saved Voice node to reuse a voice.",
        [
            ("script", "text.input", "Script", {"text": (
                "Every great journey starts with a single step. Today, take yours with confidence.")}),
            ("design", "voice.design", "Voice Design", {
                "description": "A calm, trustworthy female narrator in her thirties with a warm, clear voice",
                "sample_text": "Hello, I will be your narrator today.", "name": "Flow narrator",
                "language": "english"}),
            ("tts", "voice.tts", "Text-to-Speech", {"style": "calm, confident", "language": "english"}),
            ("normalize", "compose.normalize", "Normalize", {"target_lufs": -16}),
            ("out", "sound.output", "Audio Output", {}),
        ],
        [
            ("script", "text", "tts", "text"),
            ("design", "voice", "tts", "voice"),
            ("tts", "audio", "normalize", "media"),
            ("normalize", "media", "out", "audio"),
        ])


def _music_video() -> dict[str, Any]:
    return _graph(
        "Music video", "Music + scene prompts -> images -> clips -> joined video with the track.",
        [
            ("idea", "text.input", "Song idea", {"text": (
                "A dreamy synthwave track about driving through a neon city at night.")}),
            ("music", "music.instrumental", "Music", {"style_tags": ["synthwave", "retro", "dreamy"],
                                                      "duration": 30}),
            ("scenes", "ai.scene_prompts", "Scene prompts", {"count": 4,
                                                             "style": "neon synthwave, cinematic, night"}),
            ("images", "image.generate", "Images", {"size": "1664x928"}),
            ("clips", "video.i2v", "Clips", {"prompt": "smooth forward camera motion, neon lights flicker",
                                             "size": "832x480", "seconds": 5}),
            ("join", "compose.concat", "Join", {"size": "832x480", "fps": 16}),
            ("addmusic", "compose.add_music", "Add music", {"volume_db": 0, "fade_out": 2, "loop": True,
                                                            "keep_original": False}),
            ("export", "compose.export", "Music video", {"preset": "1920x1080", "quality": "high"}),
        ],
        [
            ("idea", "text", "music", "description"),
            ("idea", "text", "scenes", "source"),
            ("scenes", "prompts", "images", "prompt"),
            ("images", "image", "clips", "image"),
            ("clips", "video", "join", "video"),
            ("join", "video", "addmusic", "video"),
            ("music", "audio", "addmusic", "audio"),
            ("addmusic", "video", "export", "video"),
        ])


BUILTINS: dict[str, tuple[str, str, Any]] = {
    "builtin_mva_video_ad": ("MVA video ad", "ads", _mva),
    "builtin_talking_character": ("Talking character ad", "ads", _talking),
    "builtin_social_ad_pack": ("Social ad pack", "ads", _social),
    "builtin_voiceover": ("Voiceover", "audio", _voiceover),
    "builtin_music_video": ("Music video", "music", _music_video),
}


def builtin_templates() -> list[dict[str, Any]]:
    out = []
    for tid, (name, category, fn) in BUILTINS.items():
        graph = fn()
        out.append({"id": tid, "name": name, "category": category, "description": graph["description"],
                    "graph": graph})
    return out
