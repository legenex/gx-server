"""Creative Flows (FLO): schema, typed edges, graph algorithms, hashing,
templates, AI graph validation, FFmpeg argument builders and the netguard
use of the HTTP nodes. Pure unit tests (no network, no docker)."""

from __future__ import annotations

import copy
import json
import threading
import unittest
from pathlib import Path

from support import TempEnv

from gx_control_ui.flows import ai
from gx_control_ui.flows import catalog as cat
from gx_control_ui.flows import ffmpeg as ff
from gx_control_ui.flows import hashing, nodes, schema
from gx_control_ui.flows.graph import CycleError, Graph
from gx_control_ui.flows.services import NodeFailure, SecretStore, parse_json_answer
from gx_control_ui.flows.templates import auto_layout, builtin_templates
from gx_control_ui.netguard import BlockedURL, FetchResult


def node(nid: str, ntype: str, **config) -> dict:
    return {"id": nid, "type": ntype, "config": config, "position": {"x": 0, "y": 0}}


def edge(eid: str, s: str, sp: str, t: str, tp: str) -> dict:
    return {"id": eid, "source": s, "source_port": sp, "target": t, "target_port": tp}


def doc(nodes_: list, edges_: list, **extra) -> dict:
    return {"name": "Test", "nodes": nodes_, "edges": edges_, **extra}


class CatalogTests(unittest.TestCase):
    def test_every_available_node_has_an_executor(self) -> None:
        self.assertEqual(nodes.check_registry(), [])

    def test_unavailable_nodes_explain_why(self) -> None:
        unavailable = {t: n for t, n in cat.NODES.items() if not n.available}
        self.assertIn("image.remove_bg", unavailable)
        self.assertIn("sound.sfx", unavailable)
        for n in unavailable.values():
            self.assertGreater(len(n.unavailable_reason), 30)

    def test_public_catalogue_is_json_and_live_overrides(self) -> None:
        pub = cat.catalog_public({"voice.tts": "gx-voice is down"})
        json.dumps(pub)
        tts = next(n for n in pub["nodes"] if n["type"] == "voice.tts")
        self.assertFalse(tts["available"])
        self.assertEqual(tts["unavailable_reason"], "gx-voice is down")
        self.assertEqual({c["id"] for c in pub["categories"]}, {n["category"] for n in pub["nodes"]})

    def test_upscale_is_honestly_labelled(self) -> None:
        up = cat.NODES["image.upscale"]
        self.assertIn("NOT AI", up.description)
        self.assertIn("lanczos", up.backend.lower())


