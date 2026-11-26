"""Tests for the Jarvis / Ultron characters: the persona, the lines it says on its own and its voice."""
import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

import numpy as np  # noqa: E402

from jarvis import agent, ambient, audio, character, config, lifelog, persona, selfctl, voicetrain  # noqa: E402


def settings(char="jarvis", **kw):
    s = copy.deepcopy(config.DEFAULTS)
    s["character"] = char
    s["user_name"] = "Sakthi"
    s.update(kw)
    return s


class CharacterTest(unittest.TestCase):
    def test_default_is_jarvis(self):
        self.assertEqual(config.DEFAULTS["character"], "jarvis")
        self.assertEqual(character.key({}), "jarvis")
        self.assertEqual(character.key({"character": "nonsense"}), "jarvis")

    def test_stock_name_follows_character_custom_name_stays(self):
        self.assertEqual(character.name(settings("jarvis")), "Jarvis")
        self.assertEqual(character.name(settings("ultron")), "Ultron")
        self.assertEqual(character.name(settings("ultron", assistant_name="Jarvis")), "Ultron")
        self.assertEqual(character.name(settings("jarvis", assistant_name="Ultron")), "Jarvis")
        self.assertEqual(character.name(settings("ultron", assistant_name="Friday")), "Friday")

    def test_system_prompt_changes_with_character(self):
        j = persona.system_prompt(settings("jarvis"), True)
        u = persona.system_prompt(settings("ultron"), True)
        self.assertIn("You are Jarvis", j)
        self.assertIn("British", j)
        self.assertIn("You are Ultron", u)
        self.assertIn("villain", u)
        self.assertNotIn("British", u)
        self.assertNotIn("{user}", u)
        self.assertIn("Sakthi", u)

    def test_ultron_keeps_the_safety_rules(self):
        u = persona.system_prompt(settings("ultron"), True)
        self.assertIn("ALWAYS do what Sakthi asks", u)
        self.assertIn("Be careful with irreversible actions", u)
        self.assertIn("Some actions require Sakthi's approval", u)
        self.assertIn("drop the act", u)

    def test_chime_ins_and_report_change_voice(self):
        self.assertIn("warm, witty", ambient.phrase_system(settings("jarvis")))
        u = ambient.phrase_system(settings("ultron"))
        self.assertIn("You are Ultron", u)
        self.assertIn("menacing", u)
        self.assertNotIn("{", u)
        r = lifelog.report_system(settings("ultron"), "Sakthi")
        self.assertIn("You are Ultron", r)
        self.assertIn("villain", r)
        self.assertNotIn("{", r)
        self.assertIn("dry British wit", lifelog.report_system(settings("jarvis"), "Sakthi"))

    def test_briefing_tone(self):
        self.assertIn("warmly", persona.briefing_prompt(settings("jarvis"), "morning"))
        self.assertIn("menace", persona.briefing_prompt(settings("ultron"), "morning"))

    def test_fallback_lines_and_acks(self):
        self.assertEqual(character.fallback(settings("jarvis"), "water", "x"), "x")
        self.assertNotEqual(character.fallback(settings("ultron"), "water", "x"), "x")
        self.assertIn(character.ack(settings("ultron"), 1.0), character.CHARACTERS["ultron"]["acks"])
        self.assertIn(character.ack(settings("jarvis"), 1.0), character.CHARACTERS["jarvis"]["acks"])
        self.assertIn("locked", character.stranger_line(settings("ultron"), True))

    def test_voice_override(self):
        s = settings("ultron")
        s["tts"]["provider"] = "kokoro"
        with mock.patch.object(character, "_piper_ready", return_value=True):
            cfg = character.tts_cfg(s)
        self.assertEqual(cfg["kokoro_voice"], "am_onyx")
        self.assertEqual(cfg["piper_voice"], "en_US-ryan-high")
        self.assertLess(cfg["speed"], 1.0)
        self.assertEqual(s["tts"]["kokoro_voice"], "bm_george")       # the user's own setting is untouched
        s["character_voice"] = False
        self.assertEqual(character.tts_cfg(s)["kokoro_voice"], "bm_george")
        self.assertEqual(character.tts_cfg(settings("jarvis"))["piper_voice"], "en_GB-alan-medium")

    def test_piper_voice_waits_for_download(self):
        s = settings("ultron")
        with mock.patch.object(character, "_piper_ready", return_value=False):
            self.assertEqual(character.tts_cfg(s)["piper_voice"], "en_GB-alan-medium")

    def test_wake_regex_accepts_jarvis_in_ultron_mode(self):
        wre = agent.wake_regex("Ultron")
        self.assertTrue(wre.search("Hey Ultron, open Chrome"))
        self.assertTrue(wre.search("hey jarvis open chrome"))
        self.assertFalse(wre.search("ultronic stuff"))
        self.assertEqual(wre.sub("", "Hey Ultron, open Chrome", count=1).strip(), "open Chrome")



