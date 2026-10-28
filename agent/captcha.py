"""CAPTCHA detection and solving through CapSolver, with a nightly spend cap."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Literal

import requests

from . import db

log = logging.getLogger(__name__)
_SPEND_LOCK = threading.Lock()

API = "https://api.capsolver.com"

# Approximate CapSolver prices per solve (USD).
COST = {
    "recaptcha_v2": 0.0008,
    "recaptcha_v2_invisible": 0.0008,
    "recaptcha_v2_enterprise": 0.003,
    "recaptcha_v3": 0.001,
    "recaptcha_v3_enterprise": 0.003,
    "turnstile": 0.0012,
}

# Widgets that run on their own when the form is submitted; no solver, but they need not block submit.
PASSIVE_KINDS = {"hcaptcha_invisible"}


@dataclass
class CaptchaOutcome:
    status: Literal["none", "solved", "failed", "budget", "unsupported"]
    kind: str = ""
    cost: float = 0.0
    detail: str = ""


# With captcha.passive_invisible, score-based/invisible widgets also run on their own at submit; a visible
# challenge afterwards is caught by the adapters' post-submit check and the job is queued.
PASSIVE_WHEN_ENABLED = {"recaptcha_v3", "recaptcha_v3_enterprise", "recaptcha_v2_invisible"}


def blocks_submit(outcome: CaptchaOutcome, config: dict | None = None) -> bool:
    """True when a captcha is present that we could not handle and the page will not handle itself."""
    if outcome.status not in ("failed", "budget", "unsupported"):
        return False
    passive = set(PASSIVE_KINDS)
    if (config or {}).get("captcha", {}).get("passive_invisible", True):
        passive |= PASSIVE_WHEN_ENABLED
    return outcome.kind not in passive


_DETECT_JS = r"""
() => {
  const q = s => [...document.querySelectorAll(s)];
  const param = (url, k) => {
    try { const u = new URL(url, location.href);
      return u.searchParams.get(k) || new URLSearchParams(u.hash.slice(1)).get(k); } catch (e) { return null; }
  };
  const shown = el => {
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    for (let a = el; a; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    }
    const r = el.getBoundingClientRect(); return r.width > 30 && r.height > 30;
  };
  const out = [];
  let action = '';
  for (const s of q('script:not([src])')) {
    const m = (s.textContent || '').match(/execute\([^)]*action\s*:\s*['"]([\w\/-]+)['"]/);
    if (m) { action = m[1]; break; }
  }
  for (const s of q('script[src]')) {
    if (!/recaptcha\/(api|enterprise)\.js/.test(s.src)) continue;
    const r = param(s.src, 'render');
    if (r && r !== 'explicit' && r !== 'onload')
      out.push({type: 'recaptcha', v3: true, enterprise: /enterprise\.js/.test(s.src), sitekey: r, invisible: true, action});
  }
  for (const f of q('iframe[src*="recaptcha"]')) {
    if (!/\/anchor\?/.test(f.src)) continue;
    out.push({type: 'recaptcha', v3: false, enterprise: /enterprise/.test(f.src), sitekey: param(f.src, 'k'),
              invisible: param(f.src, 'size') === 'invisible', visible: shown(f)});
  }
  for (const d of q('.g-recaptcha[data-sitekey]')) {
    out.push({type: 'recaptcha', v3: false, enterprise: false, sitekey: d.dataset.sitekey,
              invisible: d.dataset.size === 'invisible', callback: d.dataset.callback || '', action: d.dataset.action || action});
  }
  const hcInvisible = !!document.querySelector('iframe[src*="hcaptcha"][src*="checkbox-invisible"]');
  for (const d of q('.h-captcha[data-sitekey], [data-hcaptcha-sitekey]')) {
    out.push({type: 'hcaptcha', sitekey: d.dataset.sitekey || d.dataset.hcaptchaSitekey,
              invisible: d.dataset.size === 'invisible' || hcInvisible, callback: d.dataset.callback || '',
              visible: shown(d)});
  }
  for (const f of q('iframe[src*="hcaptcha.com"]')) {
    const k = param(f.src, 'sitekey');
    if (k) out.push({type: 'hcaptcha', sitekey: k, invisible: hcInvisible, visible: shown(f),
                     challenge: /frame=challenge/.test(f.src)});
  }
  for (const d of q('.cf-turnstile[data-sitekey]')) {
    out.push({type: 'turnstile', sitekey: d.dataset.sitekey, action: d.dataset.action || '', callback: d.dataset.callback || ''});
  }
  for (const f of q('iframe[src*="challenges.cloudflare.com"]')) {
    const m = f.src.match(/\/(0x[0-9A-Za-z_-]{10,})\//);
    if (m) out.push({type: 'turnstile', sitekey: m[1], action: '', callback: ''});
  }
  return out;
}
"""

_INJECT_RECAPTCHA_JS = r"""
([token, v3, cb]) => {
  const patch = o => {
    if (o && typeof o.execute === 'function' && !o.__aaPatched) {
      o.execute = function () { return Promise.resolve(token); };
      o.__aaPatched = true;
    }
  };
  if (v3 && window.grecaptcha) { patch(window.grecaptcha); patch(window.grecaptcha.enterprise); }
  document.querySelectorAll('textarea[name="g-recaptcha-response"], #g-recaptcha-response, [id^="g-recaptcha-response"]')
    .forEach(t => { t.value = token; t.innerHTML = token; });
  if (v3) return 'patched';
  if (cb && typeof window[cb] === 'function') { try { window[cb](token); return 'callback'; } catch (e) { return 'callback-error'; } }
  const cfg = window.___grecaptcha_cfg;
  if (!cfg || !cfg.clients) return 'no-cfg';
  for (const id in cfg.clients) {
    const stack = [[cfg.clients[id], 0]];
    while (stack.length) {
      const [o, d] = stack.pop();
      if (!o || typeof o !== 'object' || d > 6) continue;
      for (const k of Object.keys(o)) {
        let v; try { v = o[k]; } catch (e) { continue; }
        if (k === 'callback') {
          if (typeof v === 'function') { try { v(token); return 'callback'; } catch (e) { return 'callback-error'; } }
          if (typeof v === 'string' && typeof window[v] === 'function') { try { window[v](token); return 'callback'; } catch (e) { return 'callback-error'; } }
        }
        if (v && typeof v === 'object' && !(v instanceof Node)) stack.push([v, d + 1]);
      }
    }
  }
  return 'no-callback';
}
"""

_INJECT_TURNSTILE_JS = r"""
([token, cb]) => {
  document.querySelectorAll('[name="cf-turnstile-response"]').forEach(t => { t.value = token; });
  const name = cb || (document.querySelector('.cf-turnstile[data-callback]') || {dataset: {}}).dataset.callback;
  if (name && typeof window[name] === 'function') { try { window[name](token); return 'callback'; } catch (e) { return 'callback-error'; } }
  return 'injected';
}
"""

def _classify(w: dict) -> str:
    if w["type"] == "recaptcha":
        if w.get("v3"):
            return "recaptcha_v3_enterprise" if w.get("enterprise") else "recaptcha_v3"
        if w.get("enterprise"):
            return "recaptcha_v2_enterprise"
        return "recaptcha_v2_invisible" if w.get("invisible") else "recaptcha_v2"
    if w["type"] == "hcaptcha":
        if w.get("challenge") and w.get("visible"):
            return "hcaptcha"
        return "hcaptcha_invisible" if w.get("invisible") and not w.get("visible") else "hcaptcha"
    return "turnstile"


def detect(page) -> tuple[object, dict, str] | None:
    """(frame, widget, kind) for the most relevant captcha on the page, or None."""
    found: list[tuple[int, object, dict]] = []
    rank = {"recaptcha": 0, "turnstile": 1, "hcaptcha": 2}
    for fr in page.frames:
        url = fr.url or ""
        if any(h in url for h in ("recaptcha", "hcaptcha.com", "challenges.cloudflare.com", "googleapis.com")):
            continue
        try:
            widgets = fr.evaluate(_DETECT_JS)
        except Exception:
            continue
        for w in widgets:
            if w.get("sitekey"):
                if w.get("challenge") and not w.get("visible"):
                    continue
                # A visible challenge takes precedence over background, invisible widgets.
                priority = -1 if w.get("challenge") else (0 if w.get("visible") and not w.get("invisible") else 1)
                found.append((priority * 10 + rank[w["type"]], fr, w))
    if not found:
        return None
    found.sort(key=lambda t: t[0])
    _, fr, w = found[0]
    if w["type"] == "hcaptcha" and not w.get("invisible"):
        # secure-api.js mounts its widget frames outside the light DOM; only the frame list shows the mode.
        w["invisible"] = any("hcaptcha" in (f.url or "") and "checkbox-invisible" in (f.url or "") for f in page.frames)
    return fr, w, _classify(w)


def _task(kind: str, url: str, w: dict) -> dict | None:
    key = w["sitekey"]
    action = w.get("action") or "submit"
    if kind in ("recaptcha_v2", "recaptcha_v2_invisible"):
        return {"type": "ReCaptchaV2TaskProxyLess", "websiteURL": url, "websiteKey": key,
                "isInvisible": kind.endswith("invisible")}
    if kind == "recaptcha_v2_enterprise":
        return {"type": "ReCaptchaV2EnterpriseTaskProxyLess", "websiteURL": url, "websiteKey": key,
                "isInvisible": bool(w.get("invisible"))}
    if kind == "recaptcha_v3":
        return {"type": "ReCaptchaV3TaskProxyLess", "websiteURL": url, "websiteKey": key, "pageAction": action}
    if kind == "recaptcha_v3_enterprise":
        return {"type": "ReCaptchaV3EnterpriseTaskProxyLess", "websiteURL": url, "websiteKey": key, "pageAction": action}
    if kind == "turnstile":
        task = {"type": "AntiTurnstileTaskProxyLess", "websiteURL": url, "websiteKey": key}
        if w.get("action"):
            task["metadata"] = {"action": w["action"]}
        return task
    return None


def _solve(api_key: str, task: dict, timeout: float) -> tuple[str | None, str]:
    try:
        r = requests.post(f"{API}/createTask", json={"clientKey": api_key, "task": task}, timeout=30).json()
    except Exception as e:
        return None, f"createTask: {type(e).__name__}"
    if r.get("errorId"):
        return None, f"createTask: {r.get('errorCode')} {r.get('errorDescription', '')}"[:200]
    sol = r.get("solution")
    task_id = r.get("taskId")
    if not sol and not task_id:
        return None, "createTask: no taskId"
    deadline = time.time() + timeout
    while not sol and time.time() < deadline:
        time.sleep(3)
        try:
            r = requests.post(f"{API}/getTaskResult", json={"clientKey": api_key, "taskId": task_id}, timeout=30).json()
        except Exception as e:
            log.info("getTaskResult error: %s", e)
            continue
        if r.get("errorId"):
            return None, f"getTaskResult: {r.get('errorCode')} {r.get('errorDescription', '')}"[:200]
        if r.get("status") == "failed":
            return None, f"getTaskResult: failed {r.get('errorCode', '')}"[:200]
        if r.get("status") == "ready":
            sol = r.get("solution") or {}
    if not sol:
        return None, "timeout"
    token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("captchaResponse")
    return (token, "ok") if token else (None, "empty solution")


def solve_if_present(page, config: dict, job_id: str | None = None) -> CaptchaOutcome:
    try:
        hit = detect(page)
    except Exception as e:
        log.info("captcha detect failed: %s", e)
        hit = None
    if not hit:
        return CaptchaOutcome("none")
    frame, w, kind = hit
    if kind in ("hcaptcha", "hcaptcha_invisible"):
        return CaptchaOutcome("unsupported", kind, detail="CapSolver does not support hCaptcha; manual verification required")
    api_key = os.environ.get("CAPSOLVER_API_KEY", "")
    if not api_key:
        return CaptchaOutcome("unsupported", kind, detail="CAPSOLVER_API_KEY not set")
    ccfg = config.get("captcha", {})
    if kind.startswith("recaptcha_v3") and ccfg.get("solve_v3", True) is False:
        return CaptchaOutcome("none", kind, detail="score-based captcha left to the page")
    cost = COST.get(kind, 0.003)
    cap = float(ccfg.get("max_spend_per_night_usd", 3.0))
    task = _task(kind, frame.url, w)
    if task is None:
        return CaptchaOutcome("unsupported", kind)
    # Apply batches are process-serialized by runner.apply.lock. Serialize the spend check and
    # remote solve within each batch so parallel browser workers cannot overshoot the nightly cap.
    with _SPEND_LOCK:
        spent = db.captcha_spend_since(db.night_start())
        if spent + cost > cap:
            return CaptchaOutcome("budget", kind, detail=f"spent ${spent:.3f} of ${cap:.2f} tonight")
        log.info("solving %s via CapSolver for %s", kind, job_id or frame.url)
        token, detail = _solve(api_key, task, float(ccfg.get("timeout_seconds", 120)))
        if not token:
            db.add_captcha_spend(kind, 0.0, False, job_id)
            return CaptchaOutcome("failed", kind, detail=detail)
        db.add_captcha_spend(kind, cost, True, job_id)
    try:
        if kind == "turnstile":
            how = frame.evaluate(_INJECT_TURNSTILE_JS, [token, w.get("callback", "")])
        else:
            how = frame.evaluate(_INJECT_RECAPTCHA_JS, [token, kind.startswith("recaptcha_v3"), w.get("callback", "")])
    except Exception as e:
        return CaptchaOutcome("failed", kind, cost, f"inject: {type(e).__name__}")
    if how in ("callback-error", "no-target"):
        return CaptchaOutcome("failed", kind, cost, f"inject: {how}")
    return CaptchaOutcome("solved", kind, cost, f"token injected ({how})")