class SchemaTests(unittest.TestCase):
    def test_valid_chain_normalises(self) -> None:
        d = schema.validate_document(doc(
            [node("p", "text.prompt", template="a cat"), node("g", "image.generate"),
             node("v", "video.i2v", seconds=4)],
            [edge("e1", "p", "text", "g", "prompt"), edge("e2", "g", "image", "v", "image")]))
        self.assertEqual(d["schema"], 1)
        self.assertEqual(d["nodes"][2]["config"], {"seconds": 4.0})
        self.assertFalse(d["nodes"][0]["locked"])

    def test_rejects_nonsensical_edge(self) -> None:
        with self.assertRaises(schema.FlowValidationError) as cm:
            schema.validate_document(doc(
                [node("a", "sound.upload", asset_id="a_" + "0" * 24), node("g", "image.generate")],
                [edge("e1", "a", "audio", "g", "prompt")]))
        self.assertEqual(cm.exception.issues[0]["code"], "type_mismatch")
        self.assertIn("accepts text, not audio", cm.exception.issues[0]["message"])

    def test_image_to_video_source_and_json_needs_conversion(self) -> None:
        ok = doc([node("i", "image.upload", asset_id="a_" + "1" * 24), node("v", "video.i2v")],
                 [edge("e", "i", "image", "v", "image")])
        schema.validate_document(ok)
        bad = doc([node("t", "text.input", text="x"), node("s", "util.select", path="a")],
                  [edge("e", "t", "text", "s", "json")])
        with self.assertRaises(schema.FlowValidationError):
            schema.validate_document(bad)

    def test_any_ports_resolve_through_utilities(self) -> None:
        d = doc([node("t", "text.input", text="x"), node("d", "util.delay", seconds=1),
                 node("g", "image.generate")],
                [edge("e1", "t", "text", "d", "value"), edge("e2", "d", "value", "g", "prompt")])
        schema.validate_document(d)
        bad = copy.deepcopy(d)
        bad["nodes"][0] = node("t", "image.upload", asset_id="a_" + "2" * 24)
        bad["edges"][0]["source_port"] = "image"
        with self.assertRaises(schema.FlowValidationError):
            schema.validate_document(bad)

    def test_file_input_type_comes_from_its_kind(self) -> None:
        d = doc([node("f", "util.file_input", asset_type="audio", asset_id="a_" + "3" * 24),
                 node("m", "compose.merge_audio")], [edge("e", "f", "file", "m", "audio")])
        schema.validate_document(d)
        d["nodes"][0]["config"]["asset_type"] = "image"
        with self.assertRaises(schema.FlowValidationError):
            schema.validate_document(d)

    def test_cycle_detected(self) -> None:
        with self.assertRaises(schema.FlowValidationError) as cm:
            schema.validate_document(doc(
                [node("a", "util.delay"), node("b", "util.delay")],
                [edge("e1", "a", "value", "b", "value"), edge("e2", "b", "value", "a", "value")]))
        self.assertEqual(cm.exception.issues[-1]["code"], "cycle")

    def test_structural_rejections(self) -> None:
        cases = [
            (doc([node("a", "nope")], []), "unknown_type"),
            (doc([node("a b", "text.input")], []), "bad_id"),
            (doc([node("a", "text.input"), node("a", "text.input")], []), "duplicate"),
            (doc([node("a", "text.input", text="x", colour="red")], []), "unknown_field"),
            (doc([node("a", "text.input", text=5)], []), "invalid"),
            (doc([node("a", "image.generate", count=9)], []), "invalid"),
            (doc([node("a", "image.generate", count=1.5)], []), "invalid"),
            (doc([node("a", "image.generate", quality="ultra")], []), "invalid"),
            (doc([node("a", "image.upload", asset_id="../../etc/passwd")], []), "invalid"),
            (doc([node("a", "text.input", text="x"), node("b", "image.generate")],
                 [edge("e", "a", "text", "b", "negative")]), "bad_port"),
            (doc([node("a", "text.input", text="x"), node("b", "image.generate")],
                 [edge("e", "a", "image", "b", "prompt")]), "bad_port"),
            (doc([node("a", "text.input", text="x"), node("b", "image.generate")],
                 [edge("e", "a", "text", "b", "prompt"), edge("e2", "a", "text", "b", "prompt")]), "duplicate"),
            (doc([node("a", "text.input", text="x"), node("c", "text.input", text="y"),
                  node("b", "image.generate")],
                 [edge("e", "a", "text", "b", "prompt"), edge("e2", "c", "text", "b", "prompt")]), "port_full"),
            (doc([node("a", "util.webhook", url="https://x.example",
                       headers=[{"header": "Host", "secret": "tok"}])], []), "invalid"),
            (doc([node("a", "text.input", text="x")], [], variables={"bad name": "x"}), "invalid"),
            (doc([node("a", "text.input", text="x")], [], viewport={"x": 0, "y": 0, "zoom": 99}), "invalid"),
        ]
        for d, code in cases:
            with self.subTest(code=code), self.assertRaises(schema.FlowValidationError) as cm:
                schema.validate_document(d)
            self.assertIn(code, {i["code"] for i in cm.exception.issues})

    def test_self_loop_and_limits(self) -> None:
        with self.assertRaises(schema.FlowValidationError) as cm:
            schema.validate_document(doc([node("a", "util.delay")], [edge("e", "a", "value", "a", "value")]))
        self.assertEqual(cm.exception.issues[0]["code"], "self_loop")
        many = doc([node(f"n{i}", "text.input", text="x") for i in range(schema.MAX_NODES + 1)], [])
        with self.assertRaises(schema.FlowValidationError):
            schema.validate_document(many)
        self.assertRaises(schema.FlowValidationError, schema.validate_document, ["not", "a", "dict"])

    def test_check_edge(self) -> None:
        d = schema.validate_document(doc([node("t", "text.input", text="x"), node("g", "image.generate"),
                                          node("m", "compose.merge_audio")], []))
        self.assertIsNone(schema.check_edge(d, "t", "text", "g", "prompt"))
        self.assertIn("accepts audio", schema.check_edge(d, "t", "text", "m", "audio") or "")

    def test_readiness(self) -> None:
        d = schema.validate_document(doc(
            [node("g", "image.generate"), node("v", "video.i2v"), node("u", "image.upload"),
             node("x", "image.remove_bg"), node("t", "voice.tts", text="hi")], []))
        codes = {(i["node_id"], i["code"]) for i in schema.readiness(d)}
        self.assertIn(("g", "missing_input"), codes)   # prompt field empty, nothing connected
        self.assertIn(("v", "missing_input"), codes)   # start image required
        self.assertIn(("u", "missing_field"), codes)   # asset not chosen
        self.assertIn(("x", "unavailable"), codes)
        self.assertIn(("t", "missing_input"), codes)   # no voice chosen or connected
        d["nodes"][0]["config"]["prompt"] = "a lighthouse"
        d["nodes"][0]["disabled"] = False
        self.assertNotIn(("g", "missing_input"), {(i["node_id"], i["code"]) for i in schema.readiness(d)})
        self.assertEqual(schema.readiness(d, only={"g"}), [])


class GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        #   a -> b -> d
        #   a -> c -> d -> e
        self.g = Graph("abcde", [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("d", "e")])

    def test_topo_and_closures(self) -> None:
        order = self.g.topo_order()
        self.assertEqual(order[0], "a")
        self.assertEqual(order[-1], "e")
        self.assertLess(order.index("b"), order.index("d"))
        self.assertEqual(self.g.upstream("d"), {"a", "b", "c"})
        self.assertEqual(self.g.downstream("b"), {"d", "e"})

    def test_cycle_members(self) -> None:
        g = Graph("xyzw", [("x", "y"), ("y", "z"), ("z", "x"), ("z", "w")])
        with self.assertRaises(CycleError) as cm:
            g.topo_order()
        self.assertEqual(sorted(cm.exception.nodes), ["x", "y", "z"])

    def test_plans(self) -> None:
        self.assertEqual(self.g.plan("full"), (set("abcde"), set()))
        self.assertEqual(self.g.plan("node", "d"), ({"a", "b", "c", "d"}, set()))
        self.assertEqual(self.g.plan("regenerate", "b"), ({"a", "b"}, {"b"}))
        self.assertEqual(self.g.plan("from", "c"), (set("abcde"), {"c", "d", "e"}))
        self.assertEqual(self.g.plan("downstream", "d"), (set("abcde"), {"e"}))
        self.assertEqual(self.g.plan("rerun_failed", failed=["c"])[0], set("abcde"))
        self.assertRaises(ValueError, self.g.plan, "downstream", "e")
        self.assertRaises(ValueError, self.g.plan, "rerun_failed", failed=[])
        self.assertRaises(ValueError, self.g.plan, "node", None)
        self.assertRaises(ValueError, self.g.plan, "bogus")

    def test_runnable_set(self) -> None:
        states = {n: "pending" for n in "abcde"}
        members = set("abcde")
        self.assertEqual(self.g.runnable(states, members), ["a"])
        states["a"] = "succeeded"
        self.assertEqual(self.g.runnable(states, members), ["b", "c"])
        states.update(b="running", c="failed")
        self.assertEqual(self.g.runnable(states, members), [])
        states["b"] = "cached"
        self.assertEqual(self.g.runnable(states, members), ["d"])
        self.assertEqual(self.g.runnable({"d": "pending"}, {"d"}), ["d"])


class HashingTests(unittest.TestCase):
    def test_canonical_and_stable(self) -> None:
        self.assertEqual(hashing.canonical({"b": 1.0, "a": [2, True]}), '{"a":[2,true],"b":1}')
        self.assertRaises(ValueError, hashing.canonical, float("nan"))
        k1 = hashing.node_key("image.generate", 1, {"prompt": "x", "title": "A"}, {"gx-image": "r1"},
                              {"prompt": [{"type": "text", "text": "hi"}]}, {})
        k2 = hashing.node_key("image.generate", 1, {"title": "B", "prompt": "x"}, {"gx-image": "r1"},
                              {"prompt": [{"type": "text", "text": "hi"}]}, {})
        self.assertEqual(k1, k2, "presentation-only fields do not change the key")

    def test_changes_invalidate(self) -> None:
        base = dict(node_type="image.generate", type_version=1, config={"prompt": "x"}, identity={"a": "1"},
                    inputs={"image": [{"type": "image", "asset_id": "a_1"}]}, asset_sha={"a_1": "s1"})
        k = hashing.node_key(**base)
        for change in ({"type_version": 2}, {"config": {"prompt": "y"}}, {"identity": {"a": "2"}},
                       {"asset_sha": {"a_1": "s2"}}, {"variables": {"v": "1"}}):
            with self.subTest(change=change):
                self.assertNotEqual(k, hashing.node_key(**{**base, **change}))


