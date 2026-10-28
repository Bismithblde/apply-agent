from __future__ import annotations

import logging
import os
import shutil
import subprocess

import httpx

log = logging.getLogger(__name__)


def notify(title: str, body: str, config: dict | None = None, *, urgent: bool = False) -> None:
    ncfg = (config or {}).get("notify", {})
    if ncfg.get("desktop", True) and shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "apply-agent", "-u", "critical" if urgent else "normal",
                            title, body[:500]], timeout=5, check=False, capture_output=True)
        except Exception as e:
            log.debug("notify-send failed: %s", e)
    topic = os.environ.get("NTFY_TOPIC", "")
    if ncfg.get("ntfy", True) and topic:
        try:
            httpx.post(f"https://ntfy.sh/{topic}", content=body.encode()[:4000], timeout=10,
                       headers={"Title": title, "Priority": "high" if urgent else "default", "Tags": "briefcase"})
        except Exception as e:
            log.debug("ntfy failed: %s", e)
