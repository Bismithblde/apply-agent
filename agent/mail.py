"""Read account verification email through Gmail's read-only REST API."""
from __future__ import annotations

import base64
import concurrent.futures as cf
import html
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Literal

from . import db, llm, vault
from .config import ROOT, load_config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_HOST = "gmail.googleapis.com"
POLL_SECONDS = 5
NET_TIMEOUT = 10
_CACHE_SECONDS = 60

Want = Literal["code", "link", "any"]


@dataclass
class Verification:
    code: str | None = field(repr=False)
    link: str | None = field(repr=False)
    message_id: str
    subject: str = field(repr=False)
    sender: str


@dataclass
class _Msg:
    id: str
    subject: str
    sender: str
    to: str
    ts: float
    text: str
    html: str
    thread_id: str = ""
    delivered_to: str = ""


class MailUnavailable(RuntimeError):
    pass


_lock = threading.Lock()
_backend_cache: tuple[float, str | None] = (0.0, None)
_pool = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail")


# ---------- config ----------

def _env_path(name: str) -> Path | None:
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    p = Path(v).expanduser()
    return p if p.is_absolute() else ROOT / p


def _default_timeout() -> float:
    try:
        return float(load_config().get("email", {}).get("verification_timeout_seconds", 180))
    except Exception:
        return 180.0


def _run(fn, timeout: float):
    """Run a blocking network call with a hard deadline."""
    timeout = max(0.0, timeout)
    if timeout == 0:
        raise MailUnavailable("mail call timed out")
    fut = _pool.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except cf.TimeoutError as e:
        fut.cancel()
        raise MailUnavailable(f"mail call timed out after {timeout:.0f}s") from e


# ---------- Gmail API ----------

# Use agent.net for Google requests; Python HTTP/1.1 clients hang on eduroam.
# The token file keeps the standard authorized-user JSON format.

def _client_info() -> dict:
    import json
    client = _env_path("GMAIL_CLIENT_SECRET_FILE")
    if not client or not client.exists():
        raise MailUnavailable("GMAIL_CLIENT_SECRET_FILE missing; see README 'Gmail OAuth'")
    data = json.loads(client.read_text())
    return data.get("installed") or data.get("web") or data


