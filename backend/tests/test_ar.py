"""AR companion (phone camera): object finder matching, sightings, smart-home overlay calls and the phone request path."""
import asyncio
import base64
import io
import json
import os
import tempfile
import time
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from jarvis import ar, config, llm, phones  # noqa: E402


def jpeg_b64(color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


class FakeProvider:
    vision = True


class FakeHome:
    def __init__(self):
        self.calls = []
        self._areas = {"light.desk": "Office"}
        self._at = 0.0
        self.on = False

    def devices(self, domain="", query=""):
        return [{"entity_id": "light.desk", "name": "Desk lamp", "domain": "light", "state": "on" if self.on else "off",
                 "area": "Office"},
                {"entity_id": "lock.front", "name": "Front door", "domain": "lock", "state": "locked", "area": ""}]

    def states(self, force=False):
        return [{"entity_id": "light.desk", "state": "on" if self.on else "off",
                 "attributes": {"friendly_name": "Desk lamp", "brightness": 128}},
                {"entity_id": "lock.front", "state": "locked", "attributes": {"friendly_name": "Front door"}}]

    def risk(self, a):
        return "unlock Front door" if a.get("device") == "lock.front" else None

    def _apply(self, dev, action, a):
        self.calls.append((dev["entity_id"], action, a))
        self.on = action != "turn_off"
        return f"{dev['name']} {action}"


class FakeSession:
    def __init__(self):
        self.settings = config.load()
        self.settings["llm"]["vision"] = True
        self.settings["home_assistant"] = {"url": "http://ha.local:8123", "token": "x"}
        self.provider = FakeProvider()
        self.home = FakeHome()
        self.events = []
        self.context_notes = []
        self.loop = asyncio.new_event_loop()
        self.handled = []

    def emit(self, ev):
        self.events.append(ev)

    async def handle_user(self, text, origin=None, images=None, context=""):
        self.handled.append((text, origin, images, context))


class MatchTest(unittest.TestCase):
    def test_head_noun_must_match(self):
        self.assertGreaterEqual(ar.matches("where did I leave my keys?", "keys"), 0.7)
        self.assertGreaterEqual(ar.matches("car keys", "key ring"), 0.7)
        self.assertGreaterEqual(ar.matches("my glasses", "sunglasses"), 0.7)
        self.assertEqual(ar.matches("car keys", "toy car"), 0.0)
        self.assertEqual(ar.matches("where is my wallet", "laptop"), 0.0)
        self.assertGreater(ar.matches("tv remote", "TV remote control"), ar.matches("tv remote", "remote"))

    def test_box_and_json(self):
        self.assertEqual(ar._box([0.5, 0.6, 0.1, 0.2]), [0.1, 0.2, 0.5, 0.6])
        self.assertIsNone(ar._box([1, 2]))
        self.assertEqual(ar.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ar.parse_json("sure! {\"objects\": []} ok"), {"objects": []})


class HubTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        self.hub = ar.ARHub(self.s)
        self.hub.sightings = []
        self.hub.notes = {}
        self.dev = {"id": "dev-1", "name": "Pixel"}
        self._complete = llm.complete

    def tearDown(self):
        llm.complete = self._complete
        self.s.loop.close()

    def test_sighting_then_find(self):
        llm.complete = lambda *a, **k: json.dumps({"scene": "home office desk", "objects": [
            {"name": "keys", "where": "on the desk, left of the laptop", "box": [0.1, 0.5, 0.2, 0.6]},
            {"name": "mug", "where": "next to the monitor", "box": [0.6, 0.4, 0.7, 0.55]}]})
        r = self.hub.handle(self.dev, "sighting", {"jpeg": jpeg_b64(), "place": "Office"})
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["objects"]), 2)
        out = self.hub.handle(self.dev, "find", {"query": "where did I leave my keys"})
        self.assertEqual(out["hits"][0]["object"], "keys")
        self.assertIn("left of the laptop", out["text"])
        self.assertTrue(out["hits"][0]["thumb"])
        t = self.hub.tool({"action": "find", "object": "keys"})
        self.assertFalse(t.is_error)
        self.assertTrue(t.image_b64)
        self.assertIn("haven't seen", self.hub.tool({"action": "find", "object": "passport"}).text)
        self.assertIn("keys", self.hub.tool({"action": "recent"}).text)

    def test_old_sightings_are_pruned(self):
        self.hub.sightings = [{"id": "old", "at": time.time() - 30 * 86400, "objects": [{"name": "keys"}]}]
        with self.hub._lock:
            self.hub._save_sightings()
        self.assertEqual(self.hub.sightings, [])

    def test_look_routes_to_agent_with_photo(self):
        r = self.hub.handle(self.dev, "look", {"jpeg": jpeg_b64(), "question": "what's this error?"})
        self.assertTrue(r["ok"])
        self.s.loop.run_until_complete(asyncio.sleep(0.05))
        text, origin, images, context = self.s.handled[0]
        self.assertEqual(text, "what's this error?")
        self.assertEqual(origin, "dev-1")
        self.assertEqual(len(images), 1)
        self.assertIn("phone camera", context)

    def test_look_without_vision(self):
        self.s.provider.vision = False
        with self.assertRaises(RuntimeError):
            self.hub.handle(self.dev, "look", {"jpeg": jpeg_b64()})

    def test_scan_devices_keeps_known_entities(self):
        llm.complete = lambda *a, **k: json.dumps({"devices": [
            {"entity_id": "light.desk", "box": [0.2, 0.1, 0.4, 0.5], "confidence": 0.9},
            {"entity_id": "light.made_up", "box": [0, 0, 1, 1]}]})
        r = self.hub.handle(self.dev, "scan_devices", {"jpeg": jpeg_b64()})
        self.assertEqual([d["entity_id"] for d in r["devices"]], ["light.desk"])
        self.assertEqual(r["devices"][0]["attrs"]["brightness_pct"], 50)

    def test_ha_call_and_risky_refused(self):
        r = self.hub.handle(self.dev, "ha_call", {"entity_id": "light.desk", "action": "turn_on", "brightness": 40})
        self.assertTrue(r["ok"])
        self.assertEqual(self.s.home.calls[0], ("light.desk", "turn_on", {"brightness": 40}))
        self.assertEqual(r["state"]["state"], "on")
        with self.assertRaises(PermissionError):
            self.hub.handle(self.dev, "ha_call", {"entity_id": "lock.front", "action": "unlock"})

    def test_notes_sync_and_tool(self):
        self.hub.handle(self.dev, "notes_sync", {"notes": [
            {"id": "n1", "text": "Buy milk", "place": "fridge", "remind": True}, {"id": "n2", "text": ""}]})
        self.assertEqual(len(self.hub.all_notes()), 1)
        self.assertIn("Buy milk", self.hub.tool({"action": "notes"}).text)
        self.hub.handle(self.dev, "note_seen", {"id": "n1"})
        self.assertIn("Buy milk", self.s.context_notes[-1])

    def test_unknown_op(self):
        with self.assertRaises(ValueError):
            self.hub.handle(self.dev, "nope", {})