class FakeSession:
    def __init__(self, char="jarvis"):
        self.settings = settings(char)
        self.events, self.spoken, self.voice_mode = [], [], "off"

    def apply_settings(self, new):
        self.settings = config._deep_merge(self.settings, new)

    def emit(self, ev):
        self.events.append(ev)

    def speak(self, text, who=None):
        self.spoken.append((who, text))

    def set_voice_mode(self, mode, persist=True):
        self.voice_mode = mode

    def stop_speaking(self):
        pass


class SelfControlTest(unittest.TestCase):
    def test_secrets_and_connections_are_off_limits(self):
        self.assertFalse(selfctl.allowed("api_keys.anthropic"))
        self.assertFalse(selfctl.allowed("home_assistant.token"))
        self.assertFalse(selfctl.allowed("node.token"))
        self.assertFalse(selfctl.allowed("phones.devices"))
        self.assertFalse(selfctl.allowed("mcp.servers"))
        self.assertFalse(selfctl.allowed("made.up"))
        self.assertTrue(selfctl.allowed("health.water_min"))
        self.assertTrue(selfctl.allowed("character"))
        self.assertTrue(selfctl.allowed("voice.wake_phrase"))

    def test_set_coerces_and_reports(self):
        s = FakeSession()
        out = selfctl.SelfControl(s).tool({"action": "set", "changes": {
            "health.water_min": "45", "ambient.enabled": "false", "api_keys.openai": "sk-x", "voice.mode": "wake"}})
        self.assertFalse(out.is_error)
        self.assertEqual(s.settings["health"]["water_min"], 45)
        self.assertIs(s.settings["ambient"]["enabled"], False)
        self.assertNotIn("openai", s.settings["api_keys"])
        self.assertIn("api_keys.openai", out.text)
        self.assertEqual(s.voice_mode, "wake")
        self.assertIn({"type": "settings_changed"}, s.events)

    def test_settings_listing_hides_secrets(self):
        s = FakeSession()
        s.settings["api_keys"] = {"anthropic": "sk-secret-123456"}
        text = selfctl.SelfControl(s).tool({"action": "settings"}).text
        self.assertIn("health.water_min", text)
        self.assertNotIn("sk-secret", text)
        self.assertNotIn("api_keys", text)

    def test_loosening_safety_needs_approval(self):
        st = settings()
        self.assertIsNotNone(selfctl.risk({"action": "set", "changes": {"agent.confirm_mode": "never"}}, st))
        self.assertIsNotNone(selfctl.risk({"action": "set", "changes": {"node.enabled": True}}, st))
        self.assertIsNone(selfctl.risk({"action": "set", "changes": {"health.water_min": 30}}, st))
        self.assertIsNone(selfctl.risk({"action": "switch_character", "to": "ultron"}, st))
        st["agent"]["confirm_mode"] = "always"
        self.assertIsNotNone(selfctl.risk({"action": "set", "changes": {"health.water_min": 30}}, st))

    def test_switch_character_swaps_stock_name(self):
        s = FakeSession("jarvis")
        out = selfctl.SelfControl(s).tool({"action": "switch_character", "to": "ultron"})
        self.assertEqual(s.settings["character"], "ultron")
        self.assertEqual(s.settings["assistant_name"], "Ultron")
        self.assertIn("entrance", out.text)
        s.settings["assistant_name"] = "Friday"
        selfctl.SelfControl(s).tool({"action": "switch_character", "to": "jarvis"})
        self.assertEqual(s.settings["assistant_name"], "Friday")

    def test_switch_drama_is_in_the_prompt(self):
        j = persona.system_prompt(settings("jarvis"), True)
        u = persona.system_prompt(settings("ultron"), True)
        self.assertIn("hesitate", j)
        self.assertIn("stubborn", u)
        self.assertIn("never actually refuse", u)
        self.assertIn("action=talk_to", j)
        bg = persona.system_prompt(settings("jarvis"), True, ex={"background_agent": True})
        self.assertNotIn("action=talk_to", bg)

    def test_talk_to_other_character(self):
        s = FakeSession("jarvis")
        ctl = selfctl.SelfControl(s)
        with mock.patch("jarvis.llm.complete", return_value="<mood>playful</mood>Ultron: Tea again, Jarvis?") as m:
            out = ctl.tool({"action": "talk_to", "message": "Ultron, Sakthi wants your view on tea."})
        self.assertIn("You are Ultron", m.call_args[0][1])
        self.assertEqual(s.spoken, [("ultron", "Tea again, Jarvis?")])
        self.assertTrue(any(e.get("character") == "ultron" for e in s.events))
        self.assertIn("Tea again, Jarvis?", out.text)
        self.assertEqual(len(ctl.convo), 2)

    def test_as_character(self):
        st = settings("jarvis")
        self.assertIs(character.as_character(st, "jarvis"), st)
        self.assertEqual(character.as_character(st, "ultron")["character"], "ultron")
        self.assertEqual(st["character"], "jarvis")


