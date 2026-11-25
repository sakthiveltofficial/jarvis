"""v0.7: Home Assistant, ambient mode, Tamil/English, custom voice, clap wiring."""
import datetime as dt
import json
import os
import tempfile
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

import httpx  # noqa: E402
import numpy as np  # noqa: E402

from jarvis import ambient, config, homeassistant, lang, voiceclone  # noqa: E402

STATES = [
    {"entity_id": "light.bedroom_ceiling", "state": "off", "attributes": {"friendly_name": "Bedroom Ceiling"}},
    {"entity_id": "light.living_room_lamp", "state": "on", "attributes": {"friendly_name": "Living Room Lamp"}},
    {"entity_id": "light.living_room_strip", "state": "off", "attributes": {"friendly_name": "TV Strip"}},
    {"entity_id": "climate.bedroom_ac", "state": "off",
     "attributes": {"friendly_name": "Bedroom Air Conditioner", "temperature": 26, "current_temperature": 29}},
    {"entity_id": "media_player.samsung_tv", "state": "on", "attributes": {"friendly_name": "Samsung TV"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "sensor.outdoor_temp", "state": "31", "attributes": {"friendly_name": "Outdoor Temp"}},
]
AREAS = "light.bedroom_ceiling|Bedroom\nlight.living_room_lamp|Living Room\nlight.living_room_strip|Living Room\n" \
        "climate.bedroom_ac|Bedroom\nmedia_player.samsung_tv|Living Room\n"


class FakeHA:
    def __init__(self, token="good"):
        self.calls = []
        self.token = token

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, text="401: Unauthorized")
        p = req.url.path
        if p == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if p == "/api/config":
            return httpx.Response(200, json={"version": "2026.11.1", "location_name": "Home"})
        if p == "/api/states":
            return httpx.Response(200, json=STATES)
        if p == "/api/template":
            return httpx.Response(200, text=AREAS, headers={"content-type": "text/plain"})
        if p.startswith("/api/services/"):
            self.calls.append((p[len("/api/services/"):], json.loads(req.content or b"{}")))
            return httpx.Response(200, json=[])
        if p == "/api/conversation/process":
            return httpx.Response(200, json={"response": {"speech": {"plain": {"speech": "Turned on the fan"}}}})
        return httpx.Response(404)


def ha(token="good"):
    fake = FakeHA()
    settings = {"home_assistant": {"url": "http://ha.local:8123", "token": token}}
    return homeassistant.HomeAssistant(settings, transport=httpx.MockTransport(fake)), fake


