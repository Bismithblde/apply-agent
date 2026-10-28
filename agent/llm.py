"""Thin Gemini wrapper. All calls have a hard timeout and a circuit breaker, so a dead network
degrades to LLMUnavailable quickly instead of stalling the run."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from .config import gemini_settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Gemini could not be reached or returned unusable output. Callers should treat answers as unknown."""


_lock = threading.Lock()
_failures = 0
_open_until = 0.0
_BREAKER_THRESHOLD = 2
_BREAKER_SECONDS = 600
# On eduroam, HTTP/2 uploads to googleapis stall past ~70 KB (flow-control window never reopens).
MAX_REQUEST_BYTES = 52_000


class _Img:
    def __init__(self, data: bytes, mime: str):
        self.data, self.mime = data, mime


def _shrink_image(png: bytes, budget: int) -> _Img:
    """Re-encode a screenshot as JPEG, downscaling until it fits `budget` raw bytes."""
    import io

    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    out = b""
    for scale in (1.0, 0.8, 0.65, 0.5, 0.4, 0.3):
        w, h = int(im.width * scale), int(im.height * scale)
        frame = im if scale == 1.0 else im.resize((w, h), Image.LANCZOS)
        for q in (60, 45, 32):
            buf = io.BytesIO()
            frame.save(buf, "JPEG", quality=q, optimize=True)
            out = buf.getvalue()
            if len(out) <= budget:
                return _Img(out, "image/jpeg")
    return _Img(out, "image/jpeg")


def _fit_text(text: str, budget: int) -> str:
    raw = text.encode()
    if len(raw) <= budget:
        return text
    log.warning("prompt %d bytes over budget %d; trimming the middle", len(raw), budget)
    half = budget // 2 - 40
    return raw[:half].decode(errors="ignore") + "\n...[trimmed]...\n" + raw[-half:].decode(errors="ignore")


def _fit_body(body: dict) -> bytes:
    """Serialize exactly as sent and shrink images/text until the real byte size fits MAX_REQUEST_BYTES."""
    import base64
    for _ in range(6):
        payload = json.dumps(body, ensure_ascii=False).encode()
        over = len(payload) - MAX_REQUEST_BYTES
        if over <= 0:
            return payload
        parts = body["contents"][0]["parts"]
        imgs = [p for p in parts if "inline_data" in p]
        if imgs:
            for p in imgs:
                raw = base64.b64decode(p["inline_data"]["data"])
                small = _shrink_image(raw, max(4000, int(len(raw) * 0.6) - over * 3 // 4 // len(imgs)))
                p["inline_data"] = {"mime_type": small.mime, "data": base64.b64encode(small.data).decode()}
        else:
            texts = [p for p in parts if "text" in p]
            big = max(texts, key=lambda p: len(p["text"]))
            big["text"] = _fit_text(big["text"], max(2000, len(big["text"].encode()) - over - 500))
    return json.dumps(body, ensure_ascii=False).encode()


def _post_payload(url: str, api_key: str, payload: bytes, timeout: int = 60) -> tuple[int, str]:
    # Thinking models can take >20 s on a full page screenshot; give each try time to answer.
    from . import net
    return net.request("POST", url, headers={"Content-Type": "application/json; charset=utf-8",
                                             "x-goog-api-key": api_key}, data=payload, timeout=timeout,
                       attempts=2)


def _record(ok: bool) -> None:
    global _failures, _open_until
    with _lock:
        if ok:
            _failures = 0
        else:
            _failures += 1
            if _failures >= _BREAKER_THRESHOLD:
                _open_until = time.time() + _BREAKER_SECONDS
                log.warning("Gemini unreachable; pausing LLM calls for %ds", _BREAKER_SECONDS)


def available() -> bool:
    return time.time() >= _open_until


def _openai_settings() -> tuple[str, str, str] | None:
    """OpenAI-compatible fallback (OpenAI or OpenRouter). Used when LLM_PROVIDER=openai or Gemini is unreachable."""
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    if os.environ.get("OPENROUTER_API_KEY"):
        base = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
        model = os.environ.get("OPENAI_MODEL", "google/gemini-2.5-flash")
    else:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    return key, base.rstrip("/"), model


def _openai_generate(contents: list[Any], *, json_schema: dict | None, temperature: float,
                     system: str | None) -> str:
    import base64

    import httpx
    settings = _openai_settings()
    if settings is None:
        raise LLMUnavailable("Gemini unreachable and no OPENROUTER_API_KEY/OPENAI_API_KEY set")
    key, base, model = settings
    parts: list[dict] = []
    for c in contents:
        if isinstance(c, str):
            parts.append({"type": "text", "text": c})
        elif isinstance(c, _Img):
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{c.mime};base64," + base64.b64encode(c.data).decode()}})
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": parts}]
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if json_schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "result", "schema": json_schema}}
    try:
        r = httpx.post(f"{base}/chat/completions", json=body, timeout=60,
                       headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise LLMUnavailable(f"fallback {type(e).__name__}: {e}") from e
    if not text.strip():
        raise LLMUnavailable("empty response")
    return text


def _generate(contents: list[Any], *, model: str | None, json_schema: dict | None,
              temperature: float, system: str | None) -> str:
    has_fallback = _openai_settings() is not None
    if has_fallback and (os.environ.get("LLM_PROVIDER", "").lower() == "openai" or not available()):
        return _openai_generate(contents, json_schema=json_schema, temperature=temperature, system=system)
    if not available():
        raise LLMUnavailable("Gemini circuit breaker open and no fallback key")
    try:
        return _gemini_generate(contents, model=model, json_schema=json_schema, temperature=temperature,
                                system=system)
    except LLMUnavailable:
        if _openai_settings() is None:
            raise
        return _openai_generate(contents, json_schema=json_schema, temperature=temperature, system=system)


def _gemini_generate(contents: list[Any], *, model: str | None, json_schema: dict | None,
                     temperature: float, system: str | None) -> str:
    import base64
    key, default_model = gemini_settings()
    parts = [{"inline_data": {"mime_type": c.mime, "data": base64.b64encode(c.data).decode()}}
             if isinstance(c, _Img) else {"text": c} for c in contents]
    gen: dict[str, Any] = {"temperature": temperature}
    if json_schema is not None:
        gen["responseMimeType"] = "application/json"
        gen["responseJsonSchema"] = json_schema
    # Full thinking on a page screenshot can exceed a minute; "low" keeps vision steps to seconds.
    level = os.environ.get("GEMINI_THINKING_LEVEL", "low").strip()
    if level and level.lower() != "default":
        gen["thinkingConfig"] = {"thinkingLevel": level}
    body: dict[str, Any] = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model or default_model}:generateContent"
    try:
        status, resp_text = _post_payload(url, key, _fit_body(body))
        if status == 400 and "thinking" in resp_text.lower() and gen.pop("thinkingConfig", None):
            status, resp_text = _post_payload(url, key, _fit_body(body))
    except Exception as e:  # network errors, timeouts
        _record(False)
        raise LLMUnavailable(f"{type(e).__name__}: {e}") from e
    if status >= 500 or status in (0, 429):
        _record(False)
        raise LLMUnavailable(f"HTTP {status}: {resp_text[:200]}")
    if status != 200:
        raise LLMUnavailable(f"HTTP {status}: {resp_text[:300]}")
    _record(True)
    try:
        cand = json.loads(resp_text)["candidates"][0]
        text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
    except (KeyError, IndexError, ValueError) as e:
        raise LLMUnavailable(f"unexpected response: {resp_text[:200]}") from e
    if not text.strip():
        raise LLMUnavailable("empty response")
    return text


