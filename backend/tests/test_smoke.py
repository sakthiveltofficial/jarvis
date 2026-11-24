import os
import tempfile
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from jarvis import memory as gm  # noqa: E402
from jarvis.agent import Session  # noqa: E402


class GraphMemoryTest(unittest.TestCase):
    def setUp(self):
        self.m = gm.GraphMemory(os.path.join(tempfile.mkdtemp(), "g.db"))
        self.m.ensure_user("Alex")

    def test_save_recall_and_update(self):
        self.m.apply({"facts": [
            {"source": "Sam", "relation": "sibling_of", "target": "USER", "fact": "Sam is Alex's brother."},
            {"source": "Sam", "relation": "birthday_on", "target": "June 2", "fact": "Sam's birthday is June 2."},
            {"source": "USER", "relation": "lives_in", "target": "Chennai", "fact": "Alex lives in Chennai."},
        ]}, "ep")
        res = self.m.search("When is Sam's birthday?")
        self.assertIn("June 2", res["text"])
        old = [f for f in self.m.facts() if f["relation"] == "lives_in"][0]["id"]
        self.m.apply({"facts": [{"source": "USER", "relation": "lives_in", "target": "Bangalore",
                                 "fact": "Alex moved to Bangalore."}], "invalidate": [old]}, "ep2")
        current = [f["fact"] for f in self.m.facts()]
        self.assertIn("Alex moved to Bangalore.", current)
        self.assertNotIn("Alex lives in Chennai.", current)
        self.assertEqual(len(self.m.facts(valid_only=False)), 4)   # history kept

    def test_parse_json(self):
        self.assertEqual(gm.parse_json('```json\n{"facts": []}\n```'), {"facts": []})


class InterruptionRulesTest(unittest.TestCase):
    def test_rules(self):
        cases = {
            ("mm-hmm", ""): False, ("yeah okay", ""): False, ("", ""): False,
            ("wait", ""): True, ("can you open chrome instead", "the weather is sunny"): True,
            ("the weather is sunny", "the weather today is sunny and warm"): False,
        }
        for (heard, speaking), expected in cases.items():
            self.assertEqual(Session.is_interruption(heard, speaking)[0], expected, heard)


class ServerImportTest(unittest.TestCase):
    def test_app_routes(self):
        from jarvis import server
        paths = {r.path for r in server.app.routes}
        for p in ("/api/health", "/api/settings", "/api/graph", "/ws", "/api/agenda", "/api/skills", "/api/mcp"):
            self.assertIn(p, paths)


if __name__ == "__main__":
    unittest.main()