class HomeAssistantTest(unittest.TestCase):
    def test_connection(self):
        h, _ = ha()
        r = h.test()
        self.assertTrue(r["ok"])
        self.assertEqual(r["counts"]["light"], 3)
        self.assertNotIn("sensor", r["counts"])
        bad, _ = ha("wrong")
        self.assertFalse(bad.test()["ok"])

    def test_name_matching(self):
        h, _ = ha()
        self.assertEqual(h.find("bedroom AC")[0]["entity_id"], "climate.bedroom_ac")
        self.assertEqual(h.find("the TV")[0]["entity_id"], "media_player.samsung_tv")
        self.assertEqual(h.find("bedroom light")[0]["entity_id"], "light.bedroom_ceiling")
        self.assertEqual(h.find("living room lamp")[0]["entity_id"], "light.living_room_lamp")
        self.assertIsNone(h.find("garage heater")[0])
        self.assertEqual({d["entity_id"] for d in h.find_all("all lights", "light")},
                         {"light.bedroom_ceiling", "light.living_room_lamp", "light.living_room_strip"})
        self.assertEqual(len(h.find_all("all living room lights", "light")), 2)

    def test_controls(self):
        h, fake = ha()
        out = h.tool({"action": "turn_on", "device": "bedroom light", "brightness": 40, "color": "warm"})
        self.assertFalse(out.is_error, out.text)
        self.assertEqual(fake.calls[-1], ("light/turn_on", {"entity_id": "light.bedroom_ceiling", "brightness_pct": 40,
                                                            "color_temp_kelvin": 2700}))
        h.tool({"action": "set", "device": "AC", "temperature": 24, "hvac_mode": "cool"})
        self.assertEqual(fake.calls[-1], ("climate/set_temperature",
                                          {"entity_id": "climate.bedroom_ac", "temperature": 24.0, "hvac_mode": "cool"}))
        h.tool({"action": "turn_off", "device": "TV"})
        self.assertEqual(fake.calls[-1][0], "media_player/turn_off")
        h.tool({"action": "set", "device": "TV", "volume": 20})
        self.assertEqual(fake.calls[-1], ("media_player/volume_set",
                                          {"entity_id": "media_player.samsung_tv", "volume_level": 0.2}))
        h.tool({"action": "turn_off", "device": "all lights", "domain": "light"})
        self.assertEqual(sum(c[0] == "light/turn_off" for c in fake.calls), 3)
        self.assertIn("Turned on", h.tool({"action": "assist", "text": "turn on the fan"}).text)
        self.assertTrue(h.tool({"action": "turn_on", "device": "jacuzzi"}).is_error)
        self.assertIn("Bedroom Air Conditioner", h.tool({"action": "state", "device": "bedroom ac"}).text)

    def test_risk(self):
        h, _ = ha()
        self.assertIsNone(h.risk({"action": "turn_on", "device": "bedroom light"}))
        self.assertIsNone(h.risk({"action": "lock", "device": "front door"}))
        self.assertTrue(h.risk({"action": "unlock", "device": "front door"}))
        self.assertTrue(h.risk({"action": "service", "service": "lock.unlock"}))
        self.assertIsNone(h.risk({"action": "list"}))

    def test_summary_and_masking(self):
        h, _ = ha()
        s = h.summary()
        self.assertIn("Bedroom Air Conditioner", s)
        self.assertNotIn("Outdoor Temp", s)
        pub = config.public(config._deep_merge(config.DEFAULTS, {"home_assistant": {"url": "x", "token": "abcdefghijkl"}}))
        self.assertTrue(pub["home_assistant"]["token"].startswith("••••"))