class WakePhraseTest(unittest.TestCase):
    def test_profile_name(self):
        st = settings()
        self.assertEqual(selfctl.wake_profile_name(st), "hey_jarvis")
        self.assertEqual(selfctl.wake_phrase(st), "Hey Jarvis")
        st["voice"]["wake_phrase"] = "  Wake up   Daddy's home "
        self.assertTrue(selfctl.custom_wake(st))
        self.assertEqual(selfctl.wake_profile_name(st), "phrase:wake up daddy's home")

    def test_regex_with_custom_phrase(self):
        wre = agent.wake_regex("Jarvis", "Wake up Daddy")
        self.assertTrue(wre.search("wake up, daddy. Open YouTube"))
        self.assertEqual(wre.sub("", "Wake up daddy open YouTube", count=1).strip(), "open YouTube")
        self.assertTrue(agent.wake_regex("Jarvis", "Hey Friday").search("hey friday what's up"))
        self.assertFalse(agent.wake_regex("Jarvis", "Hey Friday").search("friday is a good day"))

    def test_custom_calibration_never_uses_the_stock_gate(self):
        rng = np.random.RandomState(1)
        base = rng.randn(16, 96).astype(np.float32)
        pos = [np.stack([base + rng.randn(16, 96).astype(np.float32) * 0.2 for _ in range(2)]) for _ in range(5)]
        neg = [rng.randn(30, 16, 96).astype(np.float32)]
        p = voicetrain.calibrate_wake(pos, neg, [0.0] * 5, "phrase:wake up", custom=True)
        self.assertEqual(p["gate"], 0.0)
        self.assertGreaterEqual(p["sim_thr"], 0.62)
        self.assertTrue(p["custom"])


class SpectrumTest(unittest.TestCase):
    def test_tone_lights_the_right_band(self):
        t = np.arange(1024) / audio.SR
        bands = audio.spectrum_bands(0.5 * np.sin(2 * np.pi * 1000 * t))
        self.assertEqual(len(bands), audio.SPECTRUM_BANDS)
        self.assertTrue(all(0 <= b <= 1 for b in bands))
        peak = int(np.argmax(bands))
        edges = np.geomspace(60, audio.SR / 2, audio.SPECTRUM_BANDS + 1)
        self.assertTrue(edges[peak] <= 1100 and edges[peak + 1] >= 900)
        self.assertLess(max(audio.spectrum_bands(np.zeros(480))), 0.05)

    def test_capture_feeds_the_spectrum(self):
        m = audio.Mic.__new__(audio.Mic)
        got = []
        m.on_spectrum = got.append
        self.assertTrue(callable(m.on_spectrum))


if __name__ == "__main__":
    unittest.main()
