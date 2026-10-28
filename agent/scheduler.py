"""Long-running loop: fetch sources and drain the apply queue on the intervals in config.yaml.
Config is re-read every tick, so edits to intervals, caps, or submit_mode apply without a restart."""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time

from . import db, report
from .config import ROOT, load_config, parse_duration
from .notify import notify

log = logging.getLogger(__name__)

TICK_SECONDS = 30
EX_TEMPFAIL = 75


class ApplyLockBusy(RuntimeError):
    """An apply child reported that another process owns the shared browser lock."""


def _in_active_hours(spec: str) -> bool:
    try:
        start, end = spec.split("-")
        now = time.strftime("%H:%M")
        return start <= now <= end if start <= end else (now >= start or now <= end)
    except ValueError:
        return True


def user_idle_seconds() -> float | None:
    """GNOME/Mutter idle time (works on Wayland). None if unknown."""
    if not shutil.which("gdbus"):
        return None
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
             "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
             "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
            capture_output=True, text=True, timeout=5).stdout
        return int(out.strip("()\n ,").split()[-1].rstrip(",")) / 1000
    except Exception:
        return None


def _due(key: str, every: str) -> bool:
    last = float(db.kv_get(key, "0") or 0)
    return time.time() - last >= parse_duration(every)


def fetch_once(config: dict) -> dict:
    from .fetch import fetch_all
    stats = fetch_all(config)
    log.info("fetch: fetched=%s matched=%s new=%s", stats.get("fetched"), stats.get("matched"), stats.get("new"))
    return stats


def _run_apply_subprocess(timeout: float, cmd: list[str] | None = None) -> None:
    """Run one batch in a child process group so a hung browser/page can be killed without wedging the loop."""
    proc = subprocess.Popen(cmd or [sys.executable, "-m", "agent", "apply"], cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("apply batch exceeded %.0f min; killing it", timeout / 60)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=20)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:  # Chromium children may ignore SIGTERM and would keep the profile locked
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return
    if returncode == EX_TEMPFAIL:
        raise ApplyLockBusy("apply deferred: apply/review lock is busy; it will be retried")
    elif returncode != 0:
        log.warning("apply batch exited with code %d", returncode)


def _last_apply_run(since: float) -> dict | None:
    """Counts from the applications table, so a batch killed before end_run still reports what it did."""
    with db.connect() as c:
        r = c.execute("SELECT id FROM runs WHERE kind='apply' AND started_at>=? ORDER BY id DESC LIMIT 1",
                      (since,)).fetchone()
        if not r:
            return None
        counts = dict(c.execute("SELECT status, COUNT(*) FROM applications WHERE run_id=? GROUP BY status",
                                (r[0],)).fetchall())
    return {"id": r[0], "submitted": counts.get("submitted", 0), "queued": counts.get("needs_review", 0),
            "failed": counts.get("failed", 0)}


def apply_once(config: dict, profile: dict | None = None) -> None:
    started = time.time()
    try:
        timeout = parse_duration(str(config.get("schedule", {}).get("max_run_time", "3h")))
        _run_apply_subprocess(timeout)
    except ApplyLockBusy as exc:
        log.info("%s", exc)
        return
    except BaseException:
        db.kv_set("last_apply", str(time.time()))
        raise
    else:
        db.kv_set("last_apply", str(time.time()))
    run = _last_apply_run(started) or {"id": "?", "submitted": 0, "queued": 0, "failed": 0}
    path, summary = report.build()
    log.info("apply run %s done: %s (report %s)", run["id"], summary, path)
    if run["submitted"] or run["queued"]:
        notify("apply-agent run finished",
               f"This run: {run['submitted']} submitted, {run['queued']} queued, {run['failed']} failed.\n"
               f"Tonight: {summary}", config)


def loop(apply_every: str | None = None) -> None:
    log.info("scheduler started")
    morning_sent = db.kv_get("morning_sent", "")
    while True:
        try:
            config = load_config()
            sched = config.get("schedule", {})
            if not _in_active_hours(sched.get("active_hours", "00:00-23:59")):
                time.sleep(TICK_SECONDS)
                continue
            if not sched.get("run_while_user_active", True):
                idle = user_idle_seconds()
                if idle is not None and idle < 300:
                    time.sleep(TICK_SECONDS)
                    continue
            if _due("last_fetch", sched.get("fetch_every", "30m")):
                try:
                    fetch_once(config)
                except Exception:
                    log.exception("fetch failed")
            if _due("last_apply", apply_every or sched.get("apply_every") or config.get("loop_every", "45m")):
                apply_once(config)
            today = time.strftime("%Y-%m-%d")
            if time.localtime().tm_hour >= 8 and morning_sent != today:
                path, summary = report.build()
                notify("Good morning: apply-agent report", f"{summary}\nReport: {path}", config, urgent=True)
                morning_sent = today
                db.kv_set("morning_sent", today)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(TICK_SECONDS)
