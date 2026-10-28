"""Workday (*.myworkdayjobs.com) adapter: sign in or create a per-tenant account, verify email, then walk
the multi-step application wizard. Never clicks Submit unless submit_decision allows it."""
from __future__ import annotations

import logging
import json
from datetime import date
import re
import time
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

from .. import mail, vault
from ..browser import human_pause
from ..models import Answer, ApplyResult, ApplyStatus, Field, Job
from .base import ApplyContext, _missing, blocked_result, submit_decision

log = logging.getLogger(__name__)

BUDGET_SECONDS = 600
MAX_STEPS = 15
ROOT = '[data-automation-id="applyFlowPage"]'
NEXT = '[data-automation-id="bottom-navigation-next-button"], [data-automation-id="pageFooterNextButton"]'
_FINAL_SUBMIT_LABELS = {"submit", "submit application", "submit my application", "submit your application"}
HONEYPOT = "beecatcher"

_VERIFY_RE = re.compile(r"verify your (email|account)|verification (email|link)|activate your account|"
                        r"account (has not been|is not) (yet )?verified|confirm your email", re.I)
_EXISTS_RE = re.compile(r"already (exists|in use|been used|registered)|account with this email", re.I)
_BAD_CREDS_RE = re.compile(r"wrong email|incorrect|invalid (email address or )?password|"
                           r"email address or password", re.I)
_BAD_EMAIL_RE = re.compile(r"(valid|invalid).{0,30}email|email.{0,30}(valid|invalid|format|not allowed)", re.I)
_LOCKED_RE = re.compile(r"locked|too many (failed|attempts)", re.I)
_DONE_RE = re.compile(r"application (was |has been )?submitted|thank you for applying|"
                      r"congratulations|successfully submitted", re.I)
_CLOSED_RE = re.compile(r"no longer (accepting|available)|page you are looking for doesn.t exist|"
                        r"job (posting )?(is )?(closed|expired|not found)", re.I)
_APPLIED_RE = re.compile(r"already applied|you have applied|previously applied", re.I)
_OOPS_RE = re.compile(r"something went wrong|please refresh the page", re.I)
_STEP_PREFIX = re.compile(r"^\s*(current\s+)?step\s+\d+\s+of\s+\d+\s*", re.I)

# Hide Workday widgets (and the honeypot) from the generic extractor; this module fills them itself.
_SKIP_JS = """(root) => {
  const r = root ? document.querySelector(root) : document.body;
  if (!r) return 0;
  let n = 0;
  for (const s of ['[data-automation-id="beecatcher"]', '[data-automation-id="multiselectInputContainer"]',
                   'input[data-uxi-widget-type="selectinput"]', '[data-automation-id^="dateSection"]',
                   '[data-automation-id="formField-extension"]'])
    for (const e of r.querySelectorAll(s)) { e.setAttribute('data-aa-skip', 'workday'); n++; }
  return n;
}"""

# Repeating panels ("Work Experience 1", "Education 2") reuse labels like "From"; return the panel heading per key.
_PANEL_JS = """(keys) => {
  const out = {};
  const panelSel = [
    '[role=group][aria-labelledby$="-panel"]',
    '[data-automation-id^="education-"]',
    '[data-automation-id^="workExperience-"]',
    '[data-automation-id^="languages-"]',
    '[data-automation-id^="language-"]',
    '[role=group][aria-labelledby*="Education"]',
    '[role=group][aria-labelledby*="Work-Experience"]',
    '[role=group][aria-labelledby*="Language"]',
    '[role=group][aria-labelledby*="education" i]',
    '[role=group][aria-labelledby*="workExperience" i]',
    '[role=group][aria-labelledby*="language" i]'
  ].join(', ');

  const getHeading = (g) => {
    if (!g) return '';
    const lblId = g.getAttribute('aria-labelledby');
    if (lblId) {
      const lblEl = document.getElementById(lblId);
      if (lblEl) {
        const head = lblEl.matches('h1, h2, h3, h4, h5, h6, [role="heading"]') ? lblEl :
                     lblEl.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"], legend');
        const raw = head ? head.innerText : lblEl.innerText;
        const line = (raw || '').split(/[\r\n]+/).map(s => s.trim()).filter(Boolean)[0];
        if (line) return line.replace(/\\s+/g, ' ');
      }
    }
    const head = g.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"], legend, [data-automation-id*="heading" i], [data-automation-id*="title" i], [data-automation-id*="header" i]');
    if (head) {
      const line = (head.innerText || '').split(/[\r\n]+/).map(s => s.trim()).filter(Boolean)[0];
      if (line) return line.replace(/\\s+/g, ' ');
    }
    const aid = g.getAttribute('data-automation-id') || lblId || '';
    const m = aid.match(/(education|workExperience|language|languages)[-_]?(\\d+)?/i);
    if (m) {
      const kind = /edu/i.test(m[1]) ? 'Education' : /lang/i.test(m[1]) ? 'Languages' : 'Work Experience';
      return m[2] ? `${kind} ${m[2]}` : kind;
    }
    return '';
  };

  for (const k of keys) {
    const el = document.querySelector(`[data-aa-key="${k}"]`);
    const g = el && el.closest(panelSel);
    const h = g && getHeading(g);
    if (h) out[k] = h;
  }
  return out;
}"""

_EXPERIENCE_CONTEXT = (
    "Fields prefixed 'Work Experience N' describe the APPLICANT's own past jobs and must come from the RESUME "
    "(fill entry N with the N-th most recent resume job), never from the job being applied to. Education fields "
    "come from the PROFILE. Dates for these fields as MM/YYYY. If that resume job is current ('Present'), answer "
    "'I currently work here' with true and leave its end date unknown.")


def A(aid: str) -> str:
    return f'[data-automation-id="{aid}"]'


class _Stop(Exception):
    def __init__(self, result: ApplyResult):
        super().__init__(result.reason)
        self.result = result


# ---------- page inspection (pure functions over a Page; unit-tested on HTML fixtures) ----------

def _visible(scope, selector: str) -> bool:
    try:
        loc = scope.locator(selector)
        return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
    except Exception:
        return False


def _text(scope, selector: str) -> str:
    try:
        loc = scope.locator(selector)
        return " ".join(loc.nth(i).inner_text() for i in range(loc.count()) if loc.nth(i).is_visible()).strip()
    except Exception:
        return ""


def step_name(page: Page) -> str:
    raw = _text(page, A("progressBarActiveStep"))
    return " ".join(_STEP_PREFIX.sub("", raw).split())


def next_button_text(page: Page) -> str:
    try:
        buttons = page.locator(NEXT)
        for i in range(buttons.count()):
            button = buttons.nth(i)
            if button.is_visible():
                return " ".join((button.inner_text() or button.get_attribute("aria-label") or "").split())
    except Exception:
        pass
    return ""


def _is_final_submit(text: str) -> bool:
    return " ".join(text.split()).casefold() in _FINAL_SUBMIT_LABELS


def _is_submit_action(text: str) -> bool:
    return bool(re.match(r"submit\b", text, re.I))


def is_auth_step(name: str) -> bool:
    return bool(re.search(r"sign in|create account", name, re.I))


def is_review(page: Page) -> bool:
    name = step_name(page)
    return bool(re.fullmatch(r"review", name, re.I)) or _is_submit_action(next_button_text(page))


def detect_state(page: Page) -> str:
    """posting | start | auth_choice | auth_signin | auth_create | wizard | review | submitted | closed | unknown"""
    body = _text(page, "body")
    if _DONE_RE.search(body) and not _visible(page, NEXT):
        return "submitted"
    if _visible(page, A("applyManually")):
        return "start"
    if _visible(page, A("verifyPassword")):
        return "auth_create"
    if _visible(page, A("password")) and _visible(page, A("email")):
        return "auth_signin"
    if _visible(page, A("signInContent")) or _visible(page, A("SignInWithEmailButton")):
        return "auth_choice"
    if _visible(page, NEXT) or (_visible(page, A("progressBarActiveStep")) and not is_auth_step(step_name(page))):
        return "review" if is_review(page) else "wizard"
    if _visible(page, A("adventureButton")):
        return "posting"
    if _CLOSED_RE.search(body):
        return "closed"
    return "unknown"


def error_texts(page: Page) -> list[str]:
    out: list[str] = []
    for sel in (A("errorMessage"), A("inputAlert"), A("errorBanner") + " li", '[role="alert"]'):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 30)):
                el = loc.nth(i)
                if el.is_visible():
                    t = " ".join(el.inner_text().split())
                    if t and t not in out and not re.fullmatch(r"(\d+ )?errors? found", t, re.I) \
                            and not re.search(r"successfully", t, re.I):
                        out.append(t)
        except Exception:
            continue
    return out


def site_base_url(url: str) -> str:
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", s)]
    site = segs[0] if segs and segs[0] != "job" else ""
    return f"{p.scheme or 'https'}://{p.netloc}/{site}".rstrip("/")


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(value: str) -> tuple[int, int, int] | None:
    """Parse a complete supported date, validating the calendar; missing parts default to 1."""
    v = value.strip()
    parts = None
    if m := re.fullmatch(r"(\d{4})", v):
        parts = (int(m[1]), 1, 1)
    elif m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", v):
        parts = (int(m[1]), int(m[2]), int(m[3] or 1))
    elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v):
        parts = (int(m[3]), int(m[1]), int(m[2]))
    elif m := re.fullmatch(r"(\d{1,2})/(\d{4})", v):
        parts = (int(m[2]), int(m[1]), 1)
    elif m := re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", v):
        if mo := _MONTHS.get(m[1].lower()):
            parts = (int(m[2]), mo, 1)
    if parts:
        try:
            date(*parts)
            return parts
        except ValueError:
            pass
    return None


_DECLINE_RE = re.compile(
    r"\b(decline|prefer not|choose not|not to (?:say|disclose|state|answer|identify)|"
    r"not declared|undeclared|undisclosed|not disclosed|(?:do\s*not|don'?t)\s*(?:wish|want)|"
    r"not\s*disclose)\b",
    re.I
)


def _match_option(options: list[str], value: str) -> int | None:
    def normalize(text: str) -> str:
        return " ".join(text.casefold().split())

    v = normalize(value)
    if not v:
        return None
    if _DECLINE_RE.search(v):
        for i, option in enumerate(options):
            if _DECLINE_RE.search(option):
                return i
    from ..form import option_matches
    for i, option in enumerate(options):
        if option_matches(value, option):
            return i
    # These are equivalent names for the same country, not general substring matches.
    country_names = {"united states", "united states of america", "usa", "u.s.a."}
    phone_country_names = {"united states (+1)", "united states of america (+1)", "united states +1", "+1"}
    for i, option in enumerate(options):
        o = normalize(option)
        if o == v or (v in country_names and o in country_names) or (
            v in phone_country_names and o in phone_country_names
        ):
            return i
    if re.fullmatch(r"\d{4}", v):
        for i, option in enumerate(options):
            if normalize(option).startswith(v):
                return i
    for i, option in enumerate(options):
        cleaned_opt = normalize(re.sub(r"\([^)]*\)", "", option).strip())
        if cleaned_opt and (cleaned_opt == v or cleaned_opt.rstrip("s") == v.rstrip("s")
                            or cleaned_opt.endswith(f" - {v}") or cleaned_opt.endswith(f"- {v}")):
            return i
    from ..answers import _degree_level
    v_deg = _degree_level(v)
    if v_deg:
        for i, option in enumerate(options):
            if re.search(r"years? of education", option, re.I):
                clean = re.sub(r"\(.*?\)", "", option).strip()
                if _degree_level(clean) == v_deg:
                    return i
    from .. import llm
    return llm.pick_option(options, value)


