"""HTTP for Google APIs. On eduroam, HTTP/1.1 to *.googleapis.com hangs and Python HTTP/2 (httpx+h2) stalls
on request bodies, while `curl --http2` is reliable, so Google calls go through curl, pinned to a reachable
front end (see config._pin_googleapis), with one retry on another IP. Headers are passed on stdin so
tokens and API keys never appear in the process list."""
from __future__ import annotations

import logging
import os
import random
import shutil
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def request(method: str, url: str, *, headers: dict[str, str] | None = None, data: str | bytes | None = None,
            timeout: int = 20, attempts: int = 3) -> tuple[int, str]:
    """Returns (status, body). Raises ConnectionError when no attempt got an HTTP response."""
    headers = headers or {}
    if not shutil.which("curl"):
        import httpx
        with httpx.Client(http2=True, timeout=timeout) as client:
            r = client.request(method, url, headers=headers, content=data)
            return r.status_code, r.text
    host = urlparse(url).hostname or ""
    ips: list[str] = []
    if host.endswith(".googleapis.com") and os.environ.get("GOOGLEAPIS_PIN", "1") != "0":
        ips = list({a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET)})
        random.shuffle(ips)
    header_lines = "".join(f"{k}: {v}\n" for k, v in headers.items())
    err = ""
    with tempfile.NamedTemporaryFile("wb", suffix=".body") as f:
        if data is not None:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
        for attempt in range(attempts):
            cmd = ["curl", "-sS", "--http2", "-m", str(timeout), "--connect-timeout", "8", "-X", method,
                   "-H", "@-", "-w", "\n%{http_code}"]
            if ips:
                cmd += ["--resolve", f"{host}:443:{ips[attempt % len(ips)]}"]
            if data is not None:
                cmd += ["--data-binary", f"@{f.name}"]
            p = subprocess.run(cmd + [url], input=header_lines or "\n", capture_output=True, text=True,
                               timeout=timeout + 10)
            if p.returncode == 0:
                body, _, code = p.stdout.rpartition("\n")
                return int(code or 0), body
            err = f"curl exit {p.returncode}: {p.stderr.strip()[:200]}"
            log.info("retrying %s %s (%d bytes) after %s", method, host, len(data or b""), err)
    raise ConnectionError(err)
