"""Tests for v0.4 features: scheduler, skills, MCP client, emotion, settings masking, wake regex, plans."""
import datetime as dt
import os
import sys
import tempfile
import textwrap
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

import numpy as np  # noqa: E402

from jarvis import config, emotion, scheduler, skills  # noqa: E402
from jarvis.agent import wake_regex  # noqa: E402
from jarvis.mcp_client import MCPManager, wait_ready  # noqa: E402


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        scheduler.FILE.unlink(missing_ok=True)
        self.fired = []
        self.s = scheduler.Scheduler({"briefings": {"morning": {"enabled": True, "time": "07:30"}}}, self.fired.append)

    def test_one_off_reminder_fires_once(self):
        j = self.s.add("Tea", "Your tea is ready", in_minutes=5)
        self.assertEqual(self.s.tick(dt.datetime.now()), [])
        later = dt.datetime.now() + dt.timedelta(minutes=6)
        self.assertEqual([f["id"] for f in self.s.tick(later)], [j["id"]])
        self.assertNotIn(j["id"], [x["id"] for x in self.s.list()])

    def test_weekly_and_daily(self):
        j = self.s.add("Downloads", "clean my Downloads folder", "task", repeat="weekly", time_="09:00", days=["mon"])
        nr = dt.datetime.fromisoformat(j["next_run"])
        self.assertEqual((nr.weekday(), nr.hour, nr.minute), (0, 9, 0))
        d = self.s.add("Stand", "stand up", repeat="daily", time_="6 pm")
        self.assertEqual(dt.datetime.fromisoformat(d["next_run"]).hour, 18)

    def test_briefing_builtin(self):
        b = [j for j in self.s.list() if j.get("builtin") == "morning"]
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["time"], "07:30")

    def test_past_rejected(self):
        with self.assertRaises(ValueError):
            self.s.add("x", "y", at="2001-01-01T10:00")


class SkillsTest(unittest.TestCase):
    def test_save_improve_find(self):
        s1 = skills.save("Export invoices", "Download last month's invoices", "1. Open portal\n2. Export")
        self.assertEqual(s1["version"], 1)
        s2 = skills.save("Export invoices", "", "1. Open portal\n2. Menu > Export", notes="button moved")
        self.assertEqual(s2["version"], 2)
        got = skills.get("invoices export")
        self.assertIn("Menu > Export", got["body"])
        self.assertIn("button moved", got["body"])
        self.assertEqual(int(got["uses"]), 1)
        self.assertIn("Export invoices", skills.prompt_index())


class MCPClientTest(unittest.TestCase):
    def test_stdio_server(self):
        srv = os.path.join(tempfile.mkdtemp(), "srv.py")
        with open(srv, "w") as f:
            f.write(textwrap.dedent('''
                import json, sys
                for line in sys.stdin:
                    m = json.loads(line)
                    if "id" not in m: continue
                    if m["method"] == "initialize":
                        r = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
                    elif m["method"] == "tools/list":
                        r = {"tools": [{"name": "get_time", "description": "time", "inputSchema": {"type": "object", "properties": {}}},
                                       {"name": "send_email", "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}}]}
                    elif m["method"] == "tools/call":
                        r = {"content": [{"type": "text", "text": "called " + m["params"]["name"]}]}
                    else:
                        r = {}
                    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
            '''))
        mgr = MCPManager()
        mgr.reload({"mcp": {"servers": [{"name": "Fake", "command": sys.executable, "args": [srv]}]}})
        self.assertTrue(wait_ready(mgr, 20))
        names = [s.name for s in mgr.specs()]
        self.assertEqual(names, ["mcp_fake_get_time", "mcp_fake_send_email"])
        self.assertIsNone(mgr.risk("mcp_fake_get_time", "risky"))           # read-only by name
        self.assertIsNotNone(mgr.risk("mcp_fake_send_email", "risky"))
        self.assertEqual(mgr.call("mcp_fake_send_email", {"to": "x"}).text, "called send_email")
        mgr.close()


class EmotionTest(unittest.TestCase):
    def _voice(self, f0, amp, secs=1.5):
        t = np.arange(int(16000 * secs)) / 16000
        return (amp * np.sin(2 * np.pi * f0 * t * (1 + 0.02 * np.sin(2 * np.pi * 3 * t)))).astype(np.float32)

    def test_relative_to_baseline(self):
        prof = {"n": 0}
        for _ in range(4):
            emotion.analyze(self._voice(120, 0.05), "just a normal day at work today", prof)
        calm = emotion.analyze(self._voice(120, 0.05), "just a normal day at work today", prof, update=False)
        self.assertEqual(calm["mood"], "neutral")
        hot = emotion.analyze(self._voice(170, 0.2), "ugh the build failed again and the deadline is today", prof, update=False)
        self.assertEqual(hot["mood"], "stressed")
        self.assertTrue(emotion.tone_note("Sam", hot).startswith("<tone>Sam sounds stressed"))


class MiscTest(unittest.TestCase):
    def test_wake_regex(self):
        r = wake_regex("Jarvis")
        self.assertEqual(r.sub("", "Hey Jarvis, what's the time?", count=1), "what's the time?")
        self.assertIsNone(r.search("tell me about jarvis"))

    def test_masking_roundtrip(self):
        s = config.load()
        s["mcp"] = {"servers": [{"name": "gh", "url": "u", "headers": {"Authorization": "Bearer secret-token-123"}}]}
        pub = config.public(s)
        masked = pub["mcp"]["servers"][0]["headers"]["Authorization"]
        self.assertTrue(masked.startswith("••••"))
        back = config.restore_masked(pub["mcp"]["servers"], s["mcp"]["servers"])
        self.assertEqual(back[0]["headers"]["Authorization"], "Bearer secret-token-123")


class SessionToolsTest(unittest.TestCase):
    """Session-level tools without audio hardware."""

    def test_plan_schedule_skills(self):
        import asyncio
        from jarvis import agent

        events = []

        async def emit(ev):
            events.append(ev)

        async def main():
            s = agent.Session(emit)
            try:
                out = s.plan_tool("main", {"action": "set", "title": "Tidy", "steps": ["a", "b"]})
                self.assertIn("0/2", out.text)
                s.plan_tool("main", {"action": "update", "step": 1, "status": "done"})
                self.assertIn("✓", s._plan_text("main"))
                r = s._t_schedule({"message": "stretch", "in_minutes": 30, "title": "Stretch"})
                self.assertIn("Stretch", r.text)
                self.assertIn("Stretch", s._t_list_schedules({}).text)
                prompt = s.system_prompt(False)
                self.assertIn("Coming up", prompt)
                self.assertIn("Current plan", prompt)
                ag = s.agenda()
                self.assertEqual(ag["plan"]["title"], "Tidy")
            finally:
                s.shutdown()
            await asyncio.sleep(0.05)

        asyncio.run(main())
        self.assertTrue(any(e["type"] == "plan" for e in events))


if __name__ == "__main__":
    unittest.main()
