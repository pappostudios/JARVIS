"""Voice: speech-to-text via Gemini, text-to-speech with a British accent (Gemini TTS or Edge neural voice)."""
from __future__ import annotations

import io
import re
import wave

from google.genai import types

from . import config, db

TTS_STYLE = (
    "Read the following in a calm, refined, received-pronunciation British English accent — "
    "a composed, warm and slightly wry English butler. Natural pace, no theatrics:\n\n"
)

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
    resp = _client().models.generate_content(
        model=tts_model(),
        contents=TTS_STYLE + text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
            ),
        ),
    )
    part = resp.candidates[0].content.parts[0]
    data, mime = part.inline_data.data, (part.inline_data.mime_type or "")
    if "wav" in mime or "mpeg" in mime or "mp3" in mime:
        return data, mime
    m = re.search(r"rate=(\d+)", mime)
    return _pcm_to_wav(data, int(m.group(1)) if m else 24000), "audio/wav"


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
    if engine == "edge":
        try:
            return await _edge_tts(text, s.get("edge_voice") or "en-GB-RyanNeural")
        except Exception as e:  # noqa: BLE001
            print("[voice] edge-tts failed, falling back to Gemini:", e)
            engine = "gemini"
    if engine == "gemini":
        return await anyio.to_thread.run_sync(_gemini_tts, text, s.get("gemini_voice") or "Charon")
    return None


def transcribe(audio: bytes, mime: str) -> str:
    from .agent import jarvis

    part = types.Part.from_bytes(data=audio, mime_type=mime)
    text = jarvis.one_shot(
        "Transcribe this voice command exactly as spoken, in the language spoken. "
        "Output only the transcript text, nothing else. If there is no intelligible speech, output nothing.",
        parts=[part],
    )
    return text.strip().strip('"')