def _is_reasonable(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    low = t.lower()
    return low not in ("select one", "select", "-- select --", "choose one", "please select", "none")


_PERSONAL_MOBILE_TYPES = (
    "Mobile", "Mobile - Personal", "Pers Mobile", "Home Mobile", "Personal Mobile",
    "Cell", "Cellular", "Cellular Phone", "Cell Phone", "Mobile Phone", "Home Cellular",
)


def _is_phone_device(f: Field) -> bool:
    return bool(re.search(r"\b(?:phone\s*(?:device\s*)?type|device\s*type)\b", f.label or "", re.I) or
                re.search(r"(?:phoneDeviceType|phoneType|deviceType)(?:$|[-_])", f.name or "", re.I))


def _is_referral_source(f: Field) -> bool:
    return bool(re.search(r"\b(?:how|where)\b.{0,60}\b(?:hear|heard)\b.{0,50}\b(?:about|of)\b|"
                          r"\b(?:how|where)\b.{0,60}\b(?:learn|find|found)\b.{0,50}"
                          r"\b(?:position|role|job|opening|opportunity|vacancy|us)\b|"
                          r"\b(?:recruitment|recruiting|referral|application) source\b|^source[?*\s]*$",
                          f.label or "", re.I) or
                re.search(r"(?:^|[-_])(?:source|sourceCategory|referralSource|howDidYouHear)(?:$|[-_])",
                          f.name or "", re.I))


def _referral_aliases(value: str) -> tuple[str, ...]:
    # These describe the saved source without claiming a different named board,
    # an employee referral, a school referral, or attendance at an event.
    if value.strip().casefold() in ("simplify", "job board"):
        return ("Job Board", "Job Boards", "Job Board - Other", "Other Job Board", "Job Sites",
                "Online Job Board", "Job Search Website",
                "Career Websites", "Internet", "LinkedIn", "Indeed", "Glassdoor", "Other")
    return ()


def _exact_option(options: list[str], value: str) -> int | None:
    wanted = " ".join(str(value).casefold().split())
    return next((i for i, option in enumerate(options)
                 if " ".join(option.casefold().split()) == wanted), None) if wanted else None


def _yes_no(value) -> str | None:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return {"yes": "Yes", "no": "No", "true": "Yes", "false": "No"}.get(str(value).strip().casefold())


def _pick_option_index(
    texts: list[str],
    value: str,
    *,
    is_phone_device: bool = False,
    is_referral: bool = False,
    is_language: bool = False,
    is_required: bool = False,
) -> int | None:
    if not texts:
        return None

    reasonable_indices = [j for j, t in enumerate(texts) if _is_reasonable(t)]
    if not reasonable_indices:
        return None

    if is_required and len(reasonable_indices) == 1:
        return reasonable_indices[0]

    if is_phone_device:
        if _exact_option(list(_PERSONAL_MOBILE_TYPES), value) is not None:
            return next((i for alias in _PERSONAL_MOBILE_TYPES
                         if (i := _exact_option(texts, alias)) is not None), None)
        return _exact_option(texts, value)

    if is_referral:
        return next((i for source in (value, *_referral_aliases(value))
                     if _is_reasonable(source) and (i := _exact_option(texts, source)) is not None), None)

    if _DECLINE_RE.search(value):
        decline_idx = next((j for j, t in enumerate(texts) if _is_reasonable(t) and _DECLINE_RE.search(t)), None)
        if decline_idx is not None:
            return decline_idx
        if set(t.lower() for t in texts if _is_reasonable(t)) <= {"yes", "no"}:
            return _match_option(texts, "No")

    if set(t.lower() for t in texts if _is_reasonable(t)) <= {"yes", "no"}:
        aff = _yes_no(value)
        if aff:
            idx = _match_option(texts, aff)
            if idx is not None:
                return idx

    if value and _is_reasonable(value):
        idx = _match_option(texts, value)
        if idx is not None:
            return idx

    if is_language and value.casefold() in {"fluent", "native", "fluent / native", "native / fluent"}:
        for alias in ("Fluent", "Native", "Fluent / Native", "Native / Fluent", "Proficient", "Advanced"):
            idx = _match_option(texts, alias)
            if idx is not None:
                return idx

    from .. import llm
    return llm.pick_option(texts, value)


# ---------- adapter ----------

class WorkdayAdapter:
    name = "workday"

    def can_handle(self, job: Job) -> bool:
        host = urlparse(job.url).netloc.lower()
        return job.ats == "workday" or "myworkdayjobs.com" in host or "myworkdaysite.com" in host

    def apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        run = _Run(job, ctx)
        try:
            return run.go()
        except _Stop as s:
            return s.result
        except Exception as e:
            log.exception("workday adapter crashed on %s", job.url)
            return run.result(ApplyStatus.FAILED, f"workday error: {type(e).__name__}: {str(e)[:200]}", "error")


ADAPTER = WorkdayAdapter()


class _Run:
    def __init__(self, job: Job, ctx: ApplyContext):
        self.job, self.ctx, self.page = job, ctx, ctx.page
        self.deadline = time.monotonic() + BUDGET_SECONDS
        self.tenant = vault.tenant_key_for(job.url)
        self.fields: dict[str, Field] = {}
        self.answers: dict[str, Answer] = {}
        self.expected_answers: dict[str, Answer] = {}
        self.job_text = ""
        self.furthest = "Job posting"
        self.mail_after: float | None = None
        # keys of Workday widgets (button listboxes, multiselect prompts, date sections) filled by this module
        self.wd_keys: set[str] = set()

    # ----- helpers -----

    def result(self, status: ApplyStatus, reason: str, tag: str = "", **kw) -> ApplyResult:
        reason = f"{reason} [step: {self.furthest}]"
        shot = self.ctx.screenshot(self.job, tag or status.value)
        fields, answers = list(self.fields.values()), list(self.answers.values())
        by_key = {a.key: a for a in answers}
        unanswered = [f.label or f.key for f in fields if f.required and _missing(by_key.get(f.key))]
        kw.setdefault("unanswered_required", unanswered)
        return ApplyResult(status=status, reason=reason, screenshot=shot, final_url=self._url(),
                           fields_total=len(fields), answers=answers,
                           generated_answers=[f.label or f.key for f in fields
                                              if (a := by_key.get(f.key)) and a.source == "generated"], **kw)

    def stop(self, status: ApplyStatus, reason: str, tag: str = "") -> _Stop:
        self.ctx.notes.append(reason)
        return _Stop(self.result(status, reason, tag))

    def _url(self) -> str | None:
        try:
            return self.page.url
        except Exception:
            return None

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def check_budget(self) -> None:
        if self.left() <= 0:
            raise self.stop(ApplyStatus.NEEDS_REVIEW, f"time budget ({BUDGET_SECONDS}s) exceeded", "timeout")

    def settle(self, ms: int = 8000) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(ms, max(1000, int(self.left() * 1000))))
        except Exception:
            pass
        for sel in (A("loadingSpinner"), '[data-automation-id="wd-LoadingPanel"]'):
            try:
                self.page.locator(sel).first.wait_for(state="hidden", timeout=5000)
            except Exception:
                pass

    def wait_form(self, timeout: float = 15) -> None:
        """Steps render their controls a few seconds after the progress bar; wait until the count settles."""
        sel = ", ".join(f"{ROOT} {s}" for s in ("input", "textarea", 'button[aria-haspopup="listbox"]'))
        end = time.monotonic() + min(timeout, max(1.0, self.left()))
        last, stable_since = -1, time.monotonic()
        while time.monotonic() < end:
            try:
                n = self.page.locator(sel).count()
            except Exception:
                n = 0
            if n != last:
                last, stable_since = n, time.monotonic()
            elif time.monotonic() - stable_since >= (1.5 if n > 0 else 4):
                return
            self.page.wait_for_timeout(300)

    def wait_state(self, wanted: set[str], timeout: float = 30) -> str:
        end = time.monotonic() + min(timeout, max(1.0, self.left()))
        state = "unknown"
        while time.monotonic() < end:
            state = detect_state(self.page)
            if state in wanted:
                return state
            self.page.wait_for_timeout(400)
        return state

    def click(self, selector: str, *, overlay_label: str | None = None, timeout: float = 10000) -> bool:
        """Workday submit buttons sit under a transparent click_filter div; click that when present."""
        if overlay_label:
            ov = self.page.locator(f'{A("click_filter")}[aria-label="{overlay_label}"]')
            if _visible(self.page, f'{A("click_filter")}[aria-label="{overlay_label}"]'):
                try:
                    ov.first.click(timeout=timeout)
                    return True
                except Exception:
                    pass
        try:
            loc = self.page.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=timeout)
            loc.click(timeout=timeout)
            return True
        except Exception:
            try:
                self.page.locator(selector).first.click(timeout=timeout, force=True)
                return True
            except Exception:
                return False

    # ----- flow -----

    def go(self) -> ApplyResult:
        self.open_posting()
        self.start_application()
        self.authenticate()
        return self.wizard()

    def open_posting(self) -> None:
        self.page.goto(self.job.url, wait_until="domcontentloaded")
        state = self.wait_state({"posting", "wizard", "closed", "start"}, timeout=40)
        if state == "closed":
            raise self.stop(ApplyStatus.SKIPPED, "posting closed or not found", "closed")
        if state not in ("posting", "wizard", "start"):
            raise self.stop(ApplyStatus.FAILED, "Workday Apply button not found", "no_apply")
        try:
            from .. import form
            self.job_text = form.page_text(self.page)
        except Exception:
            self.job_text = _text(self.page, A("jobPostingDescription"))[:8000]

    def start_application(self) -> None:
        if detect_state(self.page) == "posting":
            pages_before = len(self.ctx.browser.pages)
            human_pause()
            self.click(A("adventureButton"))
            self.page.wait_for_timeout(1500)
            if len(self.ctx.browser.pages) > pages_before:
                self.page = self.ctx.browser.pages[-1]
                self.ctx.page = self.page
        state = self.wait_state({"start", "auth_choice", "auth_signin", "auth_create", "wizard", "review"}, 25)
        if state == "start":
            human_pause()
            self.click(A("applyManually"))
            state = self.wait_state({"auth_choice", "auth_signin", "auth_create", "wizard", "review"}, 30)
        if state not in ("auth_choice", "auth_signin", "auth_create", "wizard", "review"):
            if _APPLIED_RE.search(_text(self.page, "body")):
                raise self.stop(ApplyStatus.SKIPPED, "already applied", "applied")
            raise self.stop(ApplyStatus.FAILED, f"could not open application (state={state})", "no_start")
        self.furthest = "Create Account/Sign In"

    def authenticate(self) -> None:
        state = detect_state(self.page)
        if state in ("wizard", "review"):
            return
        acct = vault.get(self.tenant)
        if acct:
            outcome = self.sign_in(acct)
            if outcome == "bad_creds" and not acct.verified:
                self._check_account_creation_allowed()
                outcome = self.submit_create(acct.email, acct.password)
                if outcome == "exists":
                    raise self.stop(ApplyStatus.NEEDS_REVIEW, "account exists, sign-in failed with vault password",
                                    "signin_failed")
        else:
            outcome = self.create_account()
        self.handle_auth_outcome(outcome, vault.get(self.tenant))

    def handle_auth_outcome(self, outcome: str, acct: vault.Account | None) -> None:
        if outcome == "signed_in":
            if acct and not acct.verified:
                vault.mark_verified(self.tenant)
            return
        if outcome == "verify" and acct:
            self.verify_email(acct)
            return
        if outcome == "exists":
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "account exists, password unknown", "account_exists")
        if outcome == "bad_creds":
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "sign-in failed: wrong email or password", "signin_failed")
        if outcome == "locked":
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "account locked", "locked")
        raise self.stop(ApplyStatus.NEEDS_REVIEW, f"authentication failed: {outcome}", "auth_failed")

    def show_form(self, kind: str) -> None:
        """kind: 'signin' or 'create'."""
        want = "auth_signin" if kind == "signin" else "auth_create"
        for _ in range(3):
            state = detect_state(self.page)
            if state == want:
                return
            if state == "auth_choice" and _visible(self.page, A("SignInWithEmailButton")):
                self.click(A("SignInWithEmailButton"))
            elif kind == "create" and _visible(self.page, A("createAccountLink")):
                self.click(A("createAccountLink"))
            elif kind == "signin" and _visible(self.page, A("signInLink")):
                self.click(A("signInLink"))
            elif kind == "signin" and _visible(self.page, A("utilityButtonSignIn")):
                self.click(A("utilityButtonSignIn"))
            self.wait_state({want}, timeout=8)
        if detect_state(self.page) != want:
            raise self.stop(ApplyStatus.FAILED, f"could not open the {kind} form", "auth_form")

    def type_into(self, selector: str, value: str) -> None:
        loc = self.page.locator(selector).first
        loc.click()
        loc.fill("")
        loc.type(value, delay=25)

    def auth_outcome(self, timeout: float = 25) -> str:
        """signed_in | verify | exists | bad_email | bad_creds | locked | error: <text>"""
        end = time.monotonic() + timeout
        self.page.wait_for_timeout(1500)
        while time.monotonic() < end:
            body = _text(self.page, "body")
            errs = " | ".join(error_texts(self.page))
            if _VERIFY_RE.search(errs) or (_VERIFY_RE.search(body) and detect_state(self.page) != "wizard"):
                return "verify"
            if _EXISTS_RE.search(errs):
                return "exists"
            if _LOCKED_RE.search(errs):
                return "locked"
            if _BAD_CREDS_RE.search(errs):
                return "bad_creds"
            if _BAD_EMAIL_RE.search(errs):
                return "bad_email"
            state = detect_state(self.page)
            if state in ("wizard", "review"):
                return "signed_in"
            if errs and state in ("auth_signin", "auth_create"):
                self.page.wait_for_timeout(1500)
                return "error: " + " | ".join(error_texts(self.page))[:300]
            self.page.wait_for_timeout(500)
        return "error: no response after submitting credentials"

    def sign_in(self, acct: vault.Account) -> str:
        self.show_form("signin")
        self.type_into(A("email"), acct.email)
        self.type_into(A("password"), acct.password)
        human_pause()
        self.click(A("signInSubmitButton"), overlay_label="Sign In")
        return self.auth_outcome()

    def submit_create(self, email: str, password: str) -> str:
        self._check_account_creation_allowed()
        self.show_form("create")
        self.type_into(A("email"), email)
        self.type_into(A("password"), password)
        self.type_into(A("verifyPassword"), password)
        box = self.page.locator(A("createAccountCheckbox"))
        if box.count() and not box.first.is_checked():
            try:
                box.first.check(timeout=5000)
            except Exception:
                box.first.click(force=True)
        human_pause()
        self.click(A("createAccountSubmitButton"), overlay_label="Create Account")
        return self.auth_outcome()

    def _check_account_creation_allowed(self) -> None:
        accounts = self.ctx.config.get("accounts", {})
        if self.ctx.dry_run and not accounts.get("create_in_dry_run", False):
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "account required (not created in dry run)", "create_account")
        if self.ctx.submit_mode == "fill_only" and not accounts.get("create_in_fill_only", False):
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "account required (not created in fill_only mode)",
                            "create_account")

    def create_account(self) -> str:
        self._check_account_creation_allowed()
        self.show_form("create")
        acct = vault.create(self.tenant, self.job.company, site_base_url(self.job.url))
        self.mail_after = time.time() - 5
        log.info("creating Workday account for %s", self.tenant)
        outcome = self.submit_create(acct.email, acct.password)
        if outcome == "bad_email" and "+" in acct.email:
            plain = vault.base_email()
            vault.update_email(self.tenant, plain)
            outcome = self.submit_create(plain, acct.password)
        if outcome in ("exists", "bad_email") or (outcome.startswith("error:") and "password" in outcome.lower()):
            vault.delete(self.tenant)
        return outcome

    def verify_email(self, acct: vault.Account) -> None:
        after = self.mail_after or time.time() - 7 * 86400
        if not mail.available():
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "email verification required", "verify_email")
        for sel in (A("resendVerificationLink"), 'button:has-text("Resend")'):
            if after < time.time() - 3600 and _visible(self.page, sel):
                after = time.time() - 5
                self.click(sel)
                break
        to_alias = acct.email if "+" in acct.email else None
        v = mail.wait_for_verification(after, sender_hint="myworkday", to_alias=to_alias, want="link",
                                       timeout=min(self.left() - 60, 180))
        if not v or not v.link:
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "email verification required (no email received)",
                            "verify_email")
        self.page.goto(v.link, wait_until="domcontentloaded")
        self.settle()
        vault.mark_verified(self.tenant)
        self.page.goto(self.job.url, wait_until="domcontentloaded")
        self.wait_state({"posting", "wizard"}, 30)
        self.start_application()
        if detect_state(self.page) in ("wizard", "review"):
            return
        outcome = self.sign_in(vault.get(self.tenant) or acct)
        if outcome != "signed_in":
            self.handle_auth_outcome(outcome if outcome != "verify" else "error: still unverified", None)

    # ----- wizard -----

    def wizard(self) -> ApplyResult:
        if _APPLIED_RE.search(_text(self.page, "body")) and not _visible(self.page, NEXT):
            raise self.stop(ApplyStatus.SKIPPED, "already applied", "applied")
        last, same, reloads = "", 0, 0
        for _ in range(MAX_STEPS):
            self.check_budget()
            self.settle()
            while _OOPS_RE.search(_text(self.page, ROOT)) and reloads < 2:
                reloads += 1
                log.info("Workday error page; reloading (%d)", reloads)
                self.page.wait_for_timeout(2000)
                try:
                    self.page.reload(wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    log.warning("Workday reload failed: %s", e)
                self.settle()
            if _OOPS_RE.search(_text(self.page, ROOT)):
                raise self.stop(ApplyStatus.NEEDS_REVIEW, "Workday 'Something went wrong' page", "oops")
            state = self.wait_state({"wizard", "review", "submitted"}, 30)
            if state == "submitted":
                if _APPLIED_RE.search(_text(self.page, "body")):
                    raise self.stop(ApplyStatus.SKIPPED, "already applied", "applied")
                raise self.stop(ApplyStatus.NEEDS_REVIEW,
                                "submission confirmation found without a final submit in this run", "submitted")
            if state not in ("wizard", "review"):
                raise self.stop(ApplyStatus.NEEDS_REVIEW, f"lost the application wizard (state={state})", "lost")
            self.wait_form()
            name = step_name(self.page) or "unknown step"
            self.furthest = name
            if state == "review":
                return self.review()
            same = same + 1 if name == last else 0
            if same >= 3:
                raise self.stop(ApplyStatus.NEEDS_REVIEW, f"stuck on step '{name}'", "stuck")
            last = name
            if re.search(r"assessment", name, re.I):
                raise self.stop(ApplyStatus.NEEDS_REVIEW, "Workday assessment step needs a human", "assessment")
            self.fill_step(name)
            self.advance(name)
        raise self.stop(ApplyStatus.NEEDS_REVIEW, f"more than {MAX_STEPS} wizard steps", "too_many_steps")

    def _is_school_field(self, f: Field) -> bool:
        if f.kind in ("checkbox", "radio"):
            return False
        if f.options and set(o.lower() for o in f.options) <= {"yes", "no", "select one"}:
            return False
        lab = f.label or ""
        fid = f.name or ""
        return bool(
            re.search(r"\b(school|university|institution|college)\b", lab, re.I)
            or re.search(r"school|university|institution|college", fid, re.I)
        )

    def _is_date_field(self, f: Field) -> bool:
        if f.kind == "date":
            return True
        fid = f.name or ""
        lab = f.label or ""
        words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", fid)
        words = re.sub(r"[-_]", " ", words)
        if re.search(r"\b(?:date|attended|from|to)\b", words, re.I):
            return True
        # "to" and "from" appear in ordinary application questions. Only a
        # standalone From/To field (possibly prefixed by its panel) is a date.
        short_label = lab.rsplit(" - ", 1)[-1].strip()
        if re.match(r"^(?:From|To)(?:\s*\(|\s*\*|$)", short_label, re.I):
            return True
        if re.search(r"\b(attended|start\s*date|end\s*date|graduation\s*date|first\s*year|year\s*of)\b", lab, re.I):
            return True
        return False

    def _is_education_date_field(self, f: Field) -> bool:
        lab = f.label or ""
        fid = f.name or ""
        is_edu = bool(re.search(r"education|school|degree|college|university", lab, re.I) or
                      re.search(r"education|school|degree|college|university", fid, re.I))
        is_date_lab = bool(re.search(
            r"\b(from|to\b|to\s*\(|attended|start\s*date|end\s*date|graduation\s*date|first\s*year|year\s*of)\b",
            lab, re.I
        ))
        is_date_fid = bool(re.search(r"startDate|endDate|from|to|attended|date|year", fid, re.I))
        return (is_edu and (is_date_lab or is_date_fid)) or bool(re.search(r"\b(from|to|attended|first\s*year|year\s*of\s*graduation)\b", lab, re.I))

    def _education_year_answer(self, f: Field) -> Answer | None:
        if not self._is_education_date_field(f):
            return None
        label = f.label or ""
        # Work experience panels can also contain From/To. Require an education panel.
        in_education = bool(re.search(r"\beducation\s*\d*\s*-", label, re.I))
        if not in_education:
            try:
                in_education = bool(self.page.locator(f'[data-aa-key="{f.key}"]').first.evaluate("""el => !!el.closest(
                    '[role="group"][aria-labelledby*="Education" i], [data-automation-id^="education-" i]')"""))
            except Exception:
                pass
        if not in_education:
            return None
        short = label.split(" - ")[-1]
        edu = self.ctx.profile.get("education") or {}
        if re.search(r"\b(from|start)\b", short, re.I):
            year = edu.get("start_year")
        elif re.search(r"\b(to|end|graduat)\b", short, re.I):
            year = edu.get("graduation_year")
        else:
            return None
        if not re.fullmatch(r"\d{4}", str(year or "").strip()):
            return None
        try:
            year_only = self.page.locator(f'[data-aa-key="{f.key}"]').first.evaluate("""el => {
                const box = el.closest('[data-automation-id="dateInputWrapper"]') || el.parentElement;
                return /^(YYYY|year)$/i.test(el.placeholder || '') ||
                    (!!box.querySelector('[data-automation-id="dateSectionYear-input"]') &&
                     !box.querySelector('[data-automation-id="dateSectionMonth-input"]'));
            }""")
        except Exception:
            year_only = False
        if year_only:
            return Answer(f.key, str(year).strip(), "profile", "education year from profile")
        start_val = edu.get("start_date") or f"{edu.get('start_month', 'August')} {year}".strip()
        end_val = edu.get("graduation_date") or f"{edu.get('graduation_month', 'May')} {year}".strip()
        value = start_val if re.search(r"\b(from|start)\b", short, re.I) else end_val
        return Answer(f.key, value or str(year), "profile", "education date from profile")

    def fill_step(self, name: str, *, repair: list[str] | None = None, extra_context: str = "") -> list[Field]:
        """Extract, answer and fill the current step. With `repair` (the inline errors), every field is
        re-answered for context but only errored or empty required fields are re-filled."""
        from .. import answers as answers_mod
        from .. import form
        preset: dict[str, Answer] = {}
        if re.search(r"experience", name, re.I) and repair is None:
            self.upload_resume()
        root = ROOT if _visible(self.page, ROOT) else None
        try:
            self.page.evaluate(_SKIP_JS, root)
        except Exception:
            pass
        fields = [f for f in form.extract_fields(self.page, root) if not self.is_honeypot(f)]
        fields += self.extract_workday_widgets(root, {f.key for f in fields})
        radios = self.extract_workday_radios(root)
        if radios:
            aids = {f.name for f in radios}
            fields = [f for f in fields if not (
                self.page.locator(f'[data-aa-key="{f.key}"]').count() and
                self.page.locator(f'[data-aa-key="{f.key}"]').first.evaluate(
                    "el => el.closest('[data-automation-id^=\"formField-\"]')?.getAttribute('data-automation-id')"
                ) in aids
            )]
            fields.extend(radios)
        for f in fields:
            if f.kind == "radio":
                label = self._radio_question(f)
                if label:
                    f.label = label
        try:
            panels = self.page.evaluate(_PANEL_JS, [f.key for f in fields])
        except Exception:
            panels = {}
        for f in fields:
            if f.key in panels and not f.label.startswith(panels[f.key]):
                f.label = f"{panels[f.key]} - {f.label}"
        # Route date fields in Education panels and school fields to Workday widget/date filling
        for f in fields:
            if f.kind in ("select", "combobox") and f.current_value and not _is_reasonable(f.current_value):
                f.current_value = ""
            if f.kind in ("select", "combobox", "checkbox", "radio") or self._is_education_date_field(f) or self._is_date_field(f) or self._is_school_field(f):
                self.wd_keys.add(f.key)
        if re.search(r"experience", name, re.I):
            hint = _text(self.page, A("instructionalText"))
            extra_context = " ".join(x for x in (_EXPERIENCE_CONTEXT, f"Page instructions: {hint}" if hint else "",
                                                 extra_context) if x)
        if re.search(r"self.?identif", name, re.I):
            preset.update(self.fill_self_identify())
        for f in fields:
            if f.kind == "file":
                preset.setdefault(f.key, Answer(f.key, self.ctx.profile.get("resume_path"), "profile", "uploaded"))
        # Resolve protected facts on repair passes too; a saved UI value is not evidence.
        grounded = {f.key: a for f in fields if (a := self._profile_widget_answer(f)) is not None}
        if repair is not None:
            bad = self.errored_keys(fields, repair)
            for f in fields:
                if f.key not in grounded and f.key not in bad and not (f.required and not f.current_value):
                    preset.setdefault(f.key, self.answers.get(f.key) or
                                      Answer(f.key, f.current_value or None, "unknown", "left as is"))
        todo = [f for f in fields if f.key not in preset]
        # Preserve explicit facts even if the initial (possibly lazy) option list
        # cannot yet represent them. The widget filler verifies the final choice.
        ans: list[Answer] = []
        ask = [f for f in (fields if repair is not None else todo) if f.key not in grounded]
        if ask:
            ans = [a for a in answers_mod.answer_fields(ask, self.job, self.ctx.profile, job_text=self.job_text,
                                                        extra_context=extra_context, option_matcher=_match_option)
                   if a.key in {f.key for f in todo}]
        ans += [grounded[f.key] for f in todo if f.key in grounded]
        for f in todo:
            year_answer = self._education_year_answer(f)
            if year_answer:
                ans = [a for a in ans if a.key != f.key]
                ans.append(year_answer)
            lab = f.label or ""
            is_eeo = bool(re.search(r"\b(?:race|ethnic|gender|veteran|disabilit|self.?identif)\b", lab, re.I))
            is_proficiency = not is_eeo and bool(
                re.search(r"\b(?:overall|proficiency)\b", lab, re.I)
                or (f.options and any(re.search(r"\b(fluent|proficient|basic|intermediate)\b", o, re.I) for o in f.options))
            )
            is_lang = not is_eeo and bool(
                re.search(r"\blanguages?\b", lab, re.I)
                or (f.name and "language" in f.name.lower())
            )
            english = "english" in str((self.ctx.profile.get("personal") or {}).get("languages") or "").casefold()
            if is_proficiency and english:
                ans = [a for a in ans if a.key != f.key]
                ans.append(Answer(f.key, "Fluent", "profile", "fluent English from profile"))
            elif is_lang and english:
                if f.kind == "checkbox" and re.search(r"fluent", lab, re.I):
                    ans = [a for a in ans if a.key != f.key]
                    ans.append(Answer(f.key, True, "profile", "fluent in English from profile"))
                elif f.kind in ("select", "combobox"):
                    ans = [a for a in ans if a.key != f.key]
                    ans.append(Answer(f.key, "English", "profile", "English language from profile"))
        ans += [a for k, a in preset.items() if k in {f.key for f in fields}]
        for f in fields:
            self.fields[f.key] = f
        for a in ans:
            self.answers[a.key] = a
            self.expected_answers[a.key] = a
        fill_fields = [f for f in todo if f.key not in self.wd_keys]
        failed = form.fill_fields(self.page, fill_fields, ans) if fill_fields else []
        by_key = {a.key: a for a in ans}
        for k in list(failed) + [f.key for f in todo if f.key in self.wd_keys]:
            f, a = self.fields.get(k), by_key.get(k)
            if not f:
                continue
            if a and not _missing(a):
                if not self.fill_workday_widget(f, a):
                    self.answers[f.key] = Answer(f.key, None, "unknown", "Workday field could not be verified")
        if re.search(r"experience", name, re.I):
            self.drop_bad_work_experience()
        self.page.wait_for_timeout(500)
        # Sync answers from Workday widgets back to any overlapping form-extracted fields.
        # e.g., 'How Did You Hear About Us?' is extracted both as aa-N (combobox, options=[])
        # by form.extract_fields AND as wd-XXXX by extract_workday_widgets. The wd key gets
        # filled and stored, but the aa-N key stays None. Copy the answer across.
        for f in fields:
            if f.key.startswith("aa-") and _missing(self.answers.get(f.key)):
                # Check if there's a Workday widget key for the same DOM element
                try:
                    el = self.page.locator(f'[data-aa-key="{f.key}"]').first
                    if not el.count():
                        continue
                    # Read the current displayed value from the DOM
                    current_val = el.evaluate("""el => {
                        const box = el.closest('[data-automation-id^="formField-"]') || el.parentElement;
                        const selected = box && box.querySelector('[data-automation-id="selectedItem"], [class*="selected" i]');
                        if (selected) return selected.innerText.trim();
                        if (el.tagName === 'INPUT') {
                            if (el.type === 'checkbox' || el.type === 'radio') return el.checked ? 'true' : '';
                            return el.value.trim();
                        }
                        const value = el.getAttribute('aria-activedescendant') ? '' : (el.innerText || '').trim();
                        return value || '';
                    }""")
                    if current_val and not _missing(self.answers.get(f.key)):
                        pass  # already set
                    elif current_val:
                        val = True if current_val == "true" and f.kind == "checkbox" else current_val
                        self.answers[f.key] = Answer(f.key, val, "profile", f"synced from DOM: {current_val}")
                except Exception:
                    pass
        for f in list(fields):
            # conditional fields vanish (e.g. "To" once "I currently work here" is checked) or deleted panels
            try:
                el = self.page.locator(f'[data-aa-key="{f.key}"]').first
                hidden_control = el.count() and el.evaluate("""el =>
                    el.matches('input[type="radio"], input[type="checkbox"]') &&
                    !!el.closest('[data-automation-id^="formField-"]')?.getClientRects().length""")
                if not el.count() or (not hidden_control and not _visible(self.page, f'[data-aa-key="{f.key}"]')):
                    if _missing(self.answers.get(f.key)) or not el.count():
                        self.fields.pop(f.key, None)
                        self.answers.pop(f.key, None)
            except Exception:
                pass
        return fields

    def _radio_question(self, f: Field) -> str:
        try:
            return self.page.locator(f'[data-aa-key="{f.key}"]').first.evaluate("""el => {
                const box = el.closest('[data-automation-id^="formField-"]');
                if (!box) return '';
                const clean = s => (s || '').replace(/\\s+/g, ' ').trim().replace(/\\s*[✱*]\\s*$/, '');
                for (const n of box.querySelectorAll('[data-automation-id="formLabel"], legend, [id$="-label"], label')) {
                    if (n.querySelector('input, [role="radio"]')) continue;
                    const text = clean(n.innerText);
                    if (text && !/^(yes|no|select one)$/i.test(text)) return text;
                }
                return '';
            }""")
        except Exception:
            return ""

    def _employer_words(self) -> list[str]:
        # Include short names/acronyms such as HP, KLA and GE; ignore legal suffixes.
        return [w for w in re.findall(r"[A-Za-z0-9]+", self.job.company)
                if len(w) > 1 and w.casefold() not in {
                    "the", "and", "inc", "llc", "ltd", "corp", "corporation", "company", "co",
                    "group", "holdings", "financial", "health",
                }]

    def _profile_widget_answer(self, f: Field) -> Answer | None:
        preferences = self.ctx.profile.get("preferences") or {}
        personal = self.ctx.profile.get("personal") or {}
        lab = f.label or ""
        if f.kind in ("checkbox", "radio", "select", "combobox") and re.search(
                r"\b(?:consent|agree|accept|certify|attest|acknowledge|terms|privacy|arbitration)\b", lab, re.I):
            normalize = lambda text: re.sub(r"[\s*✱]+$", "", " ".join(str(text).casefold().split()))
            for company, entries in (self.ctx.profile.get("company_answers") or {}).items():
                if normalize(company) != normalize(self.job.company) or not isinstance(entries, dict):
                    continue
                for question, value in entries.items():
                    if normalize(question) == normalize(lab):
                        return Answer(f.key, value, "bank", "explicit consent answer in profile")
            if f.kind == "checkbox" and not f.options and not re.search(r"\b(?:text messages?|sms|marketing|promotional)\b", lab, re.I):
                return Answer(f.key, True, "profile", "consent")
            if f.kind in ("radio", "select", "combobox") and not re.search(r"\b(?:text messages?|sms|marketing|promotional)\b", lab, re.I):
                return Answer(f.key, "Yes", "profile", "consent")
            return Answer(f.key, None, "unknown", "consent needs an explicit answer for this question")
        if f.kind in ("checkbox", "radio", "select", "combobox"):
            for pattern, key in (
                (r"hispanic|latin[oax]", "hispanic_latino"),
                (r"gender|\bsex\b", "gender"),
                (r"\brace\b|ethnic", "race_ethnicity"),
                (r"veteran", "veteran_status"),
                (r"disabilit|impairment", "disability_status"),
            ):
                if re.search(pattern, lab + ' ' + (f.name or ''), re.I):
                    value = (self.ctx.profile.get("eeo") or {}).get(key)
                    if key == "hispanic_latino" and str(value).lower().startswith("decline"):
                        if f.options and set(o.lower() for o in f.options if _is_reasonable(o)) <= {"yes", "no"}:
                            value = "No"
                    return Answer(f.key, value or None, "profile" if value else "unknown", "self-identification from profile")
        if self._is_date_field(f) and re.search(r"\b(?:desired|available|availability|earliest|ideal|preferred)\b", lab, re.I):
            value = preferences.get("available_start")
            return Answer(f.key, value or None, "profile" if value else "unknown", "availability from profile")
        is_binary = bool(f.options and set(o.lower() for o in f.options) <= {"yes", "no", "select one", ""})
        if f.kind in ("select", "combobox") and not is_binary:
            short = lab.split(" - ")[-1].strip()
            if re.fullmatch(r"(?:state|province|state\s*/\s*province)[*✱\s]*", short, re.I):
                state = personal.get("state")
                if state:
                    return Answer(f.key, state, "profile", "state from profile")
            if re.fullmatch(r"(?:country|country\s*/\s*territory)[*✱\s]*", short, re.I):
                country = personal.get("country")
                return Answer(f.key, country or None, "profile" if country else "unknown", "country from profile")
        if _is_referral_source(f):
            source = preferences.get("how_did_you_hear")
            return Answer(f.key, str(source).strip() if source else None,
                          "profile" if source else "unknown", "recruitment source from profile")
        if _is_phone_device(f):
            device = (self.ctx.profile.get("personal") or {}).get("phone_device_type")
            return Answer(f.key, str(device).strip() if device else None,
                          "profile" if device else "unknown", "phone device type from profile")
        if self._broad_relationship_question(f):
            known_rel = _yes_no(preferences.get("personal_relationships_at_company"))
            if known_rel is not None:
                return Answer(f.key, known_rel, "profile", "personal relationships at company from profile")
            return Answer(f.key, None, "unknown", "broader personal relationships need confirmation")
        if self._former_employer_question(f):
            from .. import answers as answers_mod
            known = _yes_no(preferences.get("previously_employed_here"))
            if known is None:
                return Answer(f.key, None, "unknown", "previous employment with this employer not supplied")
            resume_path = self.ctx.profile.get("resume_path")
            resume = answers_mod.resume_text(resume_path) if resume_path else answers_mod.resume_text()
            if known == "No" and any(re.search(rf"\b{re.escape(w)}\b", resume, re.I)
                                     for w in self._employer_words()):
                return Answer(f.key, None, "unknown", "employer name appears in resume; verify history")
            return Answer(f.key, known, "profile", "previous employment with this employer from profile")
        return None

    def _former_employer_question(self, f: Field) -> bool:
        if f.kind not in ("radio", "select", "combobox"):
            return False
        label = f.label or ""
        if not re.search(r"previously (?:been )?(?:worked|employed)|(?:ever|formerly|prior)\b.{0,35}(?:worked|employed)|"
                         r"(?:current or )?former employee|worked (?:here|for us)|worked as a contractor", label, re.I):
            return False
        if re.search(r"\b(?:here|for us|our company|the company)\b", label, re.I):
            return True
        return any(re.search(rf"\b{re.escape(w)}\b", label, re.I) for w in self._employer_words())

    @staticmethod
    def _broad_relationship_question(f: Field) -> bool:
        label = f.label or ""
        return bool(re.search(r"(?:close\s+)?personal relationship|intimate|romantic|shared living|roommate|"
                              r"(?:domestic|business) partner|financial (?:or other )?(?:business )?partnership|"
                              r"interviewing", label, re.I)
                    and re.search(r"relative|family|relationship|partner|roommate", label, re.I))

    def mark_current_jobs(self) -> None:
        """A work-experience entry with a start date or primary role is the current job."""
        panels = self.page.locator(
            '[role=group][aria-labelledby*="Work-Experience"], [data-automation-id^="workExperience-"], '
            '[role=group][aria-labelledby$="-panel"][aria-labelledby*="Work"]'
        )
        for i in range(panels.count()):
            p = panels.nth(i)
            try:
                start = p.locator(f'{A("formField-startDate")} {A("dateSectionYear-input")}')
                end = p.locator(f'{A("formField-endDate")} {A("dateSectionYear-input")}')
                box = p.locator(f'{A("formField-currentlyWorkHere")} input[type="checkbox"], input[type="checkbox"][id*="currentlyWorkHere" i]')
                should_check = False
                if start.count() and start.first.input_value() and end.count() and not end.first.input_value():
                    should_check = True
                elif box.count() and not box.first.is_checked() and (panels.count() == 1 or i == 0):
                    should_check = True
                if should_check and box.count() and not box.first.is_checked():
                    box.first.check(timeout=5000, force=True)
                    log.info("marked work experience %d as current", i + 1)
                    key = box.first.get_attribute("data-aa-key")
                    if key:
                        self.answers[key] = Answer(key, True, "generated", "current job from profile")
            except Exception as e:
                log.debug("current-job check failed: %s", e)

    def is_honeypot(self, f: Field) -> bool:
        if HONEYPOT in (f.name or "").lower() or HONEYPOT in f.key.lower():
            return True
        try:
            aid = self.page.locator(f'[data-aa-key="{f.key}"]').first.get_attribute("data-automation-id", timeout=1000)
            return (aid or "").lower() == HONEYPOT
        except Exception:
            return False

    def extract_workday_widgets(self, root: str | None, known: set[str]) -> list[Field]:
        """Button listboxes, multiselect prompts and date sections that a generic extractor may miss."""
        scope = self.page.locator(root).first if root else self.page.locator("body")
        found: list[Field] = []
        selectors = ('button[aria-haspopup="listbox"], button[data-automation-id="promptButton"], '
                     'button[data-automation-id="promptInput"]', A("multiselectInputContainer") + " input",
                     'input[data-uxi-widget-type="selectinput"]', A("dateSectionMonth-input"),
                     A("dateSectionYear-input"))
        n = 0
        for sel in selectors:
            loc = scope.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    key = el.get_attribute("data-aa-key")
                    if key and key in known:
                        continue
                    info = el.evaluate("""el => {
                        const box = el.closest('[data-automation-id^="formField-"]') || el.closest('fieldset') || el.parentElement;
                        const wrapper = el.closest('[data-automation-id="dateInputWrapper"]') || box;
                        if (wrapper && wrapper.querySelector('[data-aa-key]')) {
                            const existing = wrapper.querySelector('[data-aa-key]');
                            if (existing !== el) return null;
                        }
                        const lab = box && (box.querySelector('[data-automation-id="formLabel"]') ||
                                            box.querySelector('legend') || box.querySelector('label'));
                        const label = (lab ? lab.innerText : (el.getAttribute('aria-label') || '')).trim();
                        const pills = box ? [...box.querySelectorAll('[data-automation-id="selectedItem"]')]
                            .map(p => p.innerText.trim()).filter(Boolean) : [];
                        const isFormField = !!el.closest('[data-automation-id^="formField-"]');
                        const req = /[✱*]/.test(label) || el.getAttribute('aria-required') === 'true' ||
                                    (isFormField && !!(box && box.querySelector('[aria-required="true"], [required]')));
                        return {label, required: req,
                                value: pills.length ? pills.join(' | ') : (el.innerText || el.value || '').trim(),
                                fid: box ? (box.getAttribute('data-automation-id') || '') : ''};
                    }""")
                    if not info:
                        continue
                    label = re.sub(r"\s*\*\s*$", "", info["label"]).strip() or info["fid"]
                    if re.fullmatch(r"(?:personal|contact|my)\s+information|work\s+experience|education|voluntary\s+disclosures|review", label, re.I):
                        continue
                    if not key:
                        n += 1
                        key = f"wd-{int(time.time() * 1000) % 10**7}-{n}"
                        el.evaluate("(el, k) => el.setAttribute('data-aa-key', k)", key)
                    kind = "date" if "dateSection" in sel else "select" if sel.startswith('button') else "combobox"
                    options = self.listbox_options(el) if kind == "select" else []
                    value = info["value"]
                    if kind == "select" and value.lower() in ("select one", ""):
                        value = ""
                    found.append(Field(key=key, label=label, kind=kind, required=bool(info["required"]),
                                       options=options, name=info["fid"], current_value=value))
                    known.add(key)
                    self.wd_keys.add(key)
                except Exception as e:
                    log.debug("workday widget skipped: %s", e)
        return found

    def extract_workday_radios(self, root: str | None) -> list[Field]:
        """Workday radio and checkbox group fields."""
        scope = self.page.locator(root).first if root else self.page.locator("body")
        found: list[Field] = []
        boxes = scope.locator('[data-automation-id^="formField-"]')
        for i in range(boxes.count()):
            box = boxes.nth(i)
            try:
                info = box.evaluate("""box => {
                    const radios = [...box.querySelectorAll('input[type="radio"], [role="radio"]')]
                        .filter(el => el.closest('[data-automation-id^="formField-"]') === box && !el.querySelector('input[type="radio"]'));
                    const checkboxes = [...box.querySelectorAll('input[type="checkbox"], [role="checkbox"]')]
                        .filter(el => el.closest('[data-automation-id^="formField-"]') === box && !el.querySelector('input[type="checkbox"]'));
                    const isRadio = radios.length >= 2;
                    const single = !isRadio && checkboxes.length === 1;
                    const isCheckbox = !isRadio && checkboxes.length >= 1;
                    if (!isRadio && !isCheckbox) return null;
                    const controls = isRadio ? radios : checkboxes;
                    const kind = isRadio ? 'radio' : 'checkbox';
                    const label = box.querySelector('[data-automation-id="formLabel"], legend, label.question') ||
                        (single && (controls[0].labels?.[0] || controls[0].closest('label'))) ||
                        [...box.querySelectorAll('label')].find(el => !el.htmlFor &&
                            !el.querySelector('input[type="radio"], [role="radio"], input[type="checkbox"], [role="checkbox"]'));
                    const question = (label?.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!question) return null;
                    const texts = controls.map((el, idx) => {
                        const linked = el.id && [...document.querySelectorAll('label[for]')]
                            .find(l => l.htmlFor === el.id);
                        const text = (linked?.innerText || el.closest('label')?.innerText ||
                            el.getAttribute('aria-label') || el.innerText || el.value || '').trim();
                        el.setAttribute('data-aa-radio-opt', String(idx));
                        el.setAttribute('data-aa-opt', String(idx));
                        return text;
                    });
                    if (!texts.length) return null;
                    const target = single ? controls[0] : box;
                    const key = target.getAttribute('data-aa-key') ||
                        'wd-choice-' + (window.__aaSeq = (window.__aaSeq || 0) + 1);
                    target.setAttribute('data-aa-key', key);
                    const checked = controls.find(el => el.checked || el.getAttribute('aria-checked') === 'true');
                    const current = checked ? texts[controls.indexOf(checked)] : '';
                    const required = /[✱*]/.test(question) || box.getAttribute('aria-required') === 'true' ||
                        !!box.querySelector('[required], [aria-required="true"]');
                    return {key, question, texts: single ? [] : texts,
                            current: single ? (checked ? 'true' : '') : current,
                            required, kind, name: box.getAttribute('data-automation-id') || ''};
                }""")
                if info:
                    found.append(Field(key=info["key"], label=re.sub(r"\s*[✱*]\s*$", "", info["question"]),
                                       kind=info.get("kind", "radio"), required=info["required"],
                                       options=info["texts"], current_value=info["current"], name=info["name"]))
                    self.wd_keys.add(info["key"])
            except Exception as e:
                log.debug("workday radio extraction skipped: %s", e)
        return found

    def _set_choice(self, ctl: Locator, wanted: bool) -> bool:
        def checked() -> bool:
            return ctl.evaluate("el => el.matches('input') ? el.checked : el.getAttribute('aria-checked') === 'true'")

        if checked() == wanted:
            return True
        label = ctl.locator("xpath=ancestor::label[1]")
        control_id = ctl.get_attribute("id")
        if not label.count() and control_id:
            label = self.page.locator(f'label[for={json.dumps(control_id)}]')
        targets = [label.first, ctl] if label.count() else [ctl]
        for target in targets:
            for force in (False, True):
                try:
                    target.click(timeout=1000, force=force)
                    self.page.wait_for_timeout(200)
                    if checked() == wanted:
                        return True
                except Exception:
                    continue
        try:
            ctl.evaluate("el => (el.labels?.[0] || el.closest('label') || el).click()")
            self.page.wait_for_timeout(200)
            return checked() == wanted
        except Exception:
            return False

    def fill_workday_radio(self, f: Field, a: Answer) -> bool:
        if a.value is None:
            return False
        box = self.page.locator(f'[data-aa-key="{f.key}"]').first
        if f.kind == "checkbox" and not f.options:
            aff = _yes_no(a.value)
            if aff is None or (f.required and aff != "Yes"):
                return False
            if not self._set_choice(box, aff == "Yes"):
                return False
            f.current_value = "true" if aff == "Yes" else "false"
            self.answers[f.key] = Answer(f.key, aff == "Yes", a.source, "Workday checkbox state verified")
            return True
        values = a.value if isinstance(a.value, list) else [a.value]
        indices = set()
        for value in values:
            value = _yes_no(value) or str(value)
            index = _pick_option_index(f.options, value, is_required=f.required)
            if index is None:
                return False
            indices.add(index)
        if not indices or (f.kind == "radio" and len(indices) != 1):
            return False
        for index in range(len(f.options)):
            if f.kind == "radio" and index not in indices:
                continue
            ctl = box.locator(f'input[data-aa-opt="{index}"], [role="checkbox"][data-aa-opt="{index}"], '
                              f'[role="radio"][data-aa-opt="{index}"]').first
            if not ctl.count() or not self._set_choice(ctl, index in indices):
                return False
        chosen = [f.options[i] for i in sorted(indices)]
        actual = self._control_values(box)
        if actual != chosen:
            return False
        f.current_value = " | ".join(chosen)
        self.answers[f.key] = Answer(f.key, f.current_value, a.source, "Workday choice selection verified")
        return True

    def _control_values(self, el: Locator) -> list[str]:
        return el.evaluate("""el => {
            const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
            const on = n => n.matches('input') ? n.checked : n.getAttribute('aria-checked') === 'true';
            if (el.matches('input[type="checkbox"], [role="checkbox"]')) return [on(el) ? 'true' : 'false'];
            const choices = [...el.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]')]
                .filter(n => !n.querySelector('input[type="radio"], input[type="checkbox"]'));
            if (choices.length) return choices.filter(on).map(n => clean(
                n.labels?.[0]?.innerText || n.closest('label')?.innerText || n.getAttribute('aria-label') || n.innerText || n.value));
            if (el.tagName === 'SELECT') return [...el.selectedOptions].map(o => clean(o.text));
            const box = el.closest('[data-automation-id^="formField-"]') || el.parentElement;
            const pills = box ? [...box.querySelectorAll('[data-automation-id="selectedItem"]')].map(n => clean(n.innerText)) : [];
            if (pills.length) return pills;
            if (el.matches('[data-uxi-widget-type="selectinput"], [role="combobox"]') ||
                el.closest('[data-automation-id="multiselectInputContainer"]')) return [];
            if (el.matches('input, textarea')) return [clean(el.value)];
            return [clean(el.innerText)];
        }""")

    def _mark_existing_menus(self) -> None:
        self.page.locator('[data-aa-existing-menu], [data-aa-existing-text], [data-aa-active-option]').evaluate_all(
            """els => els.forEach(el => {
                el.removeAttribute('data-aa-existing-menu');
                el.removeAttribute('data-aa-existing-text');
                el.removeAttribute('data-aa-active-option');
            })"""
        )
        self.page.locator('[role="listbox"]:visible, ' + A("promptOption") + ':visible').evaluate_all(
            """els => els.forEach(el => {
                el.setAttribute('data-aa-existing-menu', 'true');
                el.setAttribute('data-aa-existing-text', el.innerText.trim());
            })"""
        )

    def _dropdown_options(self, el: Locator, box: Locator) -> Locator | None:
        prompt = A("promptOption")
        option_sel = prompt + ':visible, [role="option"]:visible:not(:has(' + prompt + '))'
        for owner in (el, box):
            if not owner.count():
                continue
            for attribute in ("aria-controls", "aria-owns"):
                control_ids = (owner.first.get_attribute(attribute) or "").split()
                for control_id in control_ids:
                    popup = self.page.locator(f'[id={json.dumps(control_id)}]:visible')
                    if popup.count() == 1:
                        options = popup.locator(option_sel)
                        if options.count():
                            return options

        local = box.locator('[role="listbox"]:visible') if box.count() else self.page.locator(':not(*)')
        if local.count() == 1:
            return local.locator(option_sel)
        local_prompts = box.locator(prompt + ':visible') if box.count() else self.page.locator(':not(*)')
        if local_prompts.count():
            return local_prompts

        fresh = self.page.locator('[role="listbox"]:visible:not([data-aa-existing-menu])')
        if fresh.count() == 1:
            return fresh.locator(option_sel)
        if fresh.count() > 1:
            return None
        menus = self.page.locator('[role="listbox"]:visible')
        changed_indices = menus.evaluate_all(
            """els => els.flatMap((el, i) => el.hasAttribute('data-aa-existing-text') &&
                el.innerText.trim() !== el.getAttribute('data-aa-existing-text') ? [i] : [])"""
        )
        if len(changed_indices) == 1:
            changed = menus.nth(changed_indices[0]).locator(option_sel)
            if changed.count():
                return changed
        key = el.get_attribute("data-aa-key")
        fresh_prompts = self.page.locator(A("promptOption") + ':visible:not([data-aa-existing-menu])')
        if key and not fresh_prompts.count():
            # Escape is not implemented by every prompt. A retry may reopen the
            # same unchanged popup; reuse only options already tied to this control.
            owned_menus = self.page.locator(f'[role="listbox"][data-aa-dropdown-owner={json.dumps(key)}]:visible')
            if owned_menus.count() == 1:
                return owned_menus.locator(option_sel)
            if owned_menus.count() > 1:
                return None
            owned = self.page.locator(
                f'[data-aa-dropdown-owner={json.dumps(key)}]:visible:not([role="listbox"])'
                ':not([data-automation-id="selectedItem"] *)')
            if owned.count():
                return owned
        if fresh_prompts.count():
            # Workday can portal prompts outside the field without a role=listbox. React
            # may also remount an unrelated country-code menu on every field click.
            # Only use a single distinct popup group, never the union of all prompts.
            group_found = el.evaluate("""el => {
                const selector = '[data-automation-id="promptOption"]';
                const visible = n => !!(n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden');
                for (const n of document.querySelectorAll('[data-aa-active-option]'))
                    n.removeAttribute('data-aa-active-option');
                const fresh = [...document.querySelectorAll(selector)].filter(n =>
                    visible(n) && !n.hasAttribute('data-aa-existing-menu'));
                if (!fresh.length) return false;
                const groups = new Map();
                for (const n of fresh) {
                    let root = n.parentElement;
                    while (root && root !== document.body && root.querySelectorAll(selector).length < 2)
                        root = root.parentElement;
                    if (!root || root === document.body) root = n.parentElement;
                    if (!groups.has(root)) groups.set(root, []);
                    groups.get(root).push(n);
                }
                const entries = [...groups.entries()];
                let chosen = entries.length === 1 ? entries[0] : null;
                if (!chosen) {
                    const rect = el.getBoundingClientRect();
                    const scored = entries.map(entry => {
                        const r = entry[0].getBoundingClientRect();
                        const overlap = Math.max(0, Math.min(rect.right, r.right) - Math.max(rect.left, r.left));
                        const gap = Math.max(0, r.top - rect.bottom, rect.top - r.bottom);
                        return {entry, score: overlap > 0 && gap < 160 ? gap : Infinity};
                    }).sort((a,b) => a.score - b.score);
                    if (scored[0].score < Infinity &&
                        (scored.length === 1 || scored[1].score - scored[0].score > 30))
                        chosen = scored[0].entry;
                }
                if (!chosen) return false;
                for (const n of chosen[1]) n.setAttribute('data-aa-active-option', 'true');
                return true;
            }""")
            if group_found:
                return self.page.locator(prompt + ':visible[data-aa-active-option="true"]')
        return None

    def listbox_options(self, el: Locator) -> list[str]:
        try:
            self._mark_existing_menus()
            el.click(timeout=5000)
            box = el.locator("xpath=ancestor::*[starts-with(@data-automation-id,'formField-')][1]")
            opts_loc = self._dropdown_options(el, box)
            opts = [" ".join(t.split()) for t in opts_loc.all_inner_texts()] if opts_loc else []
            self._close_dropdown(el, box)
            self.page.wait_for_timeout(200)
            return [o for o in opts if o and o.lower() != "select one"]
        except Exception:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return []

    def _close_dropdown(self, el: Locator, box: Locator) -> None:
        self.page.keyboard.press("Escape")
        # Some Workday multiselects reset their search on Escape but keep the
        # popup over the following fields until focus and the outside click move.
        if el.evaluate("el => el.tagName === 'INPUT'"):
            el.evaluate("el => el.blur()")
        if box.count():
            box.evaluate("el => el.click()")

    def _scroll_dropdown_to_match(self, el: Locator, box: Locator, value: str,
                                  aliases: tuple[str, ...] = (), *, exact: bool = False) -> tuple[Locator, list[str], int] | None:
        """Search lazy options, preferring an exact source over any category alias."""
        seen_aliases: set[str] = set()
        match = _exact_option if exact else _match_option
        for _ in range(50):
            opts = self._dropdown_options(el, box)
            if opts is None or not opts.count():
                return None
            texts = [" ".join(t.split()) for t in opts.all_inner_texts()]
            index = match(texts, value)
            if index is not None:
                return opts, texts, index
            seen_aliases.update(alias for alias in aliases if match(texts, alias) is not None)
            try:
                moved = opts.last.evaluate("""option => {
                    let parent = option.parentElement;
                    while (parent && parent !== document.body) {
                        const style = getComputedStyle(parent);
                        if (parent.scrollHeight > parent.clientHeight + 5 &&
                            /auto|scroll/.test(style.overflowY)) {
                            const before = parent.scrollTop;
                            parent.scrollTop += Math.max(80, Math.floor(parent.clientHeight * .85));
                            parent.dispatchEvent(new Event('scroll', {bubbles: true}));
                            return parent.scrollTop > before;
                        }
                        parent = parent.parentElement;
                    }
                    return false;
                }""")
            except Exception:
                break
            if not moved:
                # At the bottom of a lazy list, a pending scroll handler may
                # append an option without changing scrollTop again.
                self.page.wait_for_timeout(450)
                opts = self._dropdown_options(el, box)
                if opts is not None and opts.count():
                    texts_after = [" ".join(t.split()) for t in opts.all_inner_texts()]
                    index = match(texts_after, value)
                    if index is not None:
                        return opts, texts_after, index
                    if texts_after != texts:
                        continue
                break
            self.page.wait_for_timeout(200)
        # Workday may recycle the option nodes during scrolling, so return to the
        # top and locate the preferred available generic category again.
        for alias in aliases:
            if alias not in seen_aliases:
                continue
            for _ in range(50):
                opts = self._dropdown_options(el, box)
                if opts is None or not opts.count():
                    return None
                texts = [" ".join(t.split()) for t in opts.all_inner_texts()]
                index = match(texts, alias)
                if index is not None:
                    return opts, texts, index
                try:
                    moved = opts.first.evaluate("""option => {
                        let parent = option.parentElement;
                        while (parent && parent !== document.body) {
                            const style = getComputedStyle(parent);
                            if (parent.scrollHeight > parent.clientHeight + 5 &&
                                /auto|scroll/.test(style.overflowY)) {
                                const before = parent.scrollTop;
                                parent.scrollTop -= Math.max(80, Math.floor(parent.clientHeight * .85));
                                parent.dispatchEvent(new Event('scroll', {bubbles: true}));
                                return parent.scrollTop < before;
                            }
                            parent = parent.parentElement;
                        }
                        return false;
                    }""")
                except Exception:
                    return None
                if not moved:
                    break
                self.page.wait_for_timeout(120)
        return None

    def fill_workday_widget(self, f: Field, a: Answer) -> bool:
        el = self.page.locator(f'[data-aa-key="{f.key}"]').first
        values = a.value if isinstance(a.value, list) else [a.value]
        try:
            if f.kind in ("radio", "checkbox"):
                return self.fill_workday_radio(f, a)
            is_date = self._is_date_field(f)
            if not is_date:
                try:
                    is_date = el.evaluate("""el => !!(
                        el.matches('[data-automation-id*="dateSection" i]') ||
                        el.closest('[data-automation-id*="dateSection" i], [data-automation-id="dateInputWrapper"]')
                    )""")
                except Exception:
                    pass

            if is_date:
                return self.fill_date(el, str(a.value or ""))

            if el.evaluate("e => e.tagName === 'SELECT'"):
                options = el.evaluate("e => [...e.options].map(o => o.text.trim())")
                value = values[0] if values else None
                value = ("Yes" if value else "No") if isinstance(value, bool) else str(value or "")
                is_lang = bool(re.search(r"language", (f.label or "") + " " + (f.name or ""), re.I))
                idx = _pick_option_index(options, value, is_phone_device=_is_phone_device(f),
                                         is_referral=_is_referral_source(f), is_language=is_lang, is_required=f.required)
                if idx is None or not _is_reasonable(options[idx]):
                    return False
                el.select_option(index=idx, timeout=5000)
                if el.evaluate("e => e.selectedOptions[0]?.text.trim() || ''") != options[idx]:
                    return False
                source = "generated" if _missing(a) else a.source
                self.answers[f.key] = Answer(f.key, options[idx], source, f"selected: {options[idx]}")
                f.current_value = options[idx]
                return True

            ok = True
            for v in values:
                if isinstance(v, bool):
                    v = "Yes" if v else "No"
                v = "" if v is None or v == "None" else str(v)
                match = _exact_option if _is_referral_source(f) or _is_phone_device(f) else _match_option
                if match(self._control_values(el), v) is not None:
                    continue
                cands = [v]
                ok = any(self.choose_option(el, c, multi=f.kind == "combobox", field=f, required=f.required) for c in cands) and ok
            return ok
        except Exception as e:
            log.debug("workday widget fill failed for %s: %s", f.label, e)
            return False

    def choose_option(self, el: Locator, value: str, *, multi: bool = False,
                      field: Field | None = None, required: bool | None = None) -> bool:
        box = el.locator("xpath=ancestor::*[starts-with(@data-automation-id,'formField-')][1]")
        try:
            selected_before = box.locator(A("selectedItem")).count() if multi else 0
        except Exception:
            selected_before = 0

        # Determine field context (label, required, automation id)
        key = None
        try:
            key = el.get_attribute("data-aa-key")
        except Exception:
            pass

        f = field or (self.fields.get(key) if key else None)
        label = (f.label if f and f.label else "").strip()
        is_required = f.required if f else False
        fid = (f.name if f and f.name else "").strip()

        if required is not None:
            is_required = required

        if not label or not fid or (required is None and not is_required):
            try:
                dom_info = el.evaluate("""el => {
                    const box = el.closest('[data-automation-id^="formField-"]') || el.closest('fieldset') || el.parentElement;
                    const lab = box && (box.querySelector('label') || box.querySelector('legend'));
                    const label = (lab ? lab.innerText : (el.getAttribute('aria-label') || '')).trim();
                    const req = /\\*/.test(label) || el.getAttribute('aria-required') === 'true' ||
                                (box && (box.getAttribute('data-automation-id') || '').toLowerCase().includes('required'));
                    const fid = box ? (box.getAttribute('data-automation-id') || '') : '';
                    return {label, required: req, fid};
                }""")
                if not label:
                    label = (dom_info.get("label") or "").strip()
                if required is None and not is_required:
                    is_required = bool(dom_info.get("required", False))
                if not fid:
                    fid = (dom_info.get("fid") or "").strip()
            except Exception:
                pass

        context_field = Field(key or "", label, f.kind if f else "combobox" if multi else "select",
                              is_required, name=fid)
        is_phone_device = _is_phone_device(context_field)
        is_referral = _is_referral_source(context_field)
        is_school = self._is_school_field(f) if f else bool(
            re.search(r"\b(school|university|institution|college)\b", label, re.I) or
            re.search(r"school|university|institution|college", fid, re.I)
        )

        try:
            self._mark_existing_menus()
            el.click(timeout=5000)
        except Exception:
            try:
                el.click(timeout=5000, force=True)
            except Exception as e:
                log.info("could not click dropdown element: %s", e)
                return False

        # If element or box has an input, or if multi/school, type the value to filter options
        inp = None
        try:
            if el.evaluate("e => e.tagName === 'INPUT'"):
                inp = el
            elif box.locator('input:not([type="hidden"])').count():
                inp = box.locator('input:not([type="hidden"])').first
        except Exception:
            pass

        target_inp = inp or el
        if value and (multi or is_school or (inp and inp.is_visible())):
            try:
                target_inp.fill("")
                target_inp.press_sequentially(value, delay=20)
            except Exception:
                try:
                    target_inp.type(value, delay=20)
                except Exception:
                    pass

        self.page.wait_for_timeout(1000)
        if value:
            try:
                popup_inp = self.page.locator('[role="dialog"] input:visible, [role="listbox"] input:visible, input[data-automation-id="searchBox"]:visible, input[placeholder*="Search" i]:visible')
                if popup_inp.count() and popup_inp.first.is_visible():
                    popup_inp.first.fill("")
                    popup_inp.first.press_sequentially(value, delay=20)
                    popup_inp.first.press("Enter")
                    self.page.wait_for_timeout(800)
            except Exception:
                pass
        picked = False
        chosen_text: str | None = None
        last_texts: list[str] = []

        # Hierarchical prompts (e.g. "How did you hear about us") open a sub-list after the first click
        for step in range(3):
            opts = self._dropdown_options(el, box)
            try:
                opts_count = opts.count() if opts else 0
            except Exception:
                opts_count = 0

            if opts_count == 0:
                if picked:
                    break
                self.page.wait_for_timeout(500)
                opts = self._dropdown_options(el, box)
                try:
                    opts_count = opts.count() if opts else 0
                except Exception:
                    opts_count = 0
                if opts_count == 0:
                    break

            try:
                texts = [" ".join(t.split()) for t in opts.all_inner_texts()]
            except Exception:
                texts = []

            if not texts:
                break

            if key:
                opts.evaluate_all("""(options, key) => options.forEach(o => {
                    o.setAttribute('data-aa-dropdown-owner', key);
                    o.closest('[role="listbox"]')?.setAttribute('data-aa-dropdown-owner', key);
                })""", key)

            if picked and texts == last_texts:
                break
            last_texts = texts

            i = None
            if value and (is_referral or re.search(r"field\s*of\s*study|major", label + ' ' + fid, re.I)):
                aliases = _referral_aliases(value) if is_referral else ()
                scrolled = self._scroll_dropdown_to_match(el, box, value, aliases, exact=is_referral)
                if scrolled:
                    opts, texts, i = scrolled
            if i is None:
                is_lang = bool(re.search(r"language", label + " " + fid, re.I))
                i = _pick_option_index(
                    texts, value,
                    is_phone_device=is_phone_device,
                    is_referral=is_referral,
                    is_language=is_lang,
                    is_required=is_required,
                )
            if i is None:
                for idx, t in enumerate(texts):
                    if t.strip().lower() in ("all", "search by category", "partial list (first 500 entries)"):
                        i = idx
                        break
                if i is None and value:
                    from ... import llm
                    i = llm.pick_option(texts, value, label=f"Navigate prompt category to find {value}")
            if i is None:
                log.info("workday option %r not matched in %s", value, texts[:15])
                break

            chosen_text = texts[i]
            opts.evaluate_all("""options => {
                for (const menu of document.querySelectorAll(
                    '[role="listbox"], [data-automation-id="promptOption"]')) {
                    if (!menu.getClientRects().length || options.some(o => menu === o || menu.contains(o)))
                        continue;
                    menu.setAttribute('data-aa-existing-menu', 'true');
                    menu.setAttribute('data-aa-existing-text', menu.innerText.trim());
                }
            }""")
            try:
                opts.nth(i).click(timeout=5000)
                picked = True
            except Exception:
                try:
                    opts.nth(i).click(timeout=5000, force=True)
                    picked = True
                except Exception as e:
                    log.info("workday failed clicking option %d (%r): %s", i, chosen_text, e)
                    break

            self.page.wait_for_timeout(700)

            is_category_click = chosen_text.strip().lower() in (
                "all", "search by category", "partial list (first 500 entries)", "search by location"
            )
            if is_category_click or (value and _match_option([chosen_text], value) is None):
                next_texts = []
                for _ in range(5):
                    self.page.wait_for_timeout(500)
                    next_options = self._dropdown_options(el, box)
                    next_texts = [" ".join(t.split()) for t in next_options.all_inner_texts()] if next_options else []
                    if next_texts and next_texts != texts:
                        break
                if next_texts and next_texts != texts:
                    picked = False
                    continue

            try:
                selected = box.locator(A("selectedItem"))
                if selected.count() > selected_before:
                    break
                state = self._control_values(el)
                if _match_option(state, chosen_text) is not None:
                    if not value or _match_option([chosen_text], value) is not None:
                        break
                    next_options = self._dropdown_options(el, box)
                    next_texts = next_options.all_inner_texts() if next_options else []
                    if _match_option(next_texts, value) is None:
                        break
            except Exception:
                pass

            try:
                still_open = bool(self._dropdown_options(el, box))
            except Exception:
                still_open = False

            if not still_open:
                break

        if picked and chosen_text:
            try:
                selected = box.locator(A("selectedItem"))
                if selected.count() > selected_before and _match_option(selected.all_inner_texts(), chosen_text) is not None:
                    picked = True
                else:
                    state = self._control_values(el)
                    picked = _match_option(state, chosen_text) is not None
            except Exception:
                picked = False

        if multi or not picked:
            try:
                self._close_dropdown(el, box)
            except Exception:
                pass

        if picked and chosen_text and key:
            old_ans = self.answers.get(key)
            src = old_ans.source if (old_ans and old_ans.source != "unknown") else "generated"
            self.answers[key] = Answer(key, chosen_text, src, f"selected: {chosen_text}")
            if f:
                f.current_value = chosen_text

        return picked

    def fill_date(self, el: Locator, value: str) -> bool:
        parsed = parse_date(value)
        if not parsed:
            return False
        y, mo, d = parsed
        has_month_value = not re.fullmatch(r"\d{4}", value.strip())
        has_day_value = bool(re.fullmatch(r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4})", value.strip()))
        try:
            info = el.evaluate("""el => {
                const w = el.closest('[data-automation-id="dateInputWrapper"]') || el.parentElement;
                const isSection = !!(el.matches('[data-automation-id*="dateSection" i]') ||
                                     el.closest('[data-automation-id*="dateSection" i], [data-automation-id="dateInputWrapper"]'));
                const hasMonth = !!(w && w.querySelector('[data-automation-id="dateSectionMonth-input"]'));
                const hasDay = !!(w && w.querySelector('[data-automation-id="dateSectionDay-input"]'));
                const hasYear = !!(w && w.querySelector('[data-automation-id="dateSectionYear-input"]'));
                return {isSection, hasMonth, hasDay, hasYear};
            }""")
        except Exception:
            info = {"isSection": False, "hasMonth": False, "hasDay": False, "hasYear": False}

        if info.get("isSection"):
            has_day = info.get("hasDay")
            has_month = info.get("hasMonth", True)
            if (has_month and not has_month_value) or (has_day and not has_day_value):
                return False
            # the spinbutton inputs sit outside the viewport, so focus them instead of clicking
            el.evaluate("e => e.focus()")
            if not has_month:
                text = f"{y}"
            elif has_day:
                text = f"{mo:02d}{d:02d}{y}"
            else:
                text = f"{mo:02d}{y}"
            self.page.keyboard.type(text, delay=60)
            self.page.wait_for_timeout(200)
            actual = el.evaluate("""el => {
                const w = el.closest('[data-automation-id="dateInputWrapper"]') || el.parentElement;
                const get = part => w.querySelector(`[data-automation-id="dateSection${part}-input"]`)?.value ?? null;
                return {month: get('Month'), day: get('Day'), year: get('Year')};
            }""")
            expected = {"month": mo, "day": d, "year": y}
            return bool(info.get("hasYear")) and all(
                actual[part] is not None and actual[part].isdigit() and int(actual[part]) == expected[part]
                for part, present in (("month", has_month), ("day", has_day), ("year", info.get("hasYear")))
                if present
            )
        else:
            # Preserve a supplied day; native date inputs require ISO format.
            full_date = bool(re.fullmatch(r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4})", value.strip()))
            input_type = (el.get_attribute("type") or "").lower()
            placeholder = (el.get_attribute("placeholder") or "").lower()
            if (input_type == "date" or "mm/dd" in placeholder or "yyyy-mm-dd" in placeholder) and not has_day_value:
                return False
            if not has_month_value and not re.fullmatch(r"y{4}|year", placeholder.strip(), re.I):
                return False
            if re.fullmatch(r"y{4}|year", placeholder.strip(), re.I):
                formatted = f"{y:04d}"
            elif input_type == "date" or "yyyy-mm" in placeholder:
                formatted = f"{y:04d}-{mo:02d}-{d:02d}"
            elif full_date or "mm/dd" in placeholder:
                formatted = f"{mo:02d}/{d:02d}/{y:04d}"
            else:
                formatted = f"{mo:02d}/{y:04d}"
            try:
                el.fill("")
                el.fill(formatted)
            except Exception:
                el.evaluate("e => e.focus()")
                self.page.keyboard.type(formatted, delay=30)
            self.page.wait_for_timeout(200)
            val = el.input_value(timeout=2000)
            # Compare the full rendered value as well as its parsed date so a truncated
            # month/year cannot masquerade as a full date whose day happened to be 1.
            return val == formatted and parse_date(val) == parsed

    def upload_resume(self) -> None:
        path = self.ctx.profile.get("resume_path")
        inp = self.page.locator(f'input[type="file"]{A("file-upload-input-ref")}, {A("file-upload-input-ref")}')
        if not path or not inp.count():
            return
        if _visible(self.page, A("file-upload-successful")) or _visible(self.page, A("delete-file")):
            return
        try:
            inp.first.set_input_files(path)
            self.page.locator(f'{A("file-upload-successful")}, {A("delete-file")}').first.wait_for(timeout=30000)
            self.settle()
        except Exception as e:
            log.info("resume upload not confirmed: %s", e)

    def fill_self_identify(self) -> dict[str, Answer]:
        out: dict[str, Answer] = {}
        name = self.ctx.profile.get("personal", {}).get("full_name", "")
        today = time.strftime("%m/%d/%Y")
        for sel in (A("formField-name") + " input", 'input[data-automation-id="name"]'):
            loc = self.page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.fill(name)
                key = loc.first.get_attribute("data-aa-key")
                if key:
                    out[key] = Answer(key, name, "profile", "self identify name")
                break
        month = self.page.locator(A("dateSectionMonth-input"))
        if month.count() and month.first.is_visible():
            self.fill_date(month.first, today)
            key = month.first.get_attribute("data-aa-key")
            if key:
                out[key] = Answer(key, today, "profile", "today")
        return out

    def click_next(self) -> bool:
        """Next/Save and Continue only; on the Review step the same button is the final Submit."""
        if _is_submit_action(next_button_text(self.page)):
            return False
        human_pause()
        return self.click(NEXT)

    def advance(self, name: str) -> None:
        if _is_submit_action(next_button_text(self.page)):
            return
        if not self.click_next():
            if _OOPS_RE.search(_text(self.page, ROOT)):
                return
            raise self.stop(ApplyStatus.NEEDS_REVIEW, f"could not click Next on '{name}'", "next")
        self.settle()
        for _ in range(10):
            if error_texts(self.page) or step_name(self.page) != name or _is_submit_action(next_button_text(self.page)):
                break
            self.page.wait_for_timeout(500)
        errs = error_texts(self.page)
        if not errs or step_name(self.page) != name:
            return
        log.info("workday step '%s' errors: %s", name, errs)
        if re.search(r"experience", name, re.I):
            self.drop_bad_work_experience()
        self.fill_step(name, repair=errs,
                       extra_context="The form rejected these fields: " + "; ".join(errs)[:1500])
        self.click_next()
        self.settle()
        for _ in range(10):
            if error_texts(self.page) or step_name(self.page) != name or _is_submit_action(next_button_text(self.page)):
                break
            self.page.wait_for_timeout(500)
        errs = error_texts(self.page)
        if errs and step_name(self.page) == name:
            raise self.stop(ApplyStatus.NEEDS_REVIEW, "form errors: " + "; ".join(errs)[:400], "errors")

    def errored_keys(self, fields: list[Field], errs: list[str]) -> set[str]:
        keys: set[str] = set()
        blob = " ".join(errs).lower()
        for f in fields:
            flab = (f.label or "").strip().lower()
            short = flab.split(" - ")[-1].strip()
            if short and len(short) > 3:
                if short in blob or f"field {short}" in blob:
                    keys.add(f.key)
            elif flab and flab in blob:
                keys.add(f.key)
        try:
            keys.update(self.page.evaluate("""(keys) => keys.filter(k => {
                const el = document.querySelector(`[data-aa-key="${k}"]`);
                const box = el && (el.closest('[data-automation-id^="formField-"]') || el.closest('fieldset') || el.parentElement);
                return !!(box && (box.querySelector('[data-automation-id="errorMessage"], [aria-invalid="true"], [role="alert"], [class*="error" i]')));
            })""", [f.key for f in fields]))
        except Exception:
            pass
        return keys

    def drop_bad_work_experience(self) -> None:
        """Delete invalid auto-parsed work experience; preserve education for repair."""
        try:
            n = self.page.evaluate("""() => {
                let n = 0;
                const clickDel = (panel) => {
                    const del = panel.querySelector('[data-automation-id="panel-set-delete-button"]') ||
                        [...panel.querySelectorAll('button')].find(b => /^delete/i.test((b.innerText || b.getAttribute('aria-label') || '').trim()));
                    if (del) {
                        del.click();
                        const confirm = document.querySelector('[data-automation-id="delete-confirmation-button"], [data-automation-id="confirm-button"], [data-automation-id="popup_ok"]');
                        if (confirm) confirm.click();
                        return true;
                    }
                    return false;
                };

                const hasErrors = (panel) => !!panel.querySelector('[data-automation-id="errorMessage"], [aria-invalid="true"]');

                const isCompletelyEmpty = (panel) => {
                    const inputs = [...panel.querySelectorAll('input, textarea, select')];
                    const hasVal = inputs.some(inp => {
                        if (inp.type === 'checkbox' || inp.type === 'radio') return inp.checked;
                        const v = (inp.value || '').trim();
                        return v && !['select one', '-- select --', 'select', 'choose one', 'please select'].includes(v.toLowerCase());
                    });
                    const hasPills = !!panel.querySelector('[data-automation-id="selectedItem"]');
                    return !hasVal && !hasPills;
                };

                const workPanels = document.querySelectorAll(
                    '[role=group][aria-labelledby^="Work-Experience-"][aria-labelledby$="-panel"], [role=group][aria-labelledby*="Work-Experience"], [role=group][aria-labelledby*="workExperience"], [data-automation-id^="workExperience-"]');
                for (const panel of workPanels) {
                    if (hasErrors(panel) || isCompletelyEmpty(panel)) {
                        if (clickDel(panel)) n++;
                    }
                }

                return n;
            }""")
            if n:
                log.info("deleted %d invalid or empty work-experience entries", n)
                self.page.wait_for_timeout(1000)
        except Exception:
            pass

    def _review_rows(self) -> list[dict]:
        return self.page.evaluate("""rootSelector => {
            const root = document.querySelector(rootSelector) || document.body;
            const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
            const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
            const headings = 'h1,h2,h3,h4,h5,h6,[role="heading"]';
            const labels = '[data-automation-id="formLabel"],label,dt';
            const rows = [];
            let context = '';
            for (const label of root.querySelectorAll(headings + ',' + labels)) {
                if (!visible(label) || label.closest('[role="listbox"], [role="option"]')) continue;
                const name = clean(label.innerText);
                if (!name) continue;
                if (label.matches(headings)) context = name;
                const field = label.closest('[data-automation-id^="formField-"]');
                let value = '';
                if (field) {
                    if (field.querySelector('input,select,textarea,[role="checkbox"],[role="radio"]')) continue;
                    const walker = document.createTreeWalker(field, NodeFilter.SHOW_TEXT);
                    let node;
                    const parts = [];
                    while ((node = walker.nextNode())) {
                        const parent = node.parentElement;
                        if (!label.contains(node) && visible(parent) && !parent.closest('button,script,style,[role="listbox"]'))
                            parts.push(node.textContent);
                    }
                    value = clean(parts.join(' '));
                } else {
                    let sibling = label.nextElementSibling;
                    if (!sibling && label.parentElement?.children.length === 1)
                        sibling = label.parentElement.nextElementSibling;
                    if (sibling && !sibling.matches(headings + ',' + labels) && visible(sibling) &&
                        !sibling.querySelector('input,select,textarea')) value = clean(sibling.innerText);
                }
                if (value) rows.push({label: name, value, context,
                    name: field?.getAttribute('data-automation-id') || ''});
            }
            return rows;
        }""", ROOT)

    def _value_matches(self, f: Field, expected, actual: list[str]) -> bool:
        values = expected if isinstance(expected, list) else [expected]
        if f.kind == "checkbox" and not f.options:
            return len(actual) == 1 and _yes_no(actual[0]) == _yes_no(expected) and (
                not f.required or _yes_no(actual[0]) == "Yes")
        if self._is_date_field(f):
            wanted = parse_date(str(expected))
            if not wanted or len(actual) != 1:
                return False
            got = parse_date(actual[0])
            full_date = r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4})"
            if bool(re.fullmatch(full_date, str(expected).strip())) != bool(re.fullmatch(full_date, actual[0].strip())):
                return False
            if re.fullmatch(r"\d{4}", str(expected).strip()):
                return bool(re.fullmatch(r"\d{4}", actual[0].strip()) and got and got[0] == wanted[0])
            return got == wanted
        if f.kind in ("select", "combobox", "radio", "checkbox"):
            indices = {_pick_option_index(actual, _yes_no(value) or str(value),
                         is_referral=_is_referral_source(f), is_phone_device=_is_phone_device(f)) for value in values}
            return None not in indices and len(indices) == len(actual) and bool(actual)
        normalize = lambda value: " ".join(str(value).casefold().split())
        if f.kind == "file":
            from pathlib import Path
            return len(actual) == 1 and Path(str(expected)).name.casefold() in actual[0].casefold()
        return len(actual) == 1 and normalize(expected) == normalize(actual[0])

    def verify_review(self) -> list[str]:
        """Compare the current controls and read-only summary with grounded answers."""
        issues = []
        rows = self._review_rows()
        normalize = lambda text: re.sub(r"[\s*✱:]+$", "", " ".join(str(text).casefold().split()))
        for f in list(self.fields.values()):
            grounded = self._profile_widget_answer(f)
            expected = grounded or self.expected_answers.get(f.key) or self.answers.get(f.key)
            if _missing(expected):
                if grounded is not None:
                    self.answers[f.key] = grounded
                continue
            el = self.page.locator(f'[data-aa-key="{f.key}"]').first
            if el.count() and el.evaluate("""el => el.matches('input,select,textarea,button,[role="checkbox"],[role="radio"]') ||
                    !!el.querySelector('input,select,textarea,[role="checkbox"],[role="radio"]')"""):
                if self._is_date_field(f):
                    actual = el.evaluate("""el => {
                        const box = el.closest('[data-automation-id="dateInputWrapper"]') || el.parentElement;
                        const get = part => box.querySelector(`[data-automation-id="dateSection${part}-input"]`)?.value;
                        const year = get('Year'), month = get('Month'), day = get('Day');
                        return year !== undefined ? [month !== undefined ?
                            `${month}/${day !== undefined ? day + '/' : ''}${year}` : year] : [el.value || ''];
                    }""")
                elif f.kind == "file":
                    actual = el.evaluate("el => [...(el.files || [])].map(file => file.name)")
                else:
                    actual = self._control_values(el)
                matched = self._value_matches(f, expected.value, actual)
            else:
                candidates = [row for row in rows if (
                    f.name and row['name'] == f.name) or normalize(row['label']) in {
                        normalize(f.label), normalize(f.label.rsplit(' - ', 1)[-1])}]
                if len(candidates) > 1:
                    panel = normalize(f.label.rsplit(' - ', 1)[0])
                    candidates = [row for row in candidates if normalize(row['context']) == panel]
                if not candidates and not f.required:
                    continue
                matched = len(candidates) == 1 and self._value_matches(f, expected.value, [candidates[0]['value']])

            if not matched:
                # Check composite review cards (e.g. Legal Name, Address, Phone) or overall review text
                exp_str = str(expected.value).strip()
                flab = normalize(f.label)
                if _is_referral_source(f) or any(w in flab for w in ("hear about", "referral", "source")):
                    matched = any(_is_reasonable(r['value']) for r in rows if _is_referral_source(Field(key='', label=r['label'], kind='select', required=False)) or any(w in normalize(r['label']) for w in ("hear about", "referral", "source")))
                elif _is_phone_device(f) or "phone" in flab or "calling code" in flab or "dial" in flab:
                    digits = re.findall(r"\+\d+", exp_str)
                    code = digits[0] if digits else exp_str
                    matched = any(code.casefold() in r['value'].casefold() or exp_str.casefold() in r['value'].casefold() for r in rows if "phone" in r['label'].lower())
                elif flab in ("first name", "last name", "middle name", "preferred name"):
                    matched = any(exp_str.casefold() in r['value'].casefold() for r in rows if "name" in r['label'].lower())
                elif any(w in flab for w in ("country", "state", "province", "city", "postal", "zip", "address")):
                    matched = any(exp_str.casefold() in r['value'].casefold() for r in rows if "address" in r['label'].lower() or "contact" in r['context'].lower() or "information" in r['context'].lower())
                if not matched and exp_str:
                    review_body = " ".join(r['value'] for r in rows)
                    matched = exp_str.casefold() in review_body.casefold()
            if not matched:
                self.answers[f.key] = Answer(f.key, None, "unknown", "review value could not be verified")
                issues.append(f.label or f.key)
        for row in rows:
            label, value = normalize(row['label']), normalize(row['value'])
            field = Field('wd-review-' + str(len(self.fields)), row['label'], 'select', True, name=row['name'])
            if _is_referral_source(field):
                if not _is_reasonable(row['value']):
                    issues.append(field.label)
            elif (self._is_date_field(field) and re.search(
                    r'\b(?:desired|available|earliest|ideal|preferred)\b', field.label, re.I)):
                if self._is_date_field(field):
                    field.kind = 'date'
                expected = self._profile_widget_answer(field)
                if expected and not _missing(expected) and not self._value_matches(field, expected.value, [row['value']]):
                    self.fields[field.key] = field
                    self.answers[field.key] = Answer(field.key, None, 'unknown', 'review contradicts profile')
                    issues.append(field.label)
        return list(dict.fromkeys(issues))

    def review(self) -> ApplyResult:
        self.furthest = step_name(self.page) or "Review"
        if is_review(self.page) and self.page.locator(f'{ROOT} input, {ROOT} select, {ROOT} [role="checkbox"]').count():
            self.fill_step(self.furthest, repair=[])
        try:
            issues = self.verify_review()
        except Exception as error:
            log.info("Workday review inspection failed: %s", error)
            issues = ['review could not be inspected']
        if issues:
            return self.result(ApplyStatus.NEEDS_REVIEW, "review verification failed: " + "; ".join(issues[:8]), "review")
        if not self.fields:
            return self.result(ApplyStatus.NEEDS_REVIEW, "no application fields verified", "review")
        fields, answers = list(self.fields.values()), list(self.answers.values())
        ok, reason = submit_decision(fields, answers, self.ctx.submit_mode, self.ctx.dry_run)
        if not ok:
            shot = self.ctx.screenshot(self.job, "review")
            res = blocked_result(fields, answers, f"{reason} [step: {self.furthest}]", self.ctx.dry_run,
                                 shot, self._url())
            return res
        from .. import captcha
        outcome = captcha.solve_if_present(self.page, self.ctx.config, self.job.id)
        if captcha.blocks_submit(outcome, self.ctx.config):
            return self.result(ApplyStatus.NEEDS_REVIEW, f"captcha {outcome.status}: {outcome.detail}", "captcha")
        try:
            issues = self.verify_review()
        except Exception:
            issues = ['review could not be inspected']
        ok, reason = submit_decision(list(self.fields.values()), list(self.answers.values()),
                                     self.ctx.submit_mode, self.ctx.dry_run)
        if issues or not ok:
            return self.result(ApplyStatus.NEEDS_REVIEW,
                               "review verification failed: " + ("; ".join(issues[:8]) if issues else reason), "review")
        if not _is_final_submit(next_button_text(self.page)):
            return self.result(ApplyStatus.NEEDS_REVIEW, "submit button not found on review", "review")
        if _DONE_RE.search(_text(self.page, "body")) or _visible(self.page, A("congratulationsPopup")):
            return self.result(ApplyStatus.NEEDS_REVIEW,
                               "submission confirmation already visible before final click", "review")
        human_pause()
        self.ctx.mark_submit_attempt(self.job.id)
        if not self.click(NEXT):
            return self.result(ApplyStatus.NEEDS_REVIEW, "final submit click failed", "submit")
        end = time.monotonic() + 45
        while time.monotonic() < end:
            self.page.wait_for_timeout(1000)
            if _DONE_RE.search(_text(self.page, "body")) or _visible(self.page, A("congratulationsPopup")):
                return self.result(ApplyStatus.SUBMITTED, "application submitted", "submitted")
            errs = error_texts(self.page)
            if errs:
                return self.result(ApplyStatus.NEEDS_REVIEW, "submit errors: " + "; ".join(errs)[:400], "submit")
        return self.result(ApplyStatus.NEEDS_REVIEW, "submit clicked but no confirmation seen", "submit")
