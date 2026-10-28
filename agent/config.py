from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
DB_PATH = DATA_DIR / "apply.db"
LOG_DIR = ROOT / "logs"

load_dotenv(ROOT / ".env")


def _pin_googleapis() -> None:
    """Some networks (eduroam here) drop the IP ranges *.googleapis.com resolves to. Any Google front end
    serves every Google API, so resolve those hosts to www.google.com's IPv4 addresses instead.
    Disable with GOOGLEAPIS_PIN=0."""
    if os.environ.get("GOOGLEAPIS_PIN", "1") == "0":
        return
    import socket
    real = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):
        if isinstance(host, str) and host.endswith(".googleapis.com"):
            try:
                return real(os.environ.get("GOOGLEAPIS_VIA", "www.google.com"), port, socket.AF_INET, type, proto, flags)
            except OSError:
                pass
        return real(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched


_pin_googleapis()


def load_config(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or ROOT / "config.yaml").read_text())


def load_profile(path: Path | None = None) -> dict[str, Any]:
    profile = yaml.safe_load((path or ROOT / "profile.yaml").read_text())
    profile["resume_path"] = str((ROOT / profile["resume_path"]).resolve())
    return profile


def gemini_settings() -> tuple[str, str]:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing from .env")
    return key, os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")


def parse_duration(text: str) -> int:
    """'45m' -> 2700, '2h' -> 7200, '30s' -> 30, '1h30m' -> 5400."""
    parts = re.findall(r"(\d+)\s*([smhd])", text.strip().lower())
    if not parts:
        raise ValueError(f"bad duration: {text!r} (use e.g. 30s, 45m, 2h)")
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(int(n) * unit[u] for n, u in parts)


for d in (DATA_DIR, SCREENSHOT_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)