def generate_text(prompt: str, *, model: str | None = None, temperature: float = 0.4,
                  system: str | None = None) -> str:
    prompt = _fit_text(prompt, MAX_REQUEST_BYTES - len((system or "").encode()) - 3000)
    return _generate([prompt], model=model, json_schema=None, temperature=temperature, system=system)


def generate_json(prompt: str, schema: dict, *, images: list[bytes] | None = None, model: str | None = None,
                  temperature: float = 0.2, system: str | None = None) -> Any:
    """Structured output. `schema` is a JSON Schema dict. `images` are PNG bytes (for the vision agent)."""
    images = images or []
    sys_len = len((system or "").encode())
    # Images go base64 (x4/3) into the JSON body; leave ~45% of the budget for them when present.
    text_budget = MAX_REQUEST_BYTES - sys_len - 3000 - (int(MAX_REQUEST_BYTES * 0.45) if images else 0)
    prompt = _fit_text(prompt, max(text_budget, 8000))
    img_budget = (MAX_REQUEST_BYTES - sys_len - len(prompt.encode()) - 3000) * 3 // 4 // max(len(images), 1)
    contents: list[Any] = [prompt, *(_shrink_image(b, max(img_budget, 6000)) for b in images)]
    text = _generate(contents, model=model, json_schema=schema, temperature=temperature, system=system)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"bad JSON from model: {text[:200]}") from e


def vision_model() -> str:
    return os.environ.get("GEMINI_VISION_MODEL") or gemini_settings()[1]


_OPTION_PICK_CACHE: dict[tuple[tuple[str, ...], str, str], int | None] = {}


def pick_option(
    options: list[str],
    value: Any,
    label: str = "",
    *,
    context: str = ""
) -> int | None:
    """Use Gemini to pick the best matching option index from a list of options.
    Returns the integer index in options (0-based), or None if no match is found."""
    if not options or value in (None, ""):
        return None
    cache_key = (tuple(options), str(value).strip().lower(), (label or "").strip().lower())
    if cache_key in _OPTION_PICK_CACHE:
        return _OPTION_PICK_CACHE[cache_key]

    formatted_opts = "\n".join(f"{i}: {opt}" for i, opt in enumerate(options))
    prompt = f"""You are an autonomous job application filler. Select the option from the list that best matches the applicant's fact, answer, or intent.

Field / Question: {label or 'Not specified'}
Applicant value / intent: {value}
{f'Additional Context: {context}' if context else ''}

Options:
{formatted_opts}

Rules:
1. NEVER select placeholder options like 'Select One', 'Select', 'Choose...', or empty string unless the applicant's intent is explicitly to select none.
2. If the applicant wants to decline or prefer not to say, pick a decline option if available. If options are only Yes/No (binary) with no decline option, choose 'No'.
3. If an exact match is not found (e.g. source is 'Simplify' or 'Job Board'), choose the most appropriate category (e.g. 'Other', 'Internet', 'Job Board', or a general online source).
4. Return the exact integer index of the chosen option (0 to {len(options) - 1}). If no option can reasonably match, return -1."""

    schema = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "reason": {"type": "string"}
        },
        "required": ["index"]
    }
    try:
        res = generate_json(prompt, schema, temperature=0.1)
        idx = res.get("index")
        if isinstance(idx, int) and 0 <= idx < len(options):
            _OPTION_PICK_CACHE[cache_key] = idx
            return idx
    except Exception as e:
        log.info("pick_option LLM call failed for %r in %s: %s", value, label, e)
    _OPTION_PICK_CACHE[cache_key] = None
    return None
