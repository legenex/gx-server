"""Build with AI / Improve My Prompt / lyric writing (gx_control_ui.music_ai).

gx-auto is replaced by a scripted fake (unit tests) or a stub gateway on
127.0.0.1 (transport tests). Nothing reaches the real gateway.
"""

from __future__ import annotations

import copy
import json
import unittest

from support import StubUpstream

from gx_control_ui import music_ai as m

GOOD = {
    "title": "Golden Hour", "description": "A travel anthem about the savannah at dusk.",
    "style_tags": ["afro house", "female vocals", "dark", "uplifting"],
    "style_prompt": "Soulful female lead over rolling African percussion, warm deep bass, emotional chorus",
    "instrumental": False, "vocal_intent": "female", "vocal_language": "en",
    "lyrics": "[Verse]\nGolden light on the road\n[Chorus]\nTake me home",
    "bpm": 122, "key": "Am", "time_signature": "4/4", "duration": 150, "seed": None, "thinking": True,
    "inference_steps": 8, "infer_method": "ode", "lm_temperature": 0.85, "notes": "Afro house at 122.",
}


class FakeChat:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, messages, *, schema_name, schema, max_tokens, temperature):
        self.calls.append({"messages": copy.deepcopy(messages), "schema_name": schema_name, "schema": schema})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"content": reply if isinstance(reply, str) else json.dumps(reply), "model": "gx-auto"}


def ai(*replies, attempts=3):
    chat = FakeChat(*replies)
    sleeps: list[float] = []
    audits: list[dict] = []
    return m.MusicAI(chat, sleep=sleeps.append, attempts=attempts, audit=lambda **kw: audits.append(kw)), \
        chat, sleeps, audits


