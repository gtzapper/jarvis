"""Voice for "Hey Jarvis": speech-to-text and text-to-speech through a Speaches server (Whisper + Kokoro),
plus the listening devices allowed to send audio to Jarvis.

Speaches runs next to Jarvis by default (docker compose --profile voice), on the CPU. Point Setup > Voice at another
address to use one running elsewhere, e.g. the CUDA image on a machine with a GPU."""

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

import requests

from .config import env

DEFAULTS = {
    "url": "http://voice:8000",                                # the 'voice' service in Jarvis's docker-compose.yml
    "stt_model": "Systran/faster-distil-whisper-small.en",     # small and quick on a CPU, English
    "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
    "voice": "bm_george",                                      # a British male Kokoro voice
    "language": "en",
    "speed": 1.0,
}
DEVICES = Path("data") / "voice_devices.json"


class VoiceError(Exception):
    pass


class VoiceClient:
    def __init__(self, cfg: dict):
        v = cfg.get("voice") or {}
        self.url = (v.get("url") or DEFAULTS["url"]).rstrip("/")
        self.stt_model = v.get("stt_model") or DEFAULTS["stt_model"]
        self.tts_model = v.get("tts_model") or DEFAULTS["tts_model"]
        self.voice = v.get("voice") or DEFAULTS["voice"]
        self.language = v.get("language") or ""
        self.speed = float(v.get("speed") or DEFAULTS["speed"])
        key = env(v.get("api_key_env") or "VOICE_API_KEY")
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}

    def _req(self, method: str, path: str, timeout: float = 60, **kw) -> requests.Response:
        try:
            r = requests.request(method, self.url + path, headers=self.headers, timeout=timeout, **kw)
        except requests.ConnectionError:
            raise VoiceError(f"Can't reach the voice server at {self.url}. Is the Speaches container running?")
        except requests.Timeout:
            raise VoiceError(f"The voice server at {self.url} took too long to answer")
        if r.status_code == 401:
            raise VoiceError("The voice server wants an API key (or the key is wrong). Set it in Setup > Voice")
        return r

    def health(self) -> None:
        r = self._req("GET", "/health", timeout=10)
        if r.status_code != 200:
            raise VoiceError(f"The voice server answered HTTP {r.status_code} on /health")

    def installed(self) -> set[str]:
        r = self._req("GET", "/v1/models", timeout=20)
        if r.status_code != 200:
            raise VoiceError(f"Couldn't list the voice server's models (HTTP {r.status_code})")
        return {m.get("id") for m in (r.json().get("data") or [])}

    def ensure_models(self) -> list[str]:
        """Download the speech and voice models if the server doesn't have them yet. Returns what was downloaded."""
        have = self.installed()
        got = []
        for model in (self.stt_model, self.tts_model):
            if model in have:
                continue
            r = self._req("POST", f"/v1/models/{model}", timeout=1800)  # first download can be several hundred MB
            if r.status_code not in (200, 201):
                raise VoiceError(f"The voice server couldn't download {model}: HTTP {r.status_code} {r.text[:200]}")
            got.append(model)
        return got

    def transcribe(self, audio: bytes, filename: str = "speech.wav") -> str:
        data = {"model": self.stt_model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        r = self._req("POST", "/v1/audio/transcriptions", timeout=120, files={"file": (filename, audio, "audio/wav")}, data=data)
        if r.status_code != 200:
            raise VoiceError(f"Speech-to-text failed: HTTP {r.status_code} {r.text[:200]}")
        try:
            return (r.json().get("text") or "").strip()
        except ValueError:
            return r.text.strip()

    def speak(self, text: str) -> bytes:
        """WAV audio of Jarvis saying text."""
        body = {"model": self.tts_model, "voice": self.voice, "input": speakable(text), "response_format": "wav", "speed": self.speed}
        r = self._req("POST", "/v1/audio/speech", timeout=120, json=body)
        if r.status_code != 200:
            raise VoiceError(f"Text-to-speech failed: HTTP {r.status_code} {r.text[:200]}")
        return r.content


def speakable(text: str, limit: int = 1200) -> str:
    """Spoken answers should sound natural: drop list markers and long technical lines, keep it short."""
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", line).strip()
        if line:
            lines.append(line)
    out = ". ".join(l.rstrip(".:;") for l in lines)
    out = re.sub(r"https?://\S+", "a link", out)
    if len(out) > limit:
        out = out[:limit].rsplit(". ", 1)[0] + ". There's more in the app."
    return out


def test_voice(cfg: dict) -> str:
    """Setup's Save & test: reachable, models present (downloading them if needed), and a round trip through both."""
    c = VoiceClient(cfg)
    started = time.monotonic()
    c.health()
    got = c.ensure_models()
    phrase = "Jarvis voice check. All systems online."
    t0 = time.monotonic()
    wav = c.speak(phrase)
    t1 = time.monotonic()
    heard = c.transcribe(wav)
    t2 = time.monotonic()
    words = lambda s: set(re.findall(r"[a-z]+", s.lower()))
    match = len(words(phrase) & words(heard)) / len(words(phrase))
    note = f" Downloaded {', '.join(got)}." if got else ""
    if match < 0.5:
        raise VoiceError(f"The voice server works, but it heard {heard!r} for {phrase!r}. Try a larger speech model.{note}")
    return (f"Voice works: speaking took {t1 - t0:.1f}s, understanding {t2 - t1:.1f}s "
            f"(heard \"{heard}\").{note} Total {time.monotonic() - started:.0f}s")


# ---------------------------------------------------------------- listening devices

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> list[dict]:
    try:
        return json.loads(DEVICES.read_text())
    except (OSError, ValueError):
        return []


def _save(devices: list[dict]) -> None:
    DEVICES.parent.mkdir(exist_ok=True)
    DEVICES.write_text(json.dumps(devices, indent=1))


def list_devices() -> list[dict]:
    return [{k: d[k] for k in ("id", "name", "created", "last_seen")} for d in _load()]


def add_device(name: str) -> dict:
    name = re.sub(r"\s+", " ", (name or "").strip())[:40]
    if not name:
        raise ValueError("Give the device a name, e.g. Kitchen Pi")
    devices = _load()
    if any(d["name"].lower() == name.lower() for d in devices):
        raise ValueError(f"There's already a device called {name}")
    token = "jv_" + secrets.token_urlsafe(24)
    d = {"id": secrets.token_hex(4), "name": name, "hash": _hash(token), "created": time.strftime("%Y-%m-%d %H:%M"), "last_seen": None}
    devices.append(d)
    _save(devices)
    return {"id": d["id"], "name": name, "token": token}  # the only time the code is shown


def remove_device(device_id: str) -> None:
    _save([d for d in _load() if d["id"] != device_id])


def device_for(token: str | None) -> dict | None:
    """The device a token belongs to, or None. Also notes when it was last heard from."""
    if not token:
        return None
    h = _hash(token)
    devices = _load()
    for d in devices:
        if secrets.compare_digest(d["hash"], h):
            now = time.strftime("%Y-%m-%d %H:%M")
            if d.get("last_seen") != now:
                d["last_seen"] = now
                _save(devices)
            return d
    return None
