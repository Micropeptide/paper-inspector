"""Which AI model the investigator uses, and its key.  Stored only on this computer (settings.json, mode 600).

Two kinds of provider cover nearly every model:
- anthropic: Claude through the Anthropic SDK (key from here, ANTHROPIC_API_KEY, or an `ant auth login` profile)
- openai:    any OpenAI-compatible endpoint - OpenAI, DeepSeek, Gemini, OpenRouter, Groq, Mistral, xAI, Qwen,
             Moonshot/Kimi, a local Ollama or LM Studio...
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from typing import Any

from .pipeline import HOME

PATH = HOME / "settings.json"

PRESETS: dict[str, dict[str, Any]] = {
    "anthropic": {"kind": "anthropic", "label": "Anthropic (Claude)", "model": "claude-opus-5-5", "vision": True,
                  "env": "ANTHROPIC_API_KEY", "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                                                         "claude-fable-5-1"]},
    "openai": {"kind": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-5",
               "vision": True, "env": "OPENAI_API_KEY"},
    "gemini": {"kind": "openai", "label": "Google Gemini", "model": "gemini-2.5-pro", "vision": True,
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "env": "GEMINI_API_KEY"},
    "deepseek": {"kind": "openai", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "vision": False, "env": "DEEPSEEK_API_KEY"},
    "openrouter": {"kind": "openai", "label": "OpenRouter (hundreds of models)", "base_url": "https://openrouter.ai/api/v1",
                   "model": "anthropic/claude-opus-5.5", "vision": True, "env": "OPENROUTER_API_KEY"},
    "mistral": {"kind": "openai", "label": "Mistral", "base_url": "https://api.mistral.ai/v1",
                "model": "mistral-large-latest", "vision": True, "env": "MISTRAL_API_KEY"},
    "xai": {"kind": "openai", "label": "xAI Grok", "base_url": "https://api.x.ai/v1", "model": "grok-4",
            "vision": True, "env": "XAI_API_KEY"},
    "groq": {"kind": "openai", "label": "Groq", "base_url": "https://api.groq.com/openai/v1",
             "model": "meta-llama/llama-4-maverick-17b-128e-instruct", "vision": True, "env": "GROQ_API_KEY"},
    "qwen": {"kind": "openai", "label": "Qwen (DashScope)", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
             "model": "qwen-vl-max", "vision": True, "env": "DASHSCOPE_API_KEY"},
    "moonshot": {"kind": "openai", "label": "Moonshot Kimi", "base_url": "https://api.moonshot.ai/v1",
                 "model": "kimi-k2-0905-preview", "vision": False, "env": "MOONSHOT_API_KEY"},
    "ollama": {"kind": "openai", "label": "Ollama (local, free)", "base_url": "http://localhost:11434/v1",
               "model": "qwen2.5vl:7b", "vision": True, "env": "", "no_key": True},
    "lmstudio": {"kind": "openai", "label": "LM Studio (local, free)", "base_url": "http://localhost:1234/v1",
                 "model": "local-model", "vision": True, "env": "", "no_key": True},
    "custom": {"kind": "openai", "label": "Other OpenAI-compatible", "base_url": "", "model": "", "vision": True,
               "env": ""},
}

DEFAULTS = {"provider": "anthropic", "model": "", "base_url": "", "vision": None, "effort": "medium", "keys": {},
            "online": False, "email": ""}
EMAIL_RE = re.compile(r"[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

_LOCK = threading.Lock()


@contextmanager
def _locked():
    """One writer at a time: threads of this server through _LOCK, other processes (the CLI, a second server)
    through a lock file."""
    HOME.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fh = open(HOME / "settings.lock", "a")
        try:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:
                pass
            yield
        finally:
            fh.close()


def load() -> dict:
    """Saved settings over the defaults.  An unreadable file is set aside as settings.json.bad (it may hold API
    keys) instead of being silently replaced by defaults on the next save."""
    try:
        s = json.loads(PATH.read_text())
    except FileNotFoundError:
        s = {}
    except (OSError, ValueError) as e:
        s = _set_aside(f"{type(e).__name__}: {e}")
    if not isinstance(s, dict):
        s = _set_aside("not a JSON object")
    return {**DEFAULTS, **s}


def _set_aside(why: str) -> dict:
    bad = PATH.with_name(PATH.name + ".bad")
    try:
        if not bad.exists():
            shutil.copy2(PATH, bad)
            os.chmod(bad, 0o600)
        print(f"paperinspector: {PATH} is unreadable ({why}); a copy is kept at {bad}", file=sys.stderr)
    except OSError:
        pass
    return {}


def save(patch: dict) -> dict:
    patch = dict(patch)
    with _locked():
        s = load()
        keys = dict(s.get("keys") or {})
        for k, v in (patch.pop("keys", None) or {}).items():
            if v:
                keys[k] = v
            else:
                keys.pop(k, None)
        if "email" in patch:
            e = str(patch["email"] or "").strip()
            if e and not EMAIL_RE.fullmatch(e):
                patch.pop("email")          # a malformed address would be sent to public APIs; keep the old one
            else:
                patch["email"] = e
        if "effort" in patch and patch["effort"] not in EFFORTS:
            patch.pop("effort")             # an unknown level would make every request fail; keep the old one
        if patch.get("provider") not in (None, s.get("provider")) and "vision" not in patch:
            patch["vision"] = None          # 'can see images' belongs to the provider it was set for
        s.update({k: v for k, v in patch.items() if k in DEFAULTS})
        s["keys"] = keys
        fd, tmp = tempfile.mkstemp(dir=HOME, prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(s, fh, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, PATH)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return s


def resolved() -> dict:
    """The effective configuration: provider preset + user overrides + key source (never the key itself)."""
    s = load()
    pid = s["provider"] if s["provider"] in PRESETS else "anthropic"
    pre = PRESETS[pid]
    key = (s.get("keys") or {}).get(pid) or (os.environ.get(pre["env"]) if pre.get("env") else None)
    return {"provider": pid, "kind": pre["kind"], "model": s.get("model") or pre["model"],
            "base_url": s.get("base_url") or pre.get("base_url", ""),
            "vision": pre["vision"] if s.get("vision") is None else bool(s["vision"]),
            "effort": s.get("effort") or "medium", "key": key, "no_key": bool(pre.get("no_key")),
            "online": bool(s.get("online"))}


def contact_email() -> str:
    """The address the user chose to share with public APIs (Crossref, OpenAlex, NCBI ask for one to contact
    heavy users).  Empty unless the user set it in Settings: no address is ever sent otherwise."""
    e = str(load().get("email") or "").strip()
    return e if EMAIL_RE.fullmatch(e) else ""


def data_keys() -> dict:
    """Keys for scholarly data services (not AI models).  Optional; stored like the model keys."""
    return {"openalex": bool((load().get("keys") or {}).get("openalex"))}


def public() -> dict:
    s = load()
    r = resolved()
    keys = s.get("keys") or {}
    return {"provider": r["provider"], "model": r["model"], "base_url": r["base_url"], "vision": r["vision"],
            "effort": r["effort"], "online": r["online"], "data_keys": data_keys(), "email": contact_email(),
            "key_set": {pid: bool(keys.get(pid) or (p.get("env") and os.environ.get(p["env"])))
                        for pid, p in PRESETS.items()},
            "presets": {pid: {k: v for k, v in p.items() if k != "env"} | {"env": p.get("env", "")}
                        for pid, p in PRESETS.items()}}