class ProviderImagesTest(unittest.TestCase):
    def test_anthropic_user_photo_pruned(self):
        p = llm.AnthropicProvider.__new__(llm.AnthropicProvider)
        p.history, p.vision = [], True
        for i in range(5):
            p.add_user(f"look {i}", ["AAAA"])
        with_img = [m for m in p.history if any(b.get("type") == "image" for b in m["content"])]
        self.assertEqual(len(with_img), llm.AnthropicProvider.KEEP_IMAGES)
        self.assertEqual(p.history[0]["content"][0]["text"], "look 0")

    def test_openai_user_photo_keeps_text(self):
        p = llm.OpenAICompatProvider.__new__(llm.OpenAICompatProvider)
        p.history, p.vision = [], True
        p.add_user("first", ["AAAA"])
        p.add_user("second", ["BBBB"])
        p.add_user("third", ["CCCC"])
        self.assertEqual(p.history[0]["content"], "first [older image removed]")
        self.assertIsInstance(p.history[2]["content"], list)


class PhoneRequestTest(unittest.TestCase):
    def test_req_resp_over_the_link(self):
        sent = []

        class Conn:
            device = {"id": "dev-1", "name": "Pixel"}

        class S:
            ar = type("A", (), {"handle": staticmethod(lambda d, op, a: {"echo": op, "args": a})})()

        hub = phones.PhoneHub.__new__(phones.PhoneHub)
        hub.s = S()

        async def safe_send(c, obj):
            sent.append(obj)
        hub._safe_send = safe_send
        hub._submit = lambda coro: asyncio.new_event_loop().run_until_complete(coro)
        hub._serve(Conn(), S.ar, {"id": "r1", "op": "status", "args": {"x": 1}})
        self.assertEqual(sent[0], {"t": "resp", "id": "r1", "ok": True, "data": {"echo": "status", "args": {"x": 1}}})
        S.ar.handle = staticmethod(lambda d, op, a: (_ for _ in ()).throw(RuntimeError("no vision")))
        hub._serve(Conn(), S.ar, {"id": "r2", "op": "look"})
        self.assertEqual(sent[1], {"t": "resp", "id": "r2", "ok": False, "error": "no vision"})
        self.assertIn("ai_mood", phones.FORWARD)


if __name__ == "__main__":
    unittest.main()