class AmbientTest(unittest.TestCase):
    CFG = {"enabled": True, "quiet_enabled": True, "quiet_start": "23:00", "quiet_end": "07:00", "min_gap_min": 45,
           "coding_break": True, "coding_break_h": 2.0, "screen_break": True, "screen_break_h": 3.0,
           "welcome_back": True, "welcome_after_min": 45, "late_night": False}

    def run_day(self, title, hours, start_hour=10, cfg=None, idle=lambda t: 0.0):
        a = ambient.Ambient(None, start=False)
        base = dt.datetime(2026, 11, 25, start_hour, 0).timestamp()
        out = []
        t = base
        while t < base + hours * 3600:
            r = a.tick(t, title(t - base) if callable(title) else title, idle(t - base), cfg or self.CFG,
                       local=dt.datetime.fromtimestamp(t))
            if r:
                out.append(((t - base) / 3600, r[0]))
            t += 20
        return out

    def test_coding_break(self):
        out = self.run_day("main.py - jarvis - Visual Studio Code", 5)
        self.assertEqual(out[0][1], "coding")
        self.assertAlmostEqual(out[0][0], 2.0, delta=0.05)
        self.assertTrue(all(b[0] - a[0] >= 0.75 for a, b in zip(out, out[1:])))     # min gap respected

    def test_breaks_reset_and_quiet_hours(self):
        # A 10-minute break every 90 minutes: never nags about coding.
        idle = lambda t: (t % 5400 - 4800) if t % 5400 > 4800 else 0.0     # noqa: E731
        out = self.run_day("Visual Studio Code", 6, idle=idle)
        self.assertFalse([o for o in out if o[1] == "coding"])
        self.assertEqual(self.run_day("Visual Studio Code", 4, start_hour=23), [])   # quiet hours
        self.assertEqual(self.run_day("Visual Studio Code", 4, cfg={**self.CFG, "enabled": False}), [])
        self.assertEqual(self.run_day("Zoom Meeting", 4), [])

    def test_quiet_hours_window(self):
        q = {"quiet_start": "23:00", "quiet_end": "07:00"}
        self.assertTrue(ambient.in_quiet_hours(q, dt.datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(ambient.in_quiet_hours(q, dt.datetime(2026, 1, 1, 6, 59)))
        self.assertFalse(ambient.in_quiet_hours(q, dt.datetime(2026, 1, 1, 7, 0)))
        self.assertTrue(ambient.in_quiet_hours({"quiet_start": "13:00", "quiet_end": "14:00"}, dt.datetime(2026, 1, 1, 13, 30)))
        self.assertFalse(ambient.in_quiet_hours({**q, "quiet_enabled": False}, dt.datetime(2026, 1, 1, 23, 30)))

    def test_welcome_back(self):
        idle = lambda t: (t - 3600) if 3600 <= t < 3600 + 3000 else 0.0      # noqa: E731  away 50 min
        out = self.run_day("Chrome", 2, idle=idle)
        self.assertEqual(out[0][1], "welcome")


class LanguageTest(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(lang.detect("இன்னைக்கு வானிலை எப்படி?"), "ta")
        self.assertEqual(lang.detect("இன்னைக்கு weather எப்படி இருக்கு? Chrome open பண்ணு"), "mixed")
        self.assertEqual(lang.detect("enna panra, konjam music podu"), "tanglish")
        self.assertEqual(lang.detect("open chrome and play some music"), "en")

    def test_stt_options_and_prompt(self):
        s = config._deep_merge(config.DEFAULTS, {})
        self.assertEqual(lang.stt_options(s)["restrict"], ("ta", "en"))
        self.assertEqual(lang.stt_options({**s, "stt": {"language": "ta"}})["language"], "ta")
        from jarvis import persona
        p = persona.system_prompt(s, True)
        self.assertIn("Tanglish", p)
        en = config._deep_merge(s, {"language": {"mode": "en"}})
        self.assertNotIn("Tanglish", persona.system_prompt(en, True))

    def test_tamil_routing(self):
        s = config._deep_merge(config.DEFAULTS, {})
        self.assertEqual(lang.tamil_route(s), "edge")
        self.assertIsNone(lang.tamil_route(config._deep_merge(s, {"tts": {"provider": "elevenlabs"}})))
        self.assertIsNone(lang.tamil_route(config._deep_merge(s, {"language": {"tamil_voice": "same"}})))
        self.assertEqual(lang.edge_rate(1.1), "+10%")

    def test_tamil_confirm_and_interrupt(self):
        from jarvis.agent import NO, YES, Session, wake_regex
        self.assertTrue(YES.match("சரி பண்ணு"))
        self.assertTrue(YES.match("seri"))
        self.assertTrue(NO.match("வேண்டாம்"))
        self.assertTrue(Session.is_interruption("நிறுத்து", "the weather is sunny")[0])
        self.assertTrue(Session.is_interruption("இல்ல இல்ல வேற பாட்டு போடு", "the weather is sunny")[0])
        self.assertEqual(wake_regex("Jarvis").sub("", "ஹே ஜார்விஸ், லைட் ஆன் பண்ணு").strip(), "லைட் ஆன் பண்ணு")


class VoiceCloneTest(unittest.TestCase):
    def test_sample_and_fish_request(self):
        import soundfile as sf
        import io
        t = np.arange(24000 * 8) / 24000
        voice = (0.3 * np.sin(2 * np.pi * 150 * t)).astype(np.float32)
        audio = np.concatenate([np.zeros(24000), voice, np.zeros(24000)])
        buf = io.BytesIO()
        sf.write(buf, audio, 24000, format="WAV")
        info = voiceclone.save_sample(buf.getvalue(), "Hello there, this is my voice.")
        self.assertTrue(info["has_sample"])
        self.assertAlmostEqual(info["seconds"], 8.3, delta=0.4)      # silence trimmed
        self.assertTrue(info["ok"])

        seen = {}

        def fish(req: httpx.Request):
            import msgpack
            body = msgpack.unpackb(req.content, raw=False)
            seen.update(body)
            out = io.BytesIO()
            sf.write(out, np.zeros(2400, np.float32), 24000, format="WAV")
            return httpx.Response(200, content=out.getvalue())
        s = config._deep_merge(config.DEFAULTS, {"tts": {"provider": "clone", "clone_engine": "fish_speech"}})
        x, sr = voiceclone.synthesize(s, "Good evening", transport=httpx.MockTransport(fish))
        self.assertEqual(sr, 24000)
        self.assertEqual(seen["text"], "Good evening")
        self.assertEqual(seen["references"][0]["text"], "Hello there, this is my voice.")
        self.assertGreater(len(seen["references"][0]["audio"]), 100000)

    def test_fallback_to_regular_voice(self):
        from jarvis.speech import TTS
        s = config._deep_merge(config.DEFAULTS, {"tts": {"provider": "clone", "clone_url": "http://127.0.0.1:9",
                                                         "clone_fallback": "piper"}})
        tts = TTS(s)
        used = []
        tts._piper = lambda text: (used.append(text), (np.zeros(10, np.float32), 22050))[1]
        notes = []
        TTS.notice = notes.append
        tts.synthesize("Hello")
        self.assertEqual(used, ["Hello"])
        self.assertTrue(notes and "Custom voice unavailable" in notes[0])
        TTS.notice = None


class SessionWiringTest(unittest.TestCase):
    def test_clap_smart_home_and_language(self):
        import asyncio
        from jarvis import agent, llm

        events = []

        async def emit(ev):
            events.append(ev)

        async def main():
            s = agent.Session(emit)
            try:
                s.voice_mode = "wake"
                s._sync_clap()
                self.assertIsNotNone(s.mic.clap)
                s.settings["voice"]["clap"] = False
                s._sync_clap()
                self.assertIsNone(s.mic.clap)
                names = [t.name for t in s.main.specs()]
                self.assertNotIn("smart_home", names)
                s.settings["home_assistant"] = {"enabled": True, "url": "http://ha.local:8123", "token": "good"}
                s.home.transport = httpx.MockTransport(FakeHA())
                self.assertIn("smart_home", [t.name for t in s.main.specs()])
                call = llm.ToolCall(id="1", name="smart_home", args={"action": "unlock", "device": "front door"})
                self.assertTrue(s.main.risk(call))
                call = llm.ToolCall(id="2", name="smart_home", args={"action": "turn_on", "device": "bedroom light"})
                self.assertIsNone(s.main.risk(call))
                out = await s.main.call_tool(call)
                self.assertFalse(out.is_error, out.text)
                s.last_lang = "tanglish"
                self.assertIn("Tanglish", s.system_prompt(True))
                s.apply_settings({"home_assistant": {"url": "http://ha.local:8123", "token": "••••good"}})
                self.assertEqual(s.settings["home_assistant"]["token"], "good")
            finally:
                s.shutdown()
            await asyncio.sleep(0.05)

        asyncio.run(main())


class ServerRoutesTest(unittest.TestCase):
    def test_routes(self):
        from jarvis import server
        paths = {r.path for r in server.app.routes}
        for p in ("/api/voice/clone", "/api/voice/sample", "/api/voice/record", "/api/homeassistant/test",
                  "/api/homeassistant/devices"):
            self.assertIn(p, paths)


if __name__ == "__main__":
    unittest.main()