def _access_token() -> str:
    import json
    from datetime import datetime, timedelta, timezone

    from . import net
    token = _env_path("GMAIL_TOKEN_FILE")
    if not token or not token.exists():
        raise MailUnavailable("Gmail not authorized; run: python -m agent auth gmail")
    t = json.loads(token.read_text())
    expiry = t.get("expiry")
    if t.get("token") and expiry:
        exp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp - datetime.now(timezone.utc) > timedelta(seconds=120):
            return t["token"]
    if not t.get("refresh_token"):
        raise MailUnavailable("Gmail token has no refresh_token; run: python -m agent auth gmail")
    from urllib.parse import urlencode
    status, body = net.request(
        "POST", t.get("token_uri", "https://oauth2.googleapis.com/token"),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=NET_TIMEOUT,
        data=urlencode({"client_id": t["client_id"], "client_secret": t["client_secret"],
                        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}))
    if status != 200:
        raise MailUnavailable(f"Gmail token refresh failed (HTTP {status}); run: python -m agent auth gmail")
    r = json.loads(body)
    t["token"] = r["access_token"]
    t["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=int(r.get("expires_in", 3600)))).isoformat()
    token.write_text(json.dumps(t))
    token.chmod(0o600)
    return t["token"]


def _gapi(path: str, **params) -> dict:
    import json
    from urllib.parse import urlencode

    from . import net
    url = f"https://{GMAIL_HOST}/gmail/v1/users/me/{path}"
    if params:
        url += "?" + urlencode({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()})
    status, body = net.request("GET", url, headers={"Authorization": f"Bearer {_access_token()}"},
                               timeout=NET_TIMEOUT)
    if status != 200:
        raise MailUnavailable(f"Gmail API request failed (HTTP {status})")
    return json.loads(body)


def _gmail_ok() -> bool:
    token, client = _env_path("GMAIL_TOKEN_FILE"), _env_path("GMAIL_CLIENT_SECRET_FILE")
    if not token or not client or not token.exists() or not client.exists():
        return False
    try:
        _gapi("profile")
        return True
    except Exception as exc:
        log.info("Gmail API unavailable (%s)", type(exc).__name__)
        return False


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk_parts(part: dict, out: dict[str, list[str]]) -> None:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        out[mime].append(_b64(data))
    for p in part.get("parts", []) or []:
        _walk_parts(p, out)


def _gmail_list(query: str) -> list[_Msg]:
    msgs = []
    seen: set[str] = set()
    page = None
    while True:
        params = dict(q=query, maxResults=50, includeSpamTrash=True)
        if page:
            params["pageToken"] = page
        resp = _gapi("messages", **params)
        for ref in resp.get("messages", []):
            if ref["id"] in seen:
                continue
            seen.add(ref["id"])
            m = _gapi(f"messages/{ref['id']}", format="full")
            headers: dict[str, list[str]] = {}
            for h in m.get("payload", {}).get("headers", []):
                headers.setdefault(h["name"].lower(), []).append(h["value"])
            header = lambda *names: ", ".join(v for name in names for v in headers.get(name, []))
            parts: dict[str, list[str]] = {"text/plain": [], "text/html": []}
            _walk_parts(m.get("payload", {}), parts)
            msgs.append(_Msg(id=m["id"], subject=header("subject"), sender=header("from"),
                             to=header("to", "cc", "bcc"), ts=int(m.get("internalDate", 0)) / 1000,
                             text="\n".join(parts["text/plain"]), html="\n".join(parts["text/html"]),
                             thread_id=m.get("threadId", ""),
                             delivered_to=header("delivered-to", "x-original-to", "envelope-to")))
        page = resp.get("nextPageToken")
        if not page:
            break
    return msgs


# ---------- backend availability ----------

def _backend(refresh: bool = False) -> str | None:
    global _backend_cache
    with _lock:
        ts, name = _backend_cache
        if not refresh and time.time() - ts < _CACHE_SECONDS:
            return name
    name = "gmail" if _gmail_ok() else None
    with _lock:
        _backend_cache = (time.time(), name)
    return name


def available() -> bool:
    try:
        return _run(_backend, NET_TIMEOUT * 2 + 5) is not None
    except Exception as exc:
        log.info("Gmail API availability check failed (%s)", type(exc).__name__)
        return False


def authorize_interactive() -> None:
    """One-time OAuth consent (loopback + PKCE) in your browser; writes GMAIL_TOKEN_FILE."""
    global _backend_cache
    import hashlib
    import http.server
    import json
    import secrets
    import webbrowser
    from datetime import datetime, timedelta, timezone
    from urllib.parse import parse_qs, urlencode, urlparse

    from . import net
    info = _client_info()
    token = _env_path("GMAIL_TOKEN_FILE")
    if not token:
        raise MailUnavailable("GMAIL_TOKEN_FILE not set in .env")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if "code" in q or "error" in q:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>apply-agent: Gmail authorized. You can close this tab.</h2>"
                             if "code" in q else b"<h2>Authorization failed. Check the terminal.</h2>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = info.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") + "?" + urlencode({
        "client_id": info["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "login_hint": os.environ.get("APPLY_EMAIL", "")})
    print(f"Opening your browser for Gmail consent. If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)
    server.timeout = 300
    deadline = time.time() + 300
    while "code" not in got and "error" not in got and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if got.get("state") != state or "code" not in got:
        raise MailUnavailable(f"OAuth consent failed: {got.get('error', 'no code received')}")
    token_uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
    status, body = net.request("POST", token_uri, headers={"Content-Type": "application/x-www-form-urlencoded"},
                               data=urlencode({"code": got["code"], "client_id": info["client_id"],
                                               "client_secret": info.get("client_secret", ""),
                                               "redirect_uri": redirect, "grant_type": "authorization_code",
                                               "code_verifier": verifier}), timeout=NET_TIMEOUT * 2)
    if status != 200:
        raise MailUnavailable(f"Gmail token exchange failed (HTTP {status})")
    r = json.loads(body)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps({
        "token": r["access_token"], "refresh_token": r.get("refresh_token"), "token_uri": token_uri,
        "client_id": info["client_id"], "client_secret": info.get("client_secret", ""), "scopes": SCOPES,
        "expiry": (datetime.now(timezone.utc) + timedelta(seconds=int(r.get("expires_in", 3600)))).isoformat()}))
    token.chmod(0o600)
    _backend_cache = (0.0, None)


# ---------- extraction ----------

_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_URL = re.compile(r"""https?://[^\s"'<>)\]]+""", re.I)
_LINK_WORDS = re.compile(r"verif|activat|confirm|validat|token|registration", re.I)
_LINK_BAD = re.compile(r"unsubscribe|privacy|preferences|terms|/help|support|facebook|linkedin|twitter", re.I)
_CODE_WORDS = re.compile(r"\b(?:code|verification|verify|passcode|one[- ]time|otp|pin)\b", re.I)
_TOKEN = re.compile(r"\b([A-Za-z0-9]{4,8})\b")
_VERIFICATION_CONTEXT = re.compile(
    r"\b(?:verif\w*|authenticat\w*|passcode|one[- ]time|otp|security\s+code|sign[- ]?in|log[- ]?in|"
    r"(?:confirm|activat|validat)\w*\s+(?:your\s+)?(?:identity|e-?mail|account|registration))\b", re.I)
_CODE_EXPIRY = re.compile(
    r"\b(?:expir(?:e[sd]?|ing)|valid)\s+(?:in|after|for|within)\s+"
    r"(?:the\s+next\s+)?(\d{1,3})\s*(second|minute|hour)s?\b", re.I)


def html_to_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", body)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def extract_links(text: str, body_html: str = "") -> list[str]:
    urls = [html.unescape(u) for u in _HREF.findall(body_html)] + _URL.findall(text)
    seen, out = set(), []
    for u in urls:
        u = u.strip().rstrip(".,;")
        if u.startswith("http") and u not in seen and _LINK_WORDS.search(u) and not _LINK_BAD.search(u):
            seen.add(u)
            out.append(u)
    return out


def extract_code(text: str) -> str | None:
    """A 4-8 char code that sits near 'code'/'verification' and contains at least one digit."""
    for m in _CODE_WORDS.finditer(text):
        after = text[m.end():m.end() + 80]
        before = text[max(0, m.start() - 40):m.start()]
        for window in (after, before):
            for t in _TOKEN.findall(window):
                if any(c.isdigit() for c in t) and not (len(t) == 4 and t.isdigit() and 1900 <= int(t) <= 2100):
                    return t
    return None


_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": ["string", "null"], "description": "verification/one-time code, or null"},
        "link": {"type": ["string", "null"], "description": "URL that verifies/activates the account, or null"},
    },
    "required": ["code", "link"],
}


def _llm_extract(subject: str, text: str, links: list[str]) -> tuple[str | None, str | None]:
    prompt = ("Extract the account verification code and/or the verification link from this email. "
              "Only return values that appear verbatim in the email; use null otherwise.\n\n"
              f"Subject: {subject}\n\n{text[:6000]}")
    try:
        out = llm.generate_json(prompt, _LLM_SCHEMA)
    except llm.LLMUnavailable:
        return None, None
    code, link = (out or {}).get("code"), (out or {}).get("link")
    code = code if isinstance(code, str) and 4 <= len(code.strip()) <= 8 and code.strip() in text else None
    link = link if isinstance(link, str) and link.startswith("http") and link in text + " ".join(links) else None
    return (code.strip() if code else None), link


def parse_message(subject: str, text: str, body_html: str = "", *,
                  use_llm: bool = True) -> tuple[str | None, str | None]:
    """(code, link) from one email body."""
    plain = (text or "") + "\n" + (html_to_text(body_html) if body_html else "")
    links = extract_links(plain, body_html)
    code = extract_code(subject + "\n" + plain)
    link = links[0] if links else None
    if not code and not link and use_llm:
        code, link = _llm_extract(subject, plain, links)
    return code, link


def _satisfies(code: str | None, link: str | None, want: Want) -> bool:
    return bool(code) if want == "code" else bool(link) if want == "link" else bool(code or link)


def build_query(after_ts: float, sender_hint: str | None, to_alias: str | None) -> str:
    q = [f"after:{int(after_ts)}"]
    if sender_hint:
        q.append(f"from:{sender_hint}")
    if to_alias:
        q.append(f"to:{to_alias}")
    return " ".join(q)


def _recipient_query(after_ts: float, recipient: str) -> str:
    terms = [f"{op}:{address}" for address in vault.email_search_addresses(recipient)
             for op in ("to", "deliveredto")]
    return build_query(after_ts, None, None) + " {" + " ".join(terms) + "}"


def _recipient_matches(message: _Msg, recipient: str) -> bool:
    expected = vault.canonical_email(recipient)
    if not expected:
        return False
    addresses = {vault.canonical_email(address) for _, address in getaddresses([message.to]) if address}
    if expected in addresses:
        return True
    base = lambda address: re.sub(r"\+[^@]*@", "@", address)
    if any(base(address) == base(expected) for address in addresses):
        return False
    return any(vault.canonical_email(address) == expected
               for _, address in getaddresses([message.delivered_to]) if address)


def acknowledge_verification(verification: Verification, *, want: Want = "any") -> None:
    """Consume a deferred result after complete entry, before attempting verification."""
    db.mark_mail_seen(verification.message_id, purpose=f"verification:{want}")


def wait_for_verification(after_ts: float, *, sender_hint: str | None = None, to_alias: str | None = None,
                          timeout: float | None = None, want: Want = "any",
                          consume: bool = True) -> Verification | None:
    """Poll for fresh verification mail; None on timeout or when mail is down.

    With consume=False, acknowledge only after verifying complete entry. Retry through
    this function with the current request timestamp so expiry and newer mail are rechecked.
    """
    global _backend_cache
    deadline = time.monotonic() + max(0.0, timeout if timeout is not None else _default_timeout())
    queries = [(build_query(after_ts, sender_hint, to_alias), False)]
    if to_alias:
        fallback = _recipient_query(after_ts, to_alias)
        if fallback != queries[0][0]:
            queries.append((fallback, True))
    parsed: dict[str, tuple[str | None, str | None]] = {}
    counts = dict(polls=0, messages=0, stale=0, recipient_mismatch=0, consumed=0, expired=0,
                  unparsed=0, ambiguous=0)
    while time.monotonic() < deadline:
        for query, fallback in queries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msgs = _run(lambda: _gmail_list(query), remaining)
            except Exception as exc:
                if isinstance(exc, MailUnavailable) and time.monotonic() >= deadline:
                    break
                log.warning("Gmail verification poll unavailable (%s)", type(exc).__name__)
                with _lock:
                    _backend_cache = (0.0, None)
                return None
            counts["polls"] += 1
            counts["messages"] += len(msgs)
            latest: dict[str, _Msg] = {}
            for m in sorted(msgs, key=lambda m: -m.ts):
                if m.ts < after_ts:
                    counts["stale"] += 1
                    continue
                if to_alias and not _recipient_matches(m, to_alias):
                    counts["recipient_mismatch"] += 1
                    continue
                sender = (parseaddr(m.sender)[1] or m.sender).casefold()
                latest.setdefault(m.thread_id or sender, m)
            candidates = []
            for m in latest.values():
                if db.mail_is_seen(m.id):
                    counts["consumed"] += 1
                    continue
                if fallback and not _VERIFICATION_CONTEXT.search(
                        m.subject + "\n" + m.text + "\n" + html_to_text(m.html)):
                    counts["unparsed"] += 1
                    continue
                if m.id not in parsed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        parsed[m.id] = _run(lambda: parse_message(
                            m.subject, m.text, m.html, use_llm=llm.available()), remaining)
                    except MailUnavailable:
                        if time.monotonic() >= deadline:
                            break
                        return None
                code, link = parsed[m.id]
                if code:
                    expiry = _CODE_EXPIRY.search(m.text + "\n" + html_to_text(m.html))
                    if expiry:
                        lifetime = int(expiry[1]) * {"second": 1, "minute": 60, "hour": 3600}[expiry[2].lower()]
                        if time.time() >= m.ts + lifetime:
                            counts["expired"] += 1
                            code = None
                if _satisfies(code, link, want):
                    candidates.append((m, code, link))
                else:
                    counts["unparsed"] += 1
            if time.monotonic() >= deadline:
                break
            if len(candidates) == 1:
                m, code, link = candidates[0]
                verification = Verification(code=code, link=link, message_id=m.id,
                                            subject=m.subject, sender=m.sender)
                if consume:
                    acknowledge_verification(verification, want=want)
                log.info("Gmail verification matched (recipient_fallback=%s, consumed=%s)", fallback, consume)
                return verification
            if len(candidates) > 1:
                counts["ambiguous"] += 1
        time.sleep(max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))
    log.info("Gmail verification timed out (%s)", counts)
    return None