class TemplateTests(unittest.TestCase):
    def test_builtins_are_valid_and_runnable(self) -> None:
        ids = set()
        for tpl in builtin_templates():
            ids.add(tpl["id"])
            clean = schema.validate_document(tpl["graph"])
            self.assertEqual(schema.readiness(clean), [], tpl["id"])
            for n in clean["nodes"]:
                self.assertTrue(cat.NODES[n["type"]].available, (tpl["id"], n["type"]))
        self.assertEqual(ids, {"builtin_mva_video_ad", "builtin_talking_character", "builtin_social_ad_pack",
                               "builtin_voiceover", "builtin_music_video",
                               "builtin_image_voiceover", "builtin_ai_campaign"})

    def test_template_voices_are_real_presets(self) -> None:
        import sys
        voice_dir = Path(__file__).resolve().parents[2] / "voice"
        sys.path.insert(0, str(voice_dir))
        try:
            from gx_voice.validation import SPEAKERS
        except ImportError:  # pragma: no cover - gx-voice source not present
            self.skipTest("gx-voice source not available")
        finally:
            sys.path.remove(str(voice_dir))
        for tpl in builtin_templates():
            for n in tpl["graph"]["nodes"]:
                vid = n["config"].get("voice_id")
                if vid:
                    self.assertTrue(vid.startswith("preset:") and vid.split(":", 1)[1] in SPEAKERS, vid)

    def test_auto_layout_is_layered(self) -> None:
        d = auto_layout(doc([node("a", "text.input"), node("b", "image.generate"), node("c", "video.i2v")],
                            [edge("1", "a", "text", "b", "prompt"), edge("2", "b", "image", "c", "image")]))
        xs = [n["position"]["x"] for n in d["nodes"]]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(len(set(xs)), 3)


class _LLM:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[list[dict]] = []

    def chat(self, model, messages, **kw):  # noqa: ANN001
        self.calls.append(messages)
        return self.answers.pop(0), {"model_requested": model, "model_used": "m", "usage": {}}


