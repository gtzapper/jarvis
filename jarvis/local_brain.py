"""Offline AI: the same brain and tools, running on a local model through Ollama. Nothing leaves your network."""

import json
import re
import time
from typing import Callable

import requests

from .brain import MAX_STEPS, MAX_TURNS, TOOLS, Brain, describe, outcome, system_prompt

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_URL = "http://localhost:11434"
DEFAULT_CTX = 32768   # Ollama's own default (4k on smaller GPUs) is too small to hold Jarvis's tool list

OLLAMA_TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                for t in TOOLS]
SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}
THINK_TAGS = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _plain(text: str | None) -> str:
    """Local models often ignore 'no markdown': strip think tags, bold and headings so the chat stays readable."""
    text = THINK_TAGS.sub("", text or "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    return text.strip()


class OllamaError(Exception):
    pass


def check_args(name: str, raw) -> tuple[dict | None, str | None]:
    """Local models are less exact than Claude: fix types where it's safe, and explain anything else back to the model."""
    if name not in SCHEMAS:
        return None, f"There is no tool called '{name}'. Use one of: {', '.join(SCHEMAS)}."
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return None, f"The arguments for {name} weren't valid JSON."
    raw = raw if isinstance(raw, dict) else {}
    schema, out = SCHEMAS[name], {}
    for key, spec in schema["properties"].items():
        types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        v = raw.get(key)
        if v in (None, "", "null", "None"):
            if "null" in types:
                out[key] = None
                continue
            return None, f"{name} needs '{key}' ({spec.get('description', types[0])})."
        try:
            if "integer" in types and not isinstance(v, bool):
                v = int(float(v))
            elif "number" in types and not isinstance(v, bool):
                v = float(str(v).replace(",", "").lstrip("$£€₹"))
            elif "boolean" in types and not isinstance(v, bool):
                v = str(v).strip().lower() in ("true", "yes", "on", "1")
            elif "string" in types:
                v = str(v)
        except (TypeError, ValueError):
            return None, f"'{key}' for {name} should be {types[0]}, got {v!r}."
        if "enum" in spec:
            match = next((e for e in spec["enum"] if str(e).lower() == str(v).strip().lower()), None)
            if match is None:
                return None, f"'{key}' for {name} must be one of {spec['enum']}, got {v!r}."
            v = match
        out[key] = v
    return out, None


class OllamaBrain(Brain):
    def __init__(self, cfg: dict, assistant, latest_reports: Callable[[], list] | None = None):
        ai = cfg.get("ai") or {}
        self.cfg, self.assistant, self.latest_reports = cfg, assistant, latest_reports
        self.url = (ai.get("ollama_url") or DEFAULT_URL).rstrip("/")
        self.model = ai.get("model") or DEFAULT_MODEL
        if self.model.startswith("claude-"):   # switched provider but kept the old model name
            self.model = DEFAULT_MODEL
        self.num_ctx = int(ai.get("num_ctx") or DEFAULT_CTX)
        self.keep_alive = ai.get("keep_alive", "30m")  # keep the model loaded between questions
        # only sent if you choose on/off: not every model supports the setting
        self.think = {"off": False, "on": True, False: False, True: True}.get(ai.get("think"))
        self.messages: list = []
        self.usage = {"input": 0, "output": 0, "cache_read": 0}
        self.unreachable = False

    def _chat(self, messages: list, tools: bool = True) -> dict:
        body = {"model": self.model, "messages": [{"role": "system", "content": system_prompt(self.cfg, local=True)}] + messages,
                "stream": False, "keep_alive": self.keep_alive,
                "options": {"num_ctx": self.num_ctx, "temperature": 0.2}}
        if tools:
            body["tools"] = OLLAMA_TOOLS
        if self.think is not None:
            body["think"] = self.think
        try:
            r = requests.post(self.url + "/api/chat", json=body, timeout=600)
        except requests.ConnectionError:
            raise OllamaError(f"I can't reach Ollama at {self.url}. Is it running?")
        except requests.Timeout:
            raise OllamaError("The local model took over 10 minutes to answer. It may be too big for this machine.")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 404:
            raise OllamaError(f"The model '{self.model}' isn't installed. On the Ollama machine run: ollama pull {self.model}")
        if not r.ok or data.get("error"):
            raise OllamaError(f"Ollama error: {data.get('error') or f'HTTP {r.status_code}'}")
        self.usage["input"] += data.get("prompt_eval_count") or 0
        self.usage["output"] += data.get("eval_count") or 0
        return data

    def ask(self, text: str, say: Callable[[str], None] = print) -> str:
        if len(self.messages) > MAX_TURNS * 2:
            self.messages = []
            say("(Started a fresh conversation to keep things quick.)")
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        self.unreachable = True
        try:
            reply = self._local_loop(say)
            self.unreachable = False
            return reply
        except OllamaError as e:
            self.messages = self.messages[:checkpoint]
            return str(e)

    def _local_loop(self, say) -> str:
        for _ in range(MAX_STEPS):
            data = self._chat(self.messages)
            msg = data.get("message") or {}
            content = _plain(msg.get("content"))
            calls = msg.get("tool_calls") or []
            entry = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                entry["tool_calls"] = calls
            self.messages.append(entry)
            if not calls:
                if data.get("done_reason") == "length":
                    content += "\n(My answer got cut off. Ask me to continue.)"
                return content or "Done."
            if content:
                say(content)  # a local model sometimes explains before calling tools
            for c in calls:  # one at a time: each may wait for the user's yes/no
                fn = c.get("function") or {}
                name = fn.get("name", "")
                args, problem = check_args(name, fn.get("arguments"))
                if problem:
                    result = f"Error: {problem}"
                else:
                    line = describe(name, args)
                    if line:
                        say(line)
                    result, is_error = self.execute(name, args)
                    note = outcome(name, result, is_error)
                    if note:
                        say(note)
                    if is_error:
                        result = f"Error: {result}"
                self.messages.append({"role": "tool", "tool_name": name, "content": result})
        self.messages.append({"role": "user", "content": "Stop using tools now and tell me where things stand."})
        data = self._chat(self.messages, tools=False)
        final = _plain((data.get("message") or {}).get("content"))
        self.messages.append({"role": "assistant", "content": final})
        return final or "I ran out of steps on that one."


def test_ollama(cfg: dict) -> str:
    """Setup's 'Save & test': is Ollama reachable, is the model installed, can it use tools, how fast is it."""
    ai = cfg.get("ai") or {}
    url = (ai.get("ollama_url") or DEFAULT_URL).rstrip("/")
    model = ai.get("model") or DEFAULT_MODEL
    if model.startswith("claude-"):
        model = DEFAULT_MODEL
    try:
        version = requests.get(url + "/api/version", timeout=5).json().get("version", "?")
    except (requests.RequestException, ValueError):
        raise RuntimeError(f"Can't reach Ollama at {url}. Install it from ollama.com (or the ollama/ollama Docker image) and make sure it's running")
    names = [m.get("name", "") for m in requests.get(url + "/api/tags", timeout=10).json().get("models", [])]
    if model not in names and f"{model}:latest" not in names:
        have = ", ".join(names) if names else "none"
        raise RuntimeError(f"Ollama {version} is running but '{model}' isn't installed (installed: {have}). Run: ollama pull {model}")
    notes = []
    try:
        caps = requests.post(url + "/api/show", json={"model": model}, timeout=20).json().get("capabilities") or []
        if caps and "tools" not in caps:
            notes.append(f"warning: Ollama doesn't list tool support for {model}, so it may not be able to use Jarvis's tools")
    except (requests.RequestException, ValueError):
        pass
    started = time.monotonic()
    r = requests.post(url + "/api/chat", timeout=600, json={
        "model": model, "stream": False, "keep_alive": ai.get("keep_alive", "30m"),
        "options": {"num_ctx": int(ai.get("num_ctx") or DEFAULT_CTX)},
        **({"think": {"off": False, "on": True}[ai["think"]]} if ai.get("think") in ("off", "on") else {}),
        "messages": [{"role": "user", "content": "Reply with just the word: online"}]})
    data = r.json()
    if not r.ok or data.get("error"):
        raise RuntimeError(f"Ollama error: {data.get('error') or r.status_code}")
    secs = time.monotonic() - started
    speed = ""
    if data.get("eval_count") and data.get("eval_duration"):
        speed = f", {data['eval_count'] / (data['eval_duration'] / 1e9):.0f} tokens/s"
    reply = THINK_TAGS.sub("", data.get("message", {}).get("content", "")).strip()[:30]
    return f"Ollama {version}, {model} answered in {secs:.1f}s{speed}: {reply}" + (f" ({'; '.join(notes)})" if notes else "")
