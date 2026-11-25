"""Tamil + English (and the mix of both) for listening, replying and speaking.

Settings (settings["language"]):
  mode   auto | en | ta | ta_en   what you speak. ta_en = Tamil and English mixed freely (default).
  reply  match | en | ta          match = answer in the same language and style you used.
  tamil_voice  auto | edge | elevenlabs | openai | same
         which voice speaks Tamil sentences when the main voice can't (Piper, Kokoro, XTTS can't).
         auto = the main voice if it can speak Tamil, else Microsoft Edge's free neural Tamil voice.
  edge_voice   ta-IN-ValluvarNeural (male) | ta-IN-PallaviNeural (female)
"""
from __future__ import annotations

import re

TAMIL = re.compile(r"[஀-௿]")
LATIN = re.compile(r"[A-Za-z]")

# Common Tanglish (Tamil written in English letters) words, to tell "Tanglish" from plain English.
TANGLISH = set("""enna epdi eppadi enga yen yenna inga anga ippo appo ippa naan nan nee neenga avan aval avanga
namma nammal unga ungal enaku enakku unaku unakku romba konjam seri sari aama ama illa illai venam vendam venum
vaa vaanga po ponga paaru paru pannu pannunga panren pannu pannidu sollu sollunga solren theriyum theriyadhu
puriyudhu puriyala nalla nallaa irukku iruku irukken irukkiya iruka vanakkam nandri machan machi da di dei
podhum podum mudiyum mudiyadhu kitta pathi aprom apram ennachu sapadu saapadu thoongu thoonga kelu kekka
""".split())

STT_PROMPT_TA = "இன்னைக்கு weather எப்படி இருக்கு? Chrome open பண்ணு. Meeting எப்போ?"
STT_PROMPT_MIX = "Hey Jarvis, இன்னைக்கு என்ன plan? Chrome open பண்ணு, okay?"

EDGE_VOICES = {"ta-IN-ValluvarNeural": "Valluvar (Tamil male)", "ta-IN-PallaviNeural": "Pallavi (Tamil female)",
               "ta-LK-KumarNeural": "Kumar (Sri Lankan Tamil male)", "ta-LK-SaranyaNeural": "Saranya (Sri Lankan Tamil female)"}
TAMIL_CAPABLE = {"elevenlabs", "openai"}        # main TTS providers that can read Tamil script themselves


def has_tamil(text: str) -> bool:
    return bool(TAMIL.search(text or ""))


def detect(text: str) -> str:
    """'ta' (Tamil script), 'mixed' (Tamil script + English), 'tanglish', 'en' or '' (no letters)."""
    t = text or ""
    ta = len(TAMIL.findall(t))
    en = len(LATIN.findall(t))
    if not ta and not en:
        return ""
    if ta and en:
        return "ta" if en < ta * 0.15 else "mixed"
    if ta:
        return "ta"
    words = re.findall(r"[a-z]+", t.lower())
    hits = sum(w in TANGLISH for w in words)
    return "tanglish" if words and (hits >= 2 or hits / len(words) >= 0.25) else "en"


LABELS = {"ta": "Tamil", "mixed": "Tamil-English mix", "tanglish": "Tanglish (Tamil in English letters)",
          "en": "English", "": ""}


def stt_options(settings: dict) -> dict:
    """language + initial prompt hints for Whisper, from settings."""
    cfg = settings.get("language") or {}
    fixed = (settings.get("stt") or {}).get("language") or ""
    mode = cfg.get("mode", "ta_en")
    if fixed:
        return {"language": fixed, "prompt": STT_PROMPT_TA if fixed == "ta" else None, "restrict": None}
    if mode == "en":
        return {"language": "en", "prompt": None, "restrict": None}
    if mode == "ta":
        return {"language": "ta", "prompt": STT_PROMPT_TA, "restrict": None}
    if mode == "ta_en":
        return {"language": None, "prompt": None, "restrict": ("ta", "en")}
    return {"language": None, "prompt": None, "restrict": None}


def reply_instruction(settings: dict, user: str) -> str:
    cfg = settings.get("language") or {}
    mode, reply = cfg.get("mode", "ta_en"), cfg.get("reply", "match")
    if reply == "en":
        return f"- Always reply in English, even when {user} speaks Tamil."
    if reply == "ta":
        return ("- Always reply in Tamil (Tamil script), keeping common English tech words in English, the way people "
                "in Tamil Nadu talk.")
    if mode == "en":
        return ""
    return (f"- Language: {user} speaks Tamil, English, or both mixed freely. Answer in kind: Tamil script -> "
            f"reply in Tamil script; English -> English; Tanglish (Tamil in English letters) -> Tanglish; a mix -> "
            f"mix the same way, keeping English tech words in English. Sound like a Chennai local, natural and "
            f"colloquial (\"சரி\", \"பண்ணிட்டேன்\"), not formal textbook Tamil. Keep using tool names and arguments in English.")


def edge_rate(speed: float) -> str:
    return f"{int(round((float(speed or 1.0) - 1.0) * 100)):+d}%"


def tamil_route(settings: dict) -> str | None:
    """Which provider speaks a sentence with Tamil script, or None to keep the main voice."""
    cfg = settings.get("language") or {}
    choice = cfg.get("tamil_voice", "auto")
    main = (settings.get("tts") or {}).get("provider", "piper")
    if choice == "same":
        return None
    if choice == "auto":
        return None if main in TAMIL_CAPABLE else "edge"
    return None if choice == main else choice


# Spoken yes / no / stop in Tamil and Tanglish (used for confirmations and interruptions).
YES_TA = r"ஆமா|ஆமாம்|சரி|ஓகே|பண்ணு|செய்|aama|aamaa|amam|seri|sari|pannu|sei"
NO_TA = r"வேண்டாம்|வேணாம்|இல்ல|இல்லை|நிறுத்து|venam|vendam|vendaam|illa|illai|niruthu"
STOP_TA = r"நிறுத்து|போதும்|ஒரு நிமிஷம்|இரு|ஜார்விஸ்|niruthu|podhum|podum|oru nimisham"
NAME_TA = {"jarvis": "ஜார்விஸ்"}