class AIGraphTests(unittest.TestCase):
    VOICES = [{"id": "preset:serena", "name": "Serena"}]

    def test_valid_answer_is_normalised(self) -> None:
        from flows_support import DEFAULT_AI_GRAPH
        llm = _LLM([json.dumps(DEFAULT_AI_GRAPH)])
        out = ai.generate(llm, "Create a 30-second MVA Meta ad", voices=self.VOICES)
        self.assertEqual(out["attempts"], 1)
        self.assertTrue(any("bogus_setting" in w for w in out["warnings"]))
        g = out["graph"]
        self.assertEqual(len(g["nodes"]), 10)
        self.assertEqual(out["readiness"], [])
        xs = {n["id"]: n["position"]["x"] for n in g["nodes"]}
        self.assertLess(xs["brief"], xs["final"])
        system = llm.calls[0][0]["content"] + llm.calls[0][1]["content"]
        self.assertIn("compose.add_voice", system)
        self.assertNotIn("image.remove_bg", llm.calls[0][1]["content"])

    def test_invalid_answer_is_repaired(self) -> None:
        bad = {"name": "x", "nodes": [{"id": "a", "type": "sound.upload", "config": {}},
                                      {"id": "g", "type": "image.generate", "config": {"prompt": "p"}}],
               "edges": [{"source": "a", "source_port": "audio", "target": "g", "target_port": "prompt"}]}
        good = {"name": "x", "nodes": [{"id": "g", "type": "image.generate", "config": {"prompt": "p"}}],
                "edges": []}
        llm = _LLM(["not json at all", json.dumps(bad), json.dumps(good)])
        out = ai.generate(llm, "an image", voices=[])
        self.assertEqual(out["attempts"], 3)
        self.assertIn("not valid", llm.calls[2][-1]["content"])
        self.assertIn("accepts text, not audio", llm.calls[2][-1]["content"])

    def test_still_invalid_edges_are_removed_with_warnings(self) -> None:
        bad = {"name": "x", "nodes": [{"id": "a", "type": "sound.upload", "config": {}},
                                      {"id": "g", "type": "image.generate", "config": {"prompt": "p"}}],
               "edges": [{"source": "a", "source_port": "audio", "target": "g", "target_port": "prompt"}]}
        out = ai.generate(_LLM([json.dumps(bad)] * 3), "x", voices=[])
        self.assertEqual(out["graph"]["edges"], [])
        self.assertTrue(any("removed" in w for w in out["warnings"]))

    def test_unknown_types_and_voices_are_dropped(self) -> None:
        answer = {"name": "x", "nodes": [{"id": "z", "type": "made.up", "config": {}},
                                         {"id": "bg", "type": "image.remove_bg", "config": {}},
                                         {"id": "t", "type": "voice.tts", "config": {"voice_id": "preset:nobody",
                                                                                      "text": "hi"}}],
                  "edges": [{"source": "z", "source_port": "x", "target": "t", "target_port": "text"}]}
        out = ai.generate(_LLM([json.dumps(answer)]), "x", voices=self.VOICES)
        self.assertEqual([n["type"] for n in out["graph"]["nodes"]], ["voice.tts"])
        self.assertNotIn("voice_id", out["graph"]["nodes"][0]["config"])
        self.assertEqual(out["readiness"][0]["code"], "missing_input")

    def test_guards(self) -> None:
        self.assertRaises(NodeFailure, ai.generate, _LLM([]), "", voices=[])
        self.assertRaises(NodeFailure, ai.generate, _LLM([]), "x" * 3000, voices=[])
        self.assertRaises(NodeFailure, ai.generate, _LLM([]), "x", model="gx-max", voices=[])
        with self.assertRaises(NodeFailure):
            ai.generate(_LLM(["{}", "[]", "nope"]), "x", voices=[])

    def test_parse_json_answer(self) -> None:
        self.assertEqual(parse_json_answer('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_answer('Sure! {"a": [1]} done'), {"a": [1]})
        self.assertRaises(ValueError, parse_json_answer, "nothing")


def media(kind: str, **kw) -> ff.Media:
    defaults = {"video": dict(duration=3.0, width=832, height=480, has_audio=False),
                "audio": dict(duration=4.0, has_audio=True), "image": dict(width=256, height=256)}[kind]
    ext = {"video": "mp4", "audio": "wav", "image": "png"}[kind]
    return ff.Media(path=Path(f"/tmp/x.{ext}"), ext=ext, kind=kind, **{**defaults, **kw})


class FFmpegBuilderTests(unittest.TestCase):
    def test_argument_arrays_only_reference_mounts(self) -> None:
        jobs = [ff.merge_audio([media("audio"), media("audio")], 0.5), ff.mix_audio([media("audio")] * 3),
                ff.add_audio(media("video"), media("audio"), role="voice", fit="longest"),
                ff.add_audio(media("video", has_audio=True), media("audio"), role="music", loop=True, fade_out=2),
                ff.add_audio(media("video"), media("audio"), role="sfx", offset=1.5),
                ff.trim(media("video"), 0.5, 2), ff.trim(media("audio"), 1, None), ff.fade(media("video"), "in", 1),
                ff.fade(media("audio"), "out", 1), ff.volume(media("audio"), -3), ff.normalize(media("audio"), -16),
                ff.resize(media("image"), 512, 300, "cover"), ff.resize(media("video"), 721, 1281, "contain"),
                ff.crop(media("video"), "9:16", "center"), ff.upscale_image(media("image"), 2),
                ff.last_frame(media("video")), ff.concat([media("video"), media("video", has_audio=True)]),
                ff.overlay(media("video"), media("image"), "bottom-left", 20, 0.5, 1, 2),
                ff.captions(media("video"), "Call 0800 now; 100% free!", "bottom", 6),
                ff.subtitles(media("video"), "One. Two!", 22), ff.export(media("video"), media("audio"),
                                                                         "1080x1920", "high", 30)]
        for job in jobs:
            with self.subTest(job=job.describe):
                self.assertTrue(all(isinstance(a, str) for a in job.args))
                self.assertIn("main", job.outputs)
                for a in job.args:
                    if a.startswith("/"):
                        self.assertTrue(a.startswith(("/in/", "/out/")), a)
                self.assertNotIn("0800", " ".join(job.args), "user text never enters the filter graph")

    def test_even_sizes_and_crop_box(self) -> None:
        job = ff.resize(media("video"), 721, 1281, "contain")
        self.assertIn("scale=720:1280", " ".join(job.args))
        self.assertEqual(ff.crop_box(832, 480, "9:16", "center"), (270, 480, 281, 0))
        self.assertEqual(ff.crop_box(1000, 1000, "16:9", "start"), (1000, 562, 0, 0))
        w, h, x, y = ff.crop_box(640, 640, "4:5", "end")
        self.assertEqual((w, h, x + w), (512, 640, 640))

    def test_captions_text_goes_to_a_file(self) -> None:
        job = ff.captions(media("video"), "It's 100% %{pts} free: call now", "top", 8, 1, 3, box=False)
        self.assertIn("textfile=/in/caption.txt:expansion=none", " ".join(job.args))
        self.assertIn("between(t,1.000,3.000)", " ".join(job.args))
        self.assertIn("%{pts}", job.files["caption.txt"])

    def test_srt_generation(self) -> None:
        srt = ff.script_to_srt("Hello there. How are you? Fine!", 6.0)
        self.assertEqual(srt.count("-->"), 3)
        self.assertTrue(srt.startswith("1\n00:00:00,000 --> "))
        self.assertIn("00:00:06,000", srt)
        raw = "1\n00:00:00,000 --> 00:00:01,000\nHi\n"
        self.assertEqual(ff.script_to_srt(raw, 5), raw)

    def test_refusals(self) -> None:
        with self.assertRaises(ff.ComposeError):
            ff.trim(media("video"), 5, None)
        with self.assertRaises(ff.ComposeError):
            ff.volume(media("video"), 3)  # no audio track
        with self.assertRaises(ff.ComposeError):
            ff.upscale_image(media("image", width=3000, height=100), 2)
        with self.assertRaises(ff.ComposeError):
            ff.normalize(media("audio"), -5)
        with self.assertRaises(ff.ComposeError):
            ff.concat([media("audio")])
        with self.assertRaises(ff.ComposeError):
            ff.add_audio(media("video", duration=None), media("audio"), role="voice")
        with self.assertRaises(ff.ComposeError):
            ff.export(media("video"), None, "8k", "high", None)

    def test_runner_command_is_locked_down(self) -> None:
        env = TempEnv()
        try:
            src = env.root / "in.mp4"
            src.write_bytes(b"x" * 100)
            runner = ff.FFmpegRunner(env.root)
            job = ff.captions(ff.Media(path=src, ext="mp4", kind="video", duration=2, width=640, height=480),
                              "hi", "bottom", 6)
            work = env.root / "w"
            (work / "in").mkdir(parents=True)
            cmd = runner.command(job, work, "gx-flow-ffmpeg-test")
            for flag in ("--network", "none", "--cap-drop", "ALL", "--memory", "--pids-limit"):
                self.assertIn(flag, cmd)
            self.assertIn(f"{src.resolve()}:/in/0.mp4:ro", cmd)
            self.assertIn(f"{(work / 'in' / 'caption.txt').resolve()}:/in/caption.txt:ro", cmd)
            bad = ff.FFJob([ff.Media(path=src, ext="mp4;rm", kind="video")], [], {"main": "x"}, "video")
            self.assertRaises(ff.ComposeError, runner.command, bad, work, "n")
            disabled = ff.FFmpegRunner(env.root, enabled=False)
            self.assertRaises(ff.ComposeError, disabled.run, job)
        finally:
            env.cleanup()


class _Ctx:
    """Just enough of NodeContext for the utility executors."""


class UtilityNodeTests(unittest.TestCase):
    def ctx(self, ntype: str, config: dict, inputs: dict, variables: dict | None = None) -> nodes.NodeContext:
        from gx_control_ui.flows.services import Services
        services = Services(library=None, store=None, llm=None, ffmpeg=None, secrets=None)  # type: ignore[arg-type]
        nt = cat.NODES[ntype]
        full = {f.id: f.default for f in nt.fields if f.default is not None}
        full.update(config)
        return nodes.NodeContext(services=services, run_id="frun_x", flow_id="flow_x", flow_name="F",
                                 node_id="n", node={"id": "n", "type": ntype}, nt=nt, config=full, inputs=inputs,
                                 connected=set(inputs), variables=variables or {}, user="u", owner="ui",
                                 cancel=threading.Event())

    @staticmethod
    def t(text: str) -> dict:
        return {"type": "text", "text": text}

    def test_prompt_renders_variables_and_input(self) -> None:
        r = nodes.EXECUTORS["text.prompt"](self.ctx("text.prompt", {"template": "{{brand}}: {{input}}"},
                                                    {"context": [self.t("a car")]}, {"brand": "GX"}))
        self.assertEqual(r.outputs["text"][0]["text"], "GX: a car")

    def test_combine_select_batch_iterator(self) -> None:
        r = nodes.EXECUTORS["text.combine"](self.ctx("text.combine", {"separator": " | "},
                                                     {"parts": [self.t("a"), self.t("b")]}))
        self.assertEqual(r.outputs["text"][0]["text"], "a | b")
        data = {"type": "json", "data": {"scenes": [{"v": "one"}, {"v": "two"}]}}
        r = nodes.EXECUTORS["util.select"](self.ctx("util.select", {"path": "scenes[1].v"}, {"json": [data]}))
        self.assertEqual(r.outputs["text"][0]["text"], "two")
        r = nodes.EXECUTORS["util.select"](self.ctx("util.select", {"path": "scenes", "each": True},
                                                    {"json": [data]}))
        self.assertEqual(len(r.outputs["text"]), 2)
        with self.assertRaises(NodeFailure):
            nodes.EXECUTORS["util.select"](self.ctx("util.select", {"path": "missing"}, {"json": [data]}))
        r = nodes.EXECUTORS["util.batch"](self.ctx("util.batch", {"count": 3}, {"text": [self.t("cat")]}))
        self.assertEqual([v["text"] for v in r.outputs["text"]],
                         ["cat, variation 1", "cat, variation 2", "cat, variation 3"])
        r = nodes.EXECUTORS["util.iterator"](self.ctx("util.iterator", {"split": "lines"},
                                                      {"value": [self.t("- one\n\n* two\nthree")]}))
        self.assertEqual([v["text"] for v in r.outputs["text"]], ["one", "two", "three"])
        r = nodes.EXECUTORS["util.iterator"](self.ctx("util.iterator", {"split": "json"},
                                                      {"value": [{"type": "json", "data": ["x", {"y": 1}]}]}))
        self.assertEqual([v["text"] for v in r.outputs["text"]], ["x", '{"y": 1}'])

    def test_branching(self) -> None:
        cond = nodes.EXECUTORS["util.conditional"]
        yes = cond(self.ctx("util.conditional", {"test": "contains", "argument": "BMW"},
                            {"value": [self.t("a bmw crash")]}))
        self.assertEqual(list(yes.outputs), ["yes"])
        no = cond(self.ctx("util.conditional", {"test": "longer", "argument": "50"}, {"value": [self.t("short")]}))
        self.assertEqual(list(no.outputs), ["no"])
        router = nodes.EXECUTORS["util.router"]
        r = router(self.ctx("util.router", {"route1": "music", "route2": "video"}, {"value": [self.t("a VIDEO")]}))
        self.assertEqual(list(r.outputs), ["route2"])
        r = router(self.ctx("util.router", {"match_on": "variable", "variable": "kind", "route1": "ad"},
                            {"value": [self.t("x")]}, {"kind": "ad"}))
        self.assertEqual(list(r.outputs), ["route1"])

    def test_dialogue_parsing(self) -> None:
        segs = nodes.parse_dialogue("ANNA: Hi!\n\nben: Hello", {"Anna": "preset:serena", "Ben": "preset:aiden"})
        self.assertEqual(segs, [{"voice_id": "preset:serena", "text": "Hi!"},
                                {"voice_id": "preset:aiden", "text": "Hello"}])
        self.assertRaises(NodeFailure, nodes.parse_dialogue, "no colon here", {})
        self.assertRaises(NodeFailure, nodes.parse_dialogue, "CARL: hi", {"Anna": "x"})

    def test_select_path_parser(self) -> None:
        self.assertEqual(nodes.select_path({"a": [{"b": 5}]}, "a[0].b"), 5)
        self.assertEqual(nodes.select_path([1, 2], "[1]"), 2)
        self.assertRaises(ValueError, nodes.select_path, {"a": 1}, "a..b")
        self.assertRaises(KeyError, nodes.select_path, {"a": []}, "a[3]")


class HttpNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.secrets = SecretStore(self.env.root / "flows" / "http-secrets.json")
        self.secrets.set("hook_token", "s3cr3t-value-123")
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        self.env.cleanup()

    def fetch(self, url, *, method="GET", body=None, headers=None, timeout=15.0, max_bytes=0):  # noqa: ANN001
        from gx_control_ui.netguard import check_url
        check_url(url)
        self.calls.append({"url": url, "method": method, "headers": headers, "body": body})
        return FetchResult(url, 200, {"content-type": "application/json"},
                           json.dumps({"echo": headers.get("Authorization")}).encode())

    def ctx(self, ntype: str, config: dict, inputs: dict) -> nodes.NodeContext:
        from gx_control_ui.flows.services import Services
        services = Services(library=None, store=None, llm=None, ffmpeg=None, secrets=self.secrets,  # type: ignore[arg-type]
                            fetch=self.fetch)
        nt = cat.NODES[ntype]
        full = {f.id: f.default for f in nt.fields if f.default is not None}
        full.update(config)
        return nodes.NodeContext(services=services, run_id="frun_x", flow_id="flow_x", flow_name="F",
                                 node_id="n", node={"id": "n", "type": ntype}, nt=nt, config=full, inputs=inputs,
                                 connected=set(inputs), variables={}, user="u", owner="ui",
                                 cancel=threading.Event())

    def test_webhook_uses_secret_and_never_echoes_it(self) -> None:
        cfg = {"url": "https://hooks.example.com/x", "headers": [{"header": "Authorization",
                                                                   "secret": "hook_token"}]}
        r = nodes.EXECUTORS["util.webhook"](self.ctx("util.webhook", cfg, {"value": [{"type": "text",
                                                                                       "text": "done"}]}))
        self.assertEqual(self.calls[0]["headers"]["Authorization"], "s3cr3t-value-123")
        dumped = json.dumps({"out": r.outputs, "payload": r.payload})
        self.assertNotIn("s3cr3t-value-123", dumped)
        self.assertIn("<secret hook_token>", dumped)
        sent = json.loads(self.calls[0]["body"])
        self.assertEqual(sent["items"], [{"type": "text", "text": "done"}])
        self.assertEqual(self.secrets.names()[0]["name"], "hook_token")
        self.assertNotIn("value", self.secrets.names()[0])

    def test_blocked_urls_and_missing_secret(self) -> None:
        for url in ("http://127.0.0.1/x", "http://192.168.100.11:18800/v1", "http://gx10-02/", "file:///etc/passwd",
                    "https://user:pw@example.com/", "http://100.105.214.61:4000/v1"):
            with self.subTest(url=url), self.assertRaises(NodeFailure) as cm:
                nodes.EXECUTORS["util.api_request"](self.ctx("util.api_request", {"url": url}, {}))
            self.assertEqual(cm.exception.code, "blocked_url")
        with self.assertRaises(NodeFailure) as cm:
            nodes.EXECUTORS["util.api_request"](self.ctx(
                "util.api_request", {"url": "https://api.example.com",
                                     "headers": [{"header": "X-Key", "secret": "nope"}]}, {}))
        self.assertEqual(cm.exception.code, "missing_secret")

    def test_secret_store_file_mode_and_validation(self) -> None:
        mode = (self.env.root / "flows" / "http-secrets.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        from gx_control_ui.flows.store import FlowError
        self.assertRaises(FlowError, self.secrets.set, "bad name", "x")
        self.assertRaises(FlowError, self.secrets.set, "ok", "line\nbreak")
        self.assertTrue(self.secrets.delete("hook_token"))
        self.assertFalse(self.secrets.delete("hook_token"))

    def test_netguard_blocks_before_fetch(self) -> None:
        from gx_control_ui.netguard import fetch
        with self.assertRaises(BlockedURL):
            fetch("http://169.254.169.254/latest/meta-data", resolver=lambda h, p: "169.254.169.254")


if __name__ == "__main__":
    unittest.main()
