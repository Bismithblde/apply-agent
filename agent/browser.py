"""Playwright launcher. Headed under Xvfb when a DISPLAY is available (set by xvfb-run), headless otherwise."""
from __future__ import annotations

import logging
import os
import random
import shutil
import signal
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import ROOT

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


def _headless(config: dict, force_visible: bool) -> bool:
    if force_visible:
        return False
    if config.get("browser", {}).get("headless") is False:
        return False
    # Under xvfb-run there is a private X display: run headed (less bot-like), invisible to the user.
    return not os.environ.get("APPLY_AGENT_XVFB")


def profile_dir(config: dict, profile_suffix: str = "") -> Path:
    return ROOT / (config.get("browser", {}).get("user_data_dir", "browser-profile") + profile_suffix)


def kill_browser(user_dir: Path) -> int:
    """SIGKILL every Chromium process using `user_dir`. Safe from any thread; the blocked Playwright call
    in the owning thread then fails with "browser has been closed"."""
    needle = f"--user-data-dir={user_dir}\0".encode()  # cmdline args are NUL-terminated; avoids matching -w1 dirs
    killed = 0
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if needle in (proc / "cmdline").read_bytes():
                os.kill(int(proc.name), signal.SIGKILL)
                killed += 1
        except (OSError, ProcessLookupError):
            continue
    return killed


@contextmanager
def open_browser(config: dict, *, visible: bool = False, profile_suffix: str = "") -> Iterator[BrowserContext]:
    """Persistent Chromium context. `profile_suffix` gives parallel workers their own profile dir."""
    user_dir = profile_dir(config, profile_suffix)
    user_dir.mkdir(parents=True, exist_ok=True)
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = user_dir / lock
        if p.is_symlink() or p.exists():
            p.unlink(missing_ok=True)
    headless = _headless(config, visible)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(user_dir),
            headless=headless,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1366, "height": 900},
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
        )
        ctx.add_init_script(_STEALTH_JS)
        timeout_ms = int(config.get("page_timeout_seconds", 45)) * 1000
        ctx.set_default_timeout(timeout_ms)
        ctx.set_default_navigation_timeout(timeout_ms)

        def _handle_dialog(d):
            try:
                if getattr(d, "type", "") == "beforeunload":
                    d.accept()
                else:
                    d.dismiss()
            except Exception:
                pass

        ctx.on("page", lambda p: p.on("dialog", _handle_dialog))
        for p in ctx.pages:
            p.on("dialog", _handle_dialog)
        log.info("browser up (headless=%s, profile=%s)", headless, user_dir.name)
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def new_page(ctx: BrowserContext) -> Page:
    for p in ctx.pages[1:]:
        try:
            p.close()
        except Exception:
            pass
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def human_pause(lo: float = 0.3, hi: float = 1.1) -> None:
    time.sleep(random.uniform(lo, hi))


def xvfb_available() -> bool:
    return shutil.which("xvfb-run") is not None
