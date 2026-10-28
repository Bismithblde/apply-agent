"""`python -m agent doctor`: checks every dependency, key, and connection, and says how to fix what's missing."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx

from . import db
from .config import ROOT, load_config, load_profile

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _check(name: str, status: str, detail: str, fix: str = "") -> tuple[str, str, str, str]:
    return name, status, detail, fix


def run() -> list[tuple[str, str, str, str]]:
    out = []
    try:
        cfg = load_config()
        out.append(_check("config.yaml", OK, f"submit_mode={cfg.get('submit_mode')}, auto_ats={cfg.get('auto_ats')}"))
    except Exception as e:
        return [_check("config.yaml", FAIL, str(e), "fix the YAML syntax")]
    try:
        prof = load_profile()
        resume = Path(prof["resume_path"])
        out.append(_check("profile + resume", OK if resume.exists() else FAIL, str(resume),
                          "" if resume.exists() else "put your resume at data/resume.pdf"))
        if not (prof.get("personal", {}).get("address_line1") or "").strip():
            out.append(_check("Street address", WARN, "personal.address_line1 is blank",
                              "enter your street address in profile.yaml for Workday applications"))
    except Exception as e:
        out.append(_check("profile.yaml", FAIL, str(e), "fix profile.yaml"))

    try:
        from . import llm
        reply = llm.generate_text("Reply with just: ok")
        out.append(_check("Gemini", OK, f"replied {reply.strip()[:20]!r}"))
    except Exception as e:
        out.append(_check("Gemini", FAIL, str(e)[:160], "check GEMINI_API_KEY in .env (or set OPENROUTER_API_KEY)"))

    key = os.environ.get("CAPSOLVER_API_KEY", "")
    if not key:
        out.append(_check("CapSolver", WARN, "no CAPSOLVER_API_KEY", "add it to .env; CAPTCHA jobs are queued until then"))
    else:
        try:
            r = httpx.post("https://api.capsolver.com/getBalance", json={"clientKey": key}, timeout=15).json()
            if r.get("errorId"):
                out.append(_check("CapSolver", FAIL, r.get("errorDescription", "error"), "check CAPSOLVER_API_KEY"))
            else:
                bal = float(r.get("balance", 0))
                out.append(_check("CapSolver", OK if bal > 0.5 else WARN, f"balance ${bal:.2f}",
                                  "" if bal > 0.5 else "add funds at capsolver.com"))
        except Exception as e:
            out.append(_check("CapSolver", WARN, f"unreachable: {e}"))

    client = ROOT / os.environ.get("GMAIL_CLIENT_SECRET_FILE", "secrets/gmail_client.json")
    token = ROOT / os.environ.get("GMAIL_TOKEN_FILE", "secrets/gmail_token.json")
    if not client.exists():
        out.append(_check("Gmail OAuth", WARN, f"missing {client.relative_to(ROOT)}",
                          "see README 'Gmail OAuth'; email verification jobs are queued until then"))
    elif not token.exists():
        out.append(_check("Gmail OAuth", WARN, "not authorized yet", "run: .venv/bin/python -m agent auth gmail"))
    else:
        try:
            from . import mail
            ok = mail.available()
            out.append(_check("Gmail OAuth", OK if ok else FAIL, "API reachable" if ok else "token present but API failed",
                              "" if ok else "re-run: .venv/bin/python -m agent auth gmail"))
        except Exception as e:
            out.append(_check("Gmail OAuth", FAIL, str(e)[:160], "re-run: .venv/bin/python -m agent auth gmail"))

    if os.environ.get("VAULT_KEY"):
        try:
            from cryptography.fernet import Fernet
            Fernet(os.environ["VAULT_KEY"].encode())
            out.append(_check("Account vault", OK, f"{len(db.list_account_rows())} saved accounts"))
        except Exception as e:
            out.append(_check("Account vault", FAIL, f"bad VAULT_KEY: {e}", "regenerate with setup.sh (only if no accounts saved)"))
    else:
        out.append(_check("Account vault", FAIL, "no VAULT_KEY", "run ./setup.sh"))

    out.append(_check("Xvfb", OK if shutil.which("xvfb-run") else WARN,
                      "headed browser on hidden display" if shutil.which("xvfb-run") else "browser will run headless",
                      "" if shutil.which("xvfb-run") else "sudo dnf install -y xorg-x11-server-Xvfb"))
    out.append(_check("ntfy push", OK if os.environ.get("NTFY_TOPIC") else WARN,
                      "subscribe to the NTFY_TOPIC value from .env in the ntfy app" if os.environ.get("NTFY_TOPIC") else "no NTFY_TOPIC"))

    svc = Path.home() / ".config/systemd/user/apply-agent.service"
    out.append(_check("systemd service", OK if svc.exists() else WARN,
                      "installed" if svc.exists() else "not installed", "" if svc.exists() else "run ./setup.sh"))

    counts = db.status_counts()
    out.append(_check("job queue", OK if counts.get("new") else WARN,
                      ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty",
                      "" if counts.get("new") else "run: .venv/bin/python -m agent fetch"))
    return out


def main() -> int:
    results = run()
    width = max(len(r[0]) for r in results)
    for name, status, detail, fix in results:
        print(f"[{status:4}] {name:<{width}}  {detail}")
        if fix:
            print(f"       {'':<{width}}  -> {fix}")
    return 1 if any(r[1] == FAIL for r in results) else 0
