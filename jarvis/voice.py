"""Voice: speech-to-text via Gemini, text-to-speech with a British accent (Gemini TTS or Edge neural voice)."""
from __future__ import annotations

import io
import re
import time
import wave

from google.genai import types

from . import config, db

# No spoken style instruction is sent: TTS models sometimes read such an instruction aloud before the reply.
# British English is requested through the speech config's language code instead (when the reply is English).
_lang_code_ok = True

GEMINI_VOICES = [
    "Charon", "Iapetus", "Algenib", "Orus", "Rasalgethi", "Sadaltager", "Schedar", "Alnilam",
    "Enceladus", "Umbriel", "Achird", "Fenrir", "Puck", "Kore", "Aoede", "Gacrux", "Sulafat",
]
EDGE_BRITISH_VOICES = ["en-GB-RyanNeural", "en-GB-ThomasNeural", "en-GB-SoniaNeural", "en-GB-LibbyNeural"]

_tts_model_cache: str | None = None


def _client():
    from .agent import jarvis

    return jarvis._ensure_client()


def available_models() -> list[str]:
    try:
        return sorted(m.name.removeprefix("models/") for m in _client().models.list())
    except Exception:  # noqa: BLE001
        return []


def _version_key(name: str) -> tuple:
    nums = re.findall(r"\d+(?:\.\d+)?", name)
    v = float(nums[0]) if nums else 0.0
    return (v, "preview" not in name, "lite" not in name, "pro" not in name)


def tts_model() -> str:
    """Resolve GEMINI_TTS_MODEL=auto to the newest Flash TTS model the key can use."""
    global _tts_model_cache
    if config.GEMINI_TTS_MODEL and config.GEMINI_TTS_MODEL != "auto":
        return config.GEMINI_TTS_MODEL
    if _tts_model_cache:
        return _tts_model_cache
    candidates = [m for m in available_models() if "tts" in m]
    _tts_model_cache = max(candidates, key=_version_key) if candidates else "gemini-2.5-flash-preview-tts"
    return _tts_model_cache


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"https?://\S+", "the link", t)
    t = re.sub(r"[*_#`>|~]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()


def _pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _gemini_tts(text: str, voice: str) -> tuple[bytes, str]:
    from . import usage

    model = tts_model()
    usage.check_limit()  # at the daily limit: synthesize() falls back to the free Microsoft voice
    try:
        resp = _gemini_tts_call(model, text, voice)
    except Exception as e:
        usage.record_error(model, "voice", e)
        raise
    usage.record_ok(model, "voice", resp)
    part = resp.candidates[0].content.parts[0]
    data, mime = part.inline_data.data, (part.inline_data.mime_type or "")
    if "wav" in mime or "mpeg" in mime or "mp3" in mime:
        return data, mime
    m = re.search(r"rate=(\d+)", mime)
    return _pcm_to_wav(data, int(m.group(1)) if m else 24000), "audio/wav"


def _gemini_tts_call(model: str, text: str, voice: str):
    global _lang_code_ok
    latin = not re.search(r"[\u0590-\u06FF\u0400-\u04FF\u3040-\u9FFF]", text)  # Hebrew/Arabic/Cyrillic/CJK: no en-GB
    use_lang = _lang_code_ok and latin

    def call(with_lang: bool):
        return _client().models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)),
                    language_code="en-GB" if with_lang else None,
                ),
            ),
        )

    try:
        return call(use_lang)
    except Exception as e:  # noqa: BLE001
        if use_lang and "400" in str(e) and "language" in str(e).lower():
            _lang_code_ok = False  # this TTS model doesn't take a language code: just use the voice
            return call(False)
        raise


async def _edge_tts(text: str, voice: str) -> tuple[bytes, str]:
    import edge_tts  # optional dependency

    comm = edge_tts.Communicate(text, voice, rate="+4%", pitch="-2Hz")
    out = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            out.extend(chunk["data"])
    return bytes(out), "audio/mpeg"


async def synthesize(text: str) -> tuple[bytes, str] | None:
    """Returns (audio bytes, mime) or None if the browser should speak it itself."""
    import anyio

    s = db.get_settings()
    text = clean_for_speech(text)
    if not text:
        return None
    engine = s.get("voice_engine", "gemini")
    edge_voice = s.get("edge_voice") or "en-GB-RyanNeural"
    if engine == "gemini" and time.time() < _gemini_tts_paused_until:
        engine = "edge"  # Gemini voice quota recently ran out: use the free British neural voice meanwhile
    if engine == "gemini":
        try:
            return await anyio.to_thread.run_sync(_gemini_tts, text, s.get("gemini_voice") or "Charon")
        except Exception as e:  # noqa: BLE001
            _pause_gemini_tts(e)
            engine = "edge"
    if engine == "edge":
        try:
            return await _edge_tts(text, edge_voice)
        except Exception as e:  # noqa: BLE001
            print("[voice] edge-tts failed, the device voice will be used:", e)
    return None


_gemini_tts_paused_until = 0.0


def _pause_gemini_tts(e: Exception) -> None:
    global _gemini_tts_paused_until
    quota = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
    _gemini_tts_paused_until = time.time() + (3600 if quota else 120)
    print(f"[voice] Gemini TTS failed ({str(e)[:120]}); using the Microsoft British voice for now")


def transcribe(audio: bytes, mime: str) -> str:
    from .agent import jarvis

    part = types.Part.from_bytes(data=audio, mime_type=mime)
    text = jarvis.one_shot(
        "Transcribe this voice command exactly as spoken, in the language spoken. "
        "Output only the transcript text, nothing else. If there is no intelligible speech, output nothing.",
        parts=[part],
    )
    return text.strip().strip('"')
