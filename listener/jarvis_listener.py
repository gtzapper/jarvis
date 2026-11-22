#!/usr/bin/env python3
"""Jarvis listener: say "Hey Jarvis" and talk. Runs on a Raspberry Pi or a Windows/Linux PC with a mic and a speaker.

The wake word is detected here, on this device (openWakeWord). Only after it hears "Hey Jarvis" does it record what you
say and send it to your Jarvis, which turns it into text, acts, and sends back the spoken answer.

    python jarvis_listener.py setup          # enter Jarvis's address and this device's access code
    python jarvis_listener.py                # listen
    python jarvis_listener.py devices        # list microphones and speakers
    python jarvis_listener.py test-wake FILE # check the wake word against a 16 kHz WAV file
"""

import argparse
import base64
import io
import json
import os
import queue
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests

RATE = 16000          # what the wake word model and Whisper expect
FRAME = 1280          # 80 ms frames, the wake word model's step
CONFIG = Path(os.environ.get("JARVIS_LISTENER_CONFIG") or Path.home() / ".config" / "jarvis-listener.json")
DEFAULTS = {"jarvis_url": "", "code": "", "threshold": 0.5, "input_device": None, "output_device": None,
            "follow_up_seconds": 5, "max_seconds": 12, "silence_seconds": 0.9}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG, 0o600)  # the access code is in here
    except OSError:
        pass


# ---------------------------------------------------------------- talking to Jarvis

