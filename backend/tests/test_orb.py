"""Tests for the orb's mood tag: the model tags each reply, the agent strips it before showing or speaking."""
import os
import tempfile
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from jarvis import config, persona  # noqa: E402


class MoodTagTest(unittest.TestCase):
    def test_strips_tag_and_returns_mood(self):
        self.assertEqual(persona.split_mood("<mood>happy</mood> Done, the file is saved."),
                         ("Done, the file is saved.", "happy"))

    def test_bracket_form_and_case(self):
        self.assertEqual(persona.split_mood("[mood: Curious] Interesting, let me look."),
                         ("Interesting, let me look.", "curious"))

    def test_unknown_mood_is_removed_but_ignored(self):
        self.assertEqual(persona.split_mood("<mood>grumpy</mood>Fine."), ("Fine.", None))

    def test_no_tag_leaves_text_alone(self):
        self.assertEqual(persona.split_mood("  Hello there.  "), ("  Hello there.  ", None))

    def test_tag_only_reply_is_empty(self):
        self.assertEqual(persona.split_mood("<mood>calm</mood>"), ("", "calm"))

    def test_prompt_mentions_tag_only_when_orb_on(self):
        s = config.load()
        on = persona.system_prompt(s, True, ex={"mood_tag": True})
        off = persona.system_prompt(s, True, ex={"mood_tag": False})
        bg = persona.system_prompt(s, True, ex={"mood_tag": True, "background_agent": True})
        self.assertIn("<mood>", on)
        self.assertNotIn("<mood>", off)
        self.assertNotIn("<mood>", bg)

    def test_orb_defaults(self):
        self.assertTrue(config.DEFAULTS["ui"]["orb"])
        self.assertEqual(config.DEFAULTS["ui"]["orb_quality"], "auto")


if __name__ == "__main__":
    unittest.main()
