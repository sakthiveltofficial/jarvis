"""Phone hub: pairing handshake, encrypted framing, tool calls and confirmation rules (no real phone needed)."""
import base64
import json
import os
import tempfile
import threading
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

from jarvis import config, phones, tools  # noqa: E402


class FakeSession:
    def __init__(self):
        self.settings = config.load()
        self.settings["phones"] = {"enabled": True, "port": 0, "devices": [], "extra_hosts": ["10.0.0.5"]}
        self.state = "idle"
        self.events = []
        self.loop = None

    def emit(self, ev):
        self.events.append(ev)


def parse(uri):
    from urllib.parse import parse_qs, urlparse
    q = {k: v[0] for k, v in parse_qs(urlparse(uri).query).items()}
    k = q["k"] + "=" * (-len(q["k"]) % 4)
    return q, base64.urlsafe_b64decode(k)


class PhoneHubTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        self.hub = phones.PhoneHub(self.s)
        self.hub.start = lambda: setattr(self.hub, "port", 8767)   # don't bind a real socket in tests
        self.client = TestClient(self.hub._app())
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_crypto_roundtrip_and_binding(self):
        k = os.urandom(32)
        f = phones.seal(k, {"a": 1}, b"c2s|x")
        self.assertEqual(phones.unseal(k, f, b"c2s|x"), {"a": 1})
        with self.assertRaises(Exception):
            phones.unseal(k, f, b"s2c|x")      # reflected frame is rejected

    def _pair(self, ws, uri, device="dev-1"):
        q, secret = parse(uri)
        hello = json.loads(ws.receive_text())
        self.assertEqual(hello["t"], "hello")
        ch = base64.b64decode(hello["challenge"])
        box = phones.seal(secret, {"challenge": hello["challenge"], "name": "Pixel", "model": "Google Pixel 8"}, b"auth|" + ch)
        ws.send_text(json.dumps({"t": "auth", "mode": "pair", "device": device, "pid": q["pid"], "box": box}))
        w = json.loads(ws.receive_text())
        self.assertEqual(w["t"], "welcome")
        body = phones.unseal(secret, w["box"], b"welcome|" + ch)
        return base64.b64decode(body["device_key"]), ch, body

    def test_pair_call_and_reconnect(self):
        pairing = self.hub.new_pairing()
        self.assertTrue(pairing["uri"].startswith("jarvis://pair?"))
        self.assertIn("10.0.0.5", pairing["uri"])
        with self.client.websocket_connect("/phone") as ws:
            key, ch, body = self._pair(ws, pairing["uri"])
            self.assertIn("10.0.0.5", body["hosts"])
            self.assertEqual(len(self.hub.devices()), 1)
            self.assertNotIn("key", config.public(self.s.settings)["phones"]["devices"][0])
            # the agent calls a phone action from a worker thread; the "phone" answers
            out = {}
            t = threading.Thread(target=lambda: out.update(r=self.hub.tool({"action": "device_info"})))
            t.start()
            call = phones.unseal(key, json.loads(ws.receive_text()), b"s2c|" + ch)
            self.assertEqual((call["t"], call["action"]), ("call", "device_info"))
            res = {"t": "result", "id": call["id"], "text": "Battery: 80%", "is_error": False, "seq": 1}
            ws.send_text(json.dumps(phones.seal(key, res, b"c2s|" + ch)))
            t.join(5)
            self.assertIn("Battery: 80%", out["r"].text)
            self.assertFalse(out["r"].is_error)
        # the pairing code is single-use
        with self.client.websocket_connect("/phone") as ws:
            hello = json.loads(ws.receive_text())
            q, secret = parse(pairing["uri"])
            c2 = base64.b64decode(hello["challenge"])
            box = phones.seal(secret, {"challenge": hello["challenge"]}, b"auth|" + c2)
            ws.send_text(json.dumps({"t": "auth", "mode": "pair", "device": "dev-2", "pid": q["pid"], "box": box}))
            self.assertEqual(json.loads(ws.receive_text())["t"], "denied")
        # reconnect with the device key
        with self.client.websocket_connect("/phone") as ws:
            hello = json.loads(ws.receive_text())
            c3 = base64.b64decode(hello["challenge"])
            box = phones.seal(key, {"challenge": hello["challenge"]}, b"auth|" + c3)
            ws.send_text(json.dumps({"t": "auth", "mode": "device", "device": "dev-1", "box": box}))
            w = json.loads(ws.receive_text())
            self.assertEqual(w["t"], "welcome")
            self.assertNotIn("device_key", phones.unseal(key, w["box"], b"welcome|" + c3))
            self.assertTrue(self.hub.online("dev-1"))

    def test_unknown_device_is_denied(self):
        with self.client.websocket_connect("/phone") as ws:
            hello = json.loads(ws.receive_text())
            ch = base64.b64decode(hello["challenge"])
            box = phones.seal(os.urandom(32), {"challenge": hello["challenge"]}, b"auth|" + ch)
            ws.send_text(json.dumps({"t": "auth", "mode": "device", "device": "nope", "box": box}))
            self.assertEqual(json.loads(ws.receive_text())["t"], "denied")

    def test_offline_phone_reports_error(self):
        self.hub._save_devices([{"id": "d", "name": "Pixel", "key": base64.b64encode(os.urandom(32)).decode()}])
        out = self.hub.tool({"action": "screen"})
        self.assertTrue(out.is_error)
        self.assertIn("offline", out.text)


class PhoneRiskTest(unittest.TestCase):
    def test_rules(self):
        r = tools.needs_confirmation
        self.assertTrue(r("phone", {"action": "sms", "op": "send", "to": "Mum", "text": "hi"}, "risky"))
        self.assertTrue(r("phone", {"action": "call", "number": "123"}, "risky"))
        self.assertTrue(r("phone", {"action": "files", "op": "delete", "path": "x"}, "risky"))
        self.assertTrue(r("phone", {"action": "shell", "command": "ls"}, "risky"))
        self.assertIsNone(r("phone", {"action": "tap", "index": 3}, "risky"))
        self.assertIsNone(r("phone", {"action": "sms", "op": "list"}, "always"))
        self.assertTrue(r("phone", {"action": "tap", "index": 3}, "always"))
        self.assertIsNone(r("phone", {"action": "sms", "op": "send"}, "never"))
        self.assertIn("phone", [s.name for s in tools.specs_for(True, phones=True)])
        self.assertNotIn("phone", [s.name for s in tools.specs_for(True)])


if __name__ == "__main__":
    unittest.main()