class Jarvis:
    def __init__(self, cfg: dict):
        self.url = cfg["jarvis_url"].rstrip("/")
        self.headers = {"Authorization": f"Bearer {cfg['code']}"}

    def hello(self) -> dict:
        r = requests.get(self.url + "/api/voice/device/hello", headers=self.headers, timeout=10)
        if r.status_code == 401:
            raise RuntimeError("Jarvis doesn't recognise this access code. Add the device again in Setup > Listening devices.")
        r.raise_for_status()
        return r.json()

    def turn(self, wav: bytes) -> dict:
        r = requests.post(self.url + "/api/voice/device/turn", data=wav,
                          headers={**self.headers, "Content-Type": "audio/wav"}, timeout=120)
        if r.status_code == 401:
            raise RuntimeError("This device was removed from Jarvis (Setup > Listening devices).")
        if r.status_code >= 400:
            raise RuntimeError((r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}).get("error")
                               or f"Jarvis answered HTTP {r.status_code}")
        return r.json()

    def announcements(self, after: int | None) -> dict:
        r = requests.get(self.url + "/api/voice/device/announcements", params={} if after is None else {"after": after},
                         headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def job(self, job_id: str, speak: bool) -> dict:
        r = requests.get(f"{self.url}/api/voice/device/job/{job_id}", params={"speak": "1"} if speak else None,
                         headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- audio

def to_wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


def from_wav(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
    a = np.frombuffer(raw, dtype=dtype)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(dtype)
    return a, rate


def tone(freqs: list[float], ms: int = 110, rate: int = 22050, volume: float = 0.25) -> np.ndarray:
    """Little chimes, made here so there are no sound files to ship."""
    parts = []
    for f in freqs:
        t = np.arange(int(rate * ms / 1000)) / rate
        env = np.minimum(1, np.minimum(t, t[::-1]) * 60)  # soft edges, no clicks
        parts.append(np.sin(2 * np.pi * f * t) * env * volume)
    return (np.concatenate(parts) * 32767).astype(np.int16)


CHIME_START = tone([660, 990])
CHIME_DONE = tone([990, 660])
CHIME_ERROR = tone([300, 220], ms=180)


class Audio:
    """Microphone frames at 16 kHz (resampled if the mic can't do 16 kHz) and playback."""

    def __init__(self, cfg: dict):
        import sounddevice as sd
        self.sd = sd
        self.q: queue.Queue = queue.Queue()
        self.input_device = cfg.get("input_device")
        self.output_device = cfg.get("output_device")
        info = sd.query_devices(self.input_device, "input")
        self.mic_rate = RATE
        try:
            sd.check_input_settings(device=self.input_device, samplerate=RATE, channels=1, dtype="int16")
        except Exception:
            self.mic_rate = int(info["default_samplerate"])
        self.block = int(FRAME * self.mic_rate / RATE)
        self.buffer = np.zeros(0, dtype=np.int16)
        self.stream = sd.InputStream(device=self.input_device, samplerate=self.mic_rate, channels=1, dtype="int16",
                                     blocksize=self.block, callback=self._cb)
        log(f"Microphone: {info['name']} at {self.mic_rate} Hz")

    def _cb(self, indata, frames, t, status):
        self.q.put(indata[:, 0].copy())

    def start(self):
        self.stream.start()

    def frames(self):
        """Yield 80 ms frames at 16 kHz."""
        from scipy.signal import resample_poly
        while True:
            chunk = self.q.get()
            if self.mic_rate != RATE:
                chunk = resample_poly(chunk.astype(np.float32), RATE, self.mic_rate).astype(np.int16)
            self.buffer = np.concatenate([self.buffer, chunk])
            while len(self.buffer) >= FRAME:
                frame, self.buffer = self.buffer[:FRAME], self.buffer[FRAME:]
                yield frame

    def drain(self):
        """Throw away what the mic heard (e.g. Jarvis's own voice while it was speaking)."""
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.buffer = np.zeros(0, dtype=np.int16)

    def play(self, samples: np.ndarray, rate: int):
        self.sd.play(samples, rate, device=self.output_device)
        self.sd.wait()
        self.drain()


# ---------------------------------------------------------------- listening

def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


def record(frames, noise: float, cfg: dict, wait_for_speech: float) -> np.ndarray | None:
    """Record until the person stops talking. None if nobody spoke within wait_for_speech seconds."""
    threshold = max(noise * 3.0, 350.0)
    got, spoke, quiet, started = [], False, 0.0, time.monotonic()
    step = FRAME / RATE
    for frame in frames:
        got.append(frame)
        loud = rms(frame) > threshold
        if loud:
            spoke, quiet = True, 0.0
        elif spoke:
            quiet += step
        elapsed = len(got) * step
        if not spoke and elapsed > wait_for_speech:
            return None
        if spoke and quiet >= cfg["silence_seconds"]:
            break
        if elapsed >= cfg["max_seconds"]:
            break
    return np.concatenate(got) if spoke else None


def speak_reply(audio: Audio, data: dict) -> bool:
    if data.get("audio"):
        samples, rate = from_wav(base64.b64decode(data["audio"]))
        audio.play(samples, rate)
        return True
    if data.get("audio_error"):
        log("Jarvis couldn't speak the answer: " + data["audio_error"])
    return False


def handle(jarvis: Jarvis, audio: Audio, wav: bytes) -> bool:
    """Send one request and play the answer. Returns True if a follow-up is worth listening for."""
    t = time.monotonic()
    data = jarvis.turn(wav)
    if data.get("status") == "empty":
        log("Didn't catch anything.")
        audio.play(CHIME_DONE, 22050)
        return False
    log(f'Heard: "{data.get("heard")}" ({time.monotonic() - t:.1f}s)')
    job_id = data.get("job_id")
    spoke_waiting = False
    deadline = time.monotonic() + 150
    while data.get("status") in ("running", "waiting") and time.monotonic() < deadline:
        if data["status"] == "waiting" and not spoke_waiting:
            log("Waiting for your OK on the phone or dashboard...")
            if not data.get("audio"):
                data = jarvis.job(job_id, speak=True)
            speak_reply(audio, data)
            spoke_waiting = True
        time.sleep(2)
        data = jarvis.job(job_id, speak=False)
        if data.get("status") == "done":
            data = jarvis.job(job_id, speak=True)
    if data.get("status") != "done":
        log("Still waiting after 2 minutes; the question stays on your phone and dashboard.")
        audio.play(CHIME_DONE, 22050)
        return False
    log(f'Jarvis: {data.get("reply", "")[:200]}')
    speak_reply(audio, data)
    return not spoke_waiting


def announcer(jarvis: Jarvis, audio: "Audio", busy: threading.Event) -> None:
    """Speak reminders, bill notices and the daily report when they come in (not in the middle of a conversation)."""
    last = None
    while True:
        try:
            data = jarvis.announcements(last)
            if last is not None and data.get("items"):
                while busy.is_set():
                    time.sleep(1)
                busy.set()
                try:
                    for item in data["items"]:
                        log(f"Announcing: {item.get('text', '')[:120]}")
                        audio.play(CHIME_START, 22050)
                        speak_reply(audio, item)
                finally:
                    busy.clear()
            last = data.get("last", last)
        except Exception as e:
            log(f"(Couldn't check for reminders: {e})")
        time.sleep(20)


def run(cfg: dict) -> None:
    if not cfg["jarvis_url"] or not cfg["code"]:
        sys.exit("Not set up yet. Run:  python jarvis_listener.py setup")
    jarvis = Jarvis(cfg)
    hello = jarvis.hello()
    log(f"Connected to Jarvis as '{hello['device']}'." + ("" if hello.get("voice_ready") else " Voice isn't set up in Jarvis yet (Setup > Voice)."))
    wake = WakeWord(cfg)
    audio = Audio(cfg)
    audio.start()
    frames = audio.frames()
    busy = threading.Event()
    threading.Thread(target=announcer, args=(jarvis, audio, busy), daemon=True).start()
    noise_hist: list[float] = []
    log('Listening for "Hey Jarvis"...')
    for frame in frames:
        noise_hist = (noise_hist + [rms(frame)])[-40:]
        if busy.is_set() or not wake.heard(frame):
            continue
        busy.set()
        noise = float(np.median(noise_hist)) if noise_hist else 200.0
        follow_up = True
        first = True
        while follow_up:
            if first:
                log("Wake word! Listening...")
                audio.play(CHIME_START, 22050)
            speech = record(frames, noise, cfg, wait_for_speech=4.0 if first else cfg["follow_up_seconds"])
            if speech is None:
                if first:
                    audio.play(CHIME_DONE, 22050)
                break
            try:
                follow_up = handle(jarvis, audio, to_wav(speech))
            except (requests.RequestException, RuntimeError) as e:
                log(f"Problem: {e}")
                audio.play(CHIME_ERROR, 22050)
                follow_up = False
            first = False
        wake.reset()
        audio.drain()
        busy.clear()
        log('Listening for "Hey Jarvis"...')


class WakeWord:
    def __init__(self, cfg: dict):
        import openwakeword
        from openwakeword.model import Model
        try:
            openwakeword.utils.download_models(model_names=["hey_jarvis"])
        except TypeError:  # older versions take no arguments
            openwakeword.utils.download_models()
        except Exception as e:
            log(f"(Couldn't check for wake word model updates: {e})")
        self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self.threshold = float(cfg.get("threshold") or 0.5)

    def score(self, frame: np.ndarray) -> float:
        p = self.model.predict(frame)
        return max(p.values()) if p else 0.0

    def heard(self, frame: np.ndarray) -> bool:
        return self.score(frame) >= self.threshold

    def reset(self):
        self.model.reset()


# ---------------------------------------------------------------- commands

def setup() -> None:
    cfg = load_config()
    print("Jarvis listener setup. Press Enter to keep the value in [brackets].")
    url = input(f"Jarvis address, e.g. http://10.0.20.5:8765 [{cfg['jarvis_url']}]: ").strip() or cfg["jarvis_url"]
    if url and "://" not in url:
        url = "http://" + url
    code = input("Access code (Setup > Listening devices > Add device)" + (" [keep]" if cfg["code"] else "") + ": ").strip() or cfg["code"]
    cfg.update(jarvis_url=url, code=code)
    try:
        hello = Jarvis(cfg).hello()
        print(f"Connected: Jarvis knows this device as '{hello['device']}'.")
        if not hello.get("voice_ready"):
            print("Note: voice isn't set up in Jarvis yet. Do Setup > Voice there.")
    except Exception as e:
        print(f"Couldn't connect yet: {e}")
    save_config(cfg)
    print(f"Saved to {CONFIG}. Start listening with: python {Path(__file__).name}")


def list_devices() -> None:
    import sounddevice as sd
    print(sd.query_devices())
    print('\nTo pick one, put its number in the config file as "input_device" / "output_device":', CONFIG)


def test_wake(path: str) -> None:
    cfg = load_config()
    wake = WakeWord(cfg)
    samples, rate = from_wav(Path(path).read_bytes())
    if rate != RATE:
        from scipy.signal import resample_poly
        samples = resample_poly(samples.astype(np.float32), RATE, rate).astype(np.int16)
    best, at = 0.0, 0.0
    for i in range(0, len(samples) - FRAME, FRAME):
        s = wake.score(samples[i:i + FRAME])
        if s > best:
            best, at = s, i / RATE
    print(f"Best wake word score {best:.2f} at {at:.1f}s (threshold {wake.threshold}): {'DETECTED' if best >= wake.threshold else 'not detected'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Say 'Hey Jarvis' and talk to your Jarvis.")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "setup", "devices", "test-wake"])
    ap.add_argument("file", nargs="?")
    a = ap.parse_args()
    if a.command == "setup":
        setup()
    elif a.command == "devices":
        list_devices()
    elif a.command == "test-wake":
        test_wake(a.file or sys.exit("Give a WAV file"))
    else:
        while True:
            try:
                run(load_config())
            except KeyboardInterrupt:
                print()
                return
            except (requests.RequestException, RuntimeError) as e:
                log(f"Can't reach Jarvis ({e}). Trying again in 30 seconds.")
                time.sleep(30)


if __name__ == "__main__":
    main()