EMPTY_FORM = {"title": "", "description": "", "style_tags": [], "style_prompt": "", "instrumental": False,
              "vocal_intent": "auto", "vocal_language": "", "lyrics": "", "bpm": None, "key": None,
              "time_signature": None, "duration": None, "seed": 42, "thinking": True, "inference_steps": None,
              "infer_method": None, "lm_temperature": None}


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict_and_complete(self):
        schema = m.SETTINGS_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(set(m.FORM_FIELDS) | {"notes"}, set(schema["properties"]))
        self.assertEqual(schema["properties"]["vocal_intent"]["enum"], list(m.VOCAL_INTENTS))

    def test_valid_proposal(self):
        s, problems = m.validate_settings(GOOD)
        self.assertEqual(problems, [])
        self.assertEqual(s["key"], "A minor")
        self.assertEqual(s["time_signature"], "4/4")
        self.assertEqual(s["bpm"], 122)

    def test_problems_are_reported(self):
        cases = [
            ({"bpm": 20}, "bpm must be between"), ({"bpm": 120.5}, "whole number"), ({"bpm": True}, "number"),
            ({"key": "H dorian"}, "key 'H dorian'"), ({"time_signature": "5/4"}, "time_signature"),
            ({"duration": 5}, "duration"), ({"vocal_intent": "robot"}, "vocal_intent"),
            ({"vocal_language": "klingon"}, "vocal_language"), ({"infer_method": "euler"}, "infer_method"),
            ({"lyrics": "just words, no tags"}, "section tags"), ({"lyrics": "[Verse]\n[Chorus]"}, "only section tags"),
            ({"instrumental": True, "vocal_intent": "female"}, "instrumental must have vocal_intent"),
            ({"instrumental": True, "lyrics": "[Verse]\nla la", "vocal_intent": "auto"}, "empty lyrics"),
            ({"vocal_language": ""}, "vocal_language"), ({"style_prompt": "x" * 513}, "maximum is 512"),
            ({"style_prompt": "p" * 480, "style_tags": ["one tag here", "two tags here", "three tags"]}, "fit in 512"),
            ({"style_tags": "house"}, "list of strings"), ({"style_tags": ["x" * 49]}, "longer than 48"),
            ({"title": 5}, "title must be a string"), ({"style_prompt": "", "style_tags": []}, "give a style_prompt"),
            ({"thinking": "yes"}, "thinking must be"), ({"instrumental": "no"}, "instrumental must be"),
        ]
        for patch, expect in cases:
            with self.subTest(patch=patch):
                _, problems = m.validate_settings({**GOOD, **patch})
                self.assertTrue(any(expect in p for p in problems), problems)
        _, problems = m.validate_settings("not an object")
        self.assertEqual(problems, ["the answer is not a JSON object"])

    def test_instrumental_is_normalised(self):
        s, problems = m.validate_settings({**GOOD, "instrumental": True, "vocal_intent": "auto", "lyrics": "",
                                           "vocal_language": ""})
        self.assertEqual(problems, [])
        self.assertEqual((s["lyrics"], s["vocal_intent"], s["vocal_language"]), ("", "auto", ""))

    def test_tags_are_cleaned(self):
        s, _ = m.validate_settings({**GOOD, "style_tags": ["Deep, House", "deep  house", " ", "Piano\x00"]})
        self.assertEqual(s["style_tags"], ["Deep House", "Piano"])

    def test_normalize_form(self):
        self.assertEqual(m.normalize_form(EMPTY_FORM)["seed"], 42)
        for bad in ([], {"prompt": "x"}, {"bpm": "fast"}, {"style_tags": "x"}, {"description": "d" * 600}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.normalize_form(bad)

    def test_form_to_request(self):
        body = m.form_to_request({**m.validate_settings(GOOD)[0], "seed": 7})
        self.assertEqual(body["prompt"], GOOD["style_prompt"])
        self.assertEqual(body["vocal_intent"], "female")
        self.assertEqual(body["time_signature"], "4/4")
        self.assertEqual(body["seed"], 7)
        self.assertNotIn("notes", body)
        inst = m.form_to_request({**EMPTY_FORM, "instrumental": True, "vocal_intent": "female",
                                  "vocal_language": "en"})
        self.assertEqual(inst, {"instrumental": True, "seed": 42, "thinking": True})


class BuildTests(unittest.TestCase):
    def test_build_populates_every_unlocked_field(self):
        svc, chat, _, audits = ai(GOOD)
        out = svc.build("dark but uplifting Afro house for a travel ad, female vocal, 122 BPM",
                        EMPTY_FORM, [], write_lyrics=True, user="u")
        s = out["settings"]
        self.assertEqual(s["style_tags"], GOOD["style_tags"])
        self.assertEqual(s["key"], "A minor")
        self.assertEqual(s["seed"], 42)  # the proposal gave no seed: the user's stays
        self.assertEqual(s["lyrics"], GOOD["lyrics"])
        self.assertEqual(out["attempts"], 1)
        self.assertIn("set", out["summary"])
        req = chat.calls[0]
        self.assertEqual(req["schema_name"], "gx_music_settings")
        self.assertIn("Write lyrics: yes", req["messages"][-1]["content"])
        self.assertEqual(audits[-1]["action"], "music.ai.build")

    def test_locked_fields_are_kept(self):
        svc, chat, _, _ = ai(GOOD)
        form = {**EMPTY_FORM, "bpm": 100, "style_prompt": "my own words"}
        out = svc.build("afro house", form, ["bpm", "style_prompt"], write_lyrics=False, user="u")
        self.assertEqual((out["settings"]["bpm"], out["settings"]["style_prompt"]), (100, "my own words"))
        self.assertEqual(out["settings"]["lyrics"], "")  # lyrics not requested
        kept = {c["field"] for c in out["changes"] if c["action"] == "kept_locked"}
        self.assertEqual(kept, {"bpm", "style_prompt"})
        self.assertIn('"bpm": 100', chat.calls[0]["messages"][-1]["content"])

    def test_invalid_answer_is_retried_with_feedback(self):
        svc, chat, sleeps, _ = ai("not json", {**GOOD, "bpm": 900}, GOOD)
        out = svc.build("house track with vocals", EMPTY_FORM, [], write_lyrics=True, user="u")
        self.assertEqual(out["attempts"], 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertIn("not valid JSON", chat.calls[1]["messages"][-1]["content"])
        self.assertIn("bpm must be between", chat.calls[2]["messages"][-1]["content"])

    def test_gives_up_after_three_attempts(self):
        svc, _, _, _ = ai("x", "y", "z")
        with self.assertRaises(m.MusicAIError) as cm:
            svc.build("house", EMPTY_FORM, [], write_lyrics=False, user="u")
        self.assertEqual((cm.exception.status, cm.exception.code), (502, "ai_invalid"))

    def test_vocal_request_cannot_come_back_instrumental(self):
        inst = {**GOOD, "instrumental": True, "vocal_intent": "auto", "lyrics": "", "vocal_language": ""}
        svc, chat, _, _ = ai(inst, GOOD)
        out = svc.build("a ballad with female vocals", EMPTY_FORM, [], write_lyrics=True, user="u")
        self.assertFalse(out["settings"]["instrumental"])
        self.assertIn("asks for vocals", chat.calls[1]["messages"][-1]["content"])

    def test_missing_lyrics_when_requested_is_retried(self):
        svc, chat, _, _ = ai({**GOOD, "lyrics": ""}, GOOD)
        svc.build("pop song", EMPTY_FORM, [], write_lyrics=True, user="u")
        self.assertIn("lyrics are required", chat.calls[1]["messages"][-1]["content"])

    def test_last_attempt_is_repaired_when_only_lengths_are_off(self):
        long_tags = {**GOOD, "style_prompt": "p " * 240, "style_tags": [f"tag number {i}" for i in range(10)]}
        svc, _, _, _ = ai(long_tags, long_tags, long_tags)
        out = svc.build("pop", EMPTY_FORM, [], write_lyrics=True, user="u")
        self.assertLessEqual(m.caption_length(out["settings"]["style_prompt"], out["settings"]["style_tags"]), 512)
        self.assertTrue(any("dropped" in n for n in out["repair_notes"]))

    def test_transport_errors(self):
        busy = m.MusicAIError("busy", 503, "gateway_busy")
        svc, _, sleeps, _ = ai(busy, GOOD)
        svc.build("house", EMPTY_FORM, [], write_lyrics=False, user="u")
        self.assertEqual(sleeps, [0.5])
        svc, _, _, _ = ai(m.MusicAIError("refused", 502, "gateway_error"))
        with self.assertRaises(m.MusicAIError) as cm:
            svc.build("house", EMPTY_FORM, [], write_lyrics=False, user="u")
        self.assertEqual(cm.exception.code, "gateway_error")

    def test_input_validation_and_limits(self):
        svc, _, _, _ = ai(GOOD, GOOD, GOOD)
        for bad in ("", "hi", 5, "x" * 1201):
            with self.subTest(bad=str(bad)[:10]), self.assertRaises(m.MusicAIError) as cm:
                svc.build(bad, EMPTY_FORM, [], write_lyrics=False, user="u")
            self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(ValueError):
            svc.build("house", EMPTY_FORM, ["seed", "api_key"], write_lyrics=False, user="u")
        with self.assertRaises(ValueError):
            svc.build("house", EMPTY_FORM, "bpm", write_lyrics=False, user="u")
        svc._busy.add("u2")
        with self.assertRaises(m.MusicAIError) as cm:
            svc.build("house", EMPTY_FORM, [], write_lyrics=False, user="u2")
        self.assertEqual((cm.exception.status, cm.exception.code), (429, "busy"))

    def test_rate_limit(self):
        svc, _, _, _ = ai(*([GOOD] * 11))
        for _ in range(m.MusicAI.RATE_PER_MIN):
            svc.build("house", EMPTY_FORM, [], write_lyrics=False, user="r")
        with self.assertRaises(m.MusicAIError) as cm:
            svc.build("house", EMPTY_FORM, [], write_lyrics=False, user="r")
        self.assertEqual(cm.exception.code, "rate_limited")
        svc.build("house", EMPTY_FORM, [], write_lyrics=False, user="someone-else")


class ImproveTests(unittest.TestCase):
    USER = {**EMPTY_FORM, "description": "a song about rain", "style_tags": ["lo-fi", "piano"],
            "style_prompt": "soft piano", "bpm": 80, "vocal_intent": "male", "lyrics": "[Verse]\nrain on glass",
            "vocal_language": "en"}
    PROPOSAL = {**GOOD, "title": "Rainfall", "description": "A tender song about rain on a city window.",
                "style_tags": ["lo-fi", "jazzy chords", "vinyl crackle"], "style_prompt": "warm felt piano, brushes",
                "bpm": 76, "key": "F major", "vocal_intent": "female", "instrumental": True, "seed": 9,
                "lyrics": "", "vocal_language": ""}

    def merged(self, locked=(), improve_lyrics=False, current=None, proposal=None):
        cur = m.normalize_form(current or self.USER)
        prop = m.validate_settings(proposal or self.PROPOSAL, strict=False)[0]
        return m.merge_improvement(cur, prop, set(locked), improve_lyrics=improve_lyrics)

    def test_merge_rules(self):
        s, changes = self.merged()
        by = {(c["field"], c["action"]) for c in changes}
        self.assertEqual(s["title"], "Rainfall")                                   # empty -> filled
        self.assertEqual(s["description"], self.PROPOSAL["description"])            # refined
        self.assertEqual(s["style_prompt"], "warm felt piano, brushes")            # refined
        self.assertEqual(s["style_tags"], ["lo-fi", "piano", "jazzy chords", "vinyl crackle"])  # union
        self.assertIn(("style_tags", "suggested"), by)                              # "piano" dropped: only suggested
        self.assertEqual(s["bpm"], 80)                                              # user's value kept
        self.assertIn(("bpm", "suggested"), by)
        self.assertEqual(s["key"], "F major")                                       # empty -> filled
        self.assertFalse(s["instrumental"])                                         # never flipped
        self.assertIn(("instrumental", "suggested"), by)
        self.assertEqual(s["vocal_intent"], "male")                                 # explicit choice kept
        self.assertEqual(s["seed"], 42)                                             # never changed
        self.assertEqual(s["lyrics"], "[Verse]\nrain on glass")                      # not asked to rewrite
        before = {c["field"]: c["before"] for c in changes if c["action"] == "refined"}
        self.assertEqual(before["description"], "a song about rain")

    def test_locked_fields_are_never_changed(self):
        s, changes = self.merged(locked=["description", "style_tags", "key", "title"])
        self.assertEqual(s["description"], "a song about rain")
        self.assertEqual(s["style_tags"], ["lo-fi", "piano"])
        self.assertIsNone(s["key"])
        self.assertEqual(s["title"], "")
        self.assertEqual({c["field"] for c in changes if c["action"] == "kept_locked"},
                         {"description", "style_tags", "key", "title"})

    def test_lyrics_are_rewritten_only_on_request(self):
        prop = {**self.PROPOSAL, "instrumental": False, "lyrics": "[Verse]\nnew words", "vocal_language": "en"}
        s, _ = self.merged(proposal=prop)
        self.assertEqual(s["lyrics"], "[Verse]\nrain on glass")
        s, _ = self.merged(proposal=prop, improve_lyrics=True)
        self.assertEqual(s["lyrics"], "[Verse]\nnew words")
        s, _ = self.merged(proposal=prop, improve_lyrics=True, locked=["lyrics"])
        self.assertEqual(s["lyrics"], "[Verse]\nrain on glass")

    def test_auto_vocal_type_is_filled(self):
        s, _ = self.merged(current={**self.USER, "vocal_intent": "auto"},
                           proposal={**self.PROPOSAL, "instrumental": False, "vocal_language": "en"})
        self.assertEqual(s["vocal_intent"], "female")

    def test_improve_end_to_end(self):
        svc, chat, _, audits = ai({**self.PROPOSAL, "instrumental": False, "vocal_language": "en",
                                   "lyrics": "[Verse]\nrain on glass"})
        out = svc.improve(self.USER, ["bpm"], improve_lyrics=False, instruction="make it jazzier", user="u")
        self.assertEqual(out["settings"]["bpm"], 80)
        self.assertIn("make it jazzier", chat.calls[0]["messages"][-1]["content"])
        self.assertNotIn('"seed"', chat.calls[0]["messages"][-1]["content"])
        self.assertTrue(out["changes"])
        self.assertEqual(audits[-1]["action"], "music.ai.improve")
        with self.assertRaises(m.MusicAIError) as cm:
            svc.improve(EMPTY_FORM, [], improve_lyrics=False, instruction="", user="u")
        self.assertEqual(cm.exception.code, "nothing_to_improve")

    def test_no_changes_summary(self):
        self.assertEqual(m.summarize([]), "No changes: the settings already match.")


class LyricsAndSuggestTests(unittest.TestCase):
    def test_write_lyrics(self):
        svc, chat, _, _ = ai({"lyrics": "no tags"}, {"lyrics": "[Verse]\nhello there\n[Chorus]\nsing"})
        text = svc.write_lyrics({"description": "a song about leaving", "prompt": "cinematic",
                                 "style_tags": ["piano"], "vocal_intent": "female", "duration": 60}, user="u")
        self.assertEqual(text, "[Verse]\nhello there\n[Chorus]\nsing")
        brief = chat.calls[0]["messages"][-1]["content"]
        self.assertIn("female vocals", brief)
        self.assertIn("cinematic, piano", brief)
        self.assertEqual(chat.calls[0]["schema_name"], "gx_music_lyrics")
        with self.assertRaises(m.MusicAIError) as cm:
            svc.write_lyrics({"style_tags": []}, user="u")
        self.assertEqual(cm.exception.status, 400)

    def test_suggest_never_returns_lyrics_or_seed(self):
        svc, chat, _, _ = ai({**GOOD, "seed": 5})
        out = svc.suggest({"measured": {"tempo_bpm": 120}}, hint="darker", user="u")
        self.assertEqual(out["settings"]["lyrics"], "")
        self.assertIsNone(out["settings"]["seed"])
        prompt = chat.calls[0]["messages"][-1]["content"]
        self.assertIn("never name the artist", prompt)
        self.assertIn("darker", prompt)


class GatewayTransportTests(unittest.TestCase):
    def test_request_shape_and_errors(self):
        answer = {"choices": [{"message": {"content": json.dumps(GOOD)}, "finish_reason": "stop"}],
                  "model": "gx-auto", "usage": {"total_tokens": 9}}
        stub = StubUpstream({("POST", "/v1/chat/completions"): (200, answer)})
        try:
            chat = m.GatewayChat(stub.url, lambda: {"Authorization": "Bearer test-gateway-credential"})
            out = chat.complete([{"role": "user", "content": "x"}], schema_name="gx_music_settings",
                                schema=m.SETTINGS_SCHEMA, max_tokens=100, temperature=0.5)
            self.assertEqual(json.loads(out["content"])["bpm"], 122)
            _, path, headers, body = stub.calls[-1]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer test-gateway-credential")
            self.assertEqual(body["model"], "gx-auto")
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            stub.routes[("POST", "/v1/chat/completions")] = (503, {"error": "overloaded"})
            with self.assertRaises(m.MusicAIError) as cm:
                chat.complete([], schema_name="x", schema={}, max_tokens=1, temperature=0)
            self.assertEqual(cm.exception.status, 503)
            stub.routes[("POST", "/v1/chat/completions")] = (400, {"error": "bad"})
            with self.assertRaises(m.MusicAIError) as cm:
                chat.complete([], schema_name="x", schema={}, max_tokens=1, temperature=0)
            self.assertEqual(cm.exception.code, "gateway_error")
        finally:
            stub.close()
        dead = m.GatewayChat("http://127.0.0.1:9", dict)
        with self.assertRaises(m.MusicAIError) as cm:
            dead.complete([], schema_name="x", schema={}, max_tokens=1, temperature=0)
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "gateway_unavailable"))


if __name__ == "__main__":
    unittest.main()
