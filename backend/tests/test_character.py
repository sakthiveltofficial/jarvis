"""Tests for the Jarvis / Ultron characters: the persona, the lines it says on its own and its voice."""
import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from jarvis import agent, ambient, character, config, lifelog, persona  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
