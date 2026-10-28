"""Fallback adapter: a Gemini vision loop that drives career sites without a dedicated adapter
(iCIMS, SmartRecruiters, custom pages). The registry routes here last."""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import zip_longest
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import Frame, Page, TimeoutError as PlaywrightTimeoutError

from .. import answers, captcha, form, llm, mail, vault
from ..browser import human_pause
from ..config import SCREENSHOT_DIR
from ..models import Answer, ApplyResult, ApplyStatus, Field, Job
from .base import ApplyContext, blocked_result, submit_decision

log = logging.getLogger(__name__)

MAX_STEPS = 25
BUDGET_SECONDS = 360
LOOP_REPEATS = 3
MAX_CLICKABLES = 80
POPUP_WAIT_MS = 8000

ACTIONS = ["fill_form", "click", "create_account", "sign_in", "need_email_code", "need_email_link",
           "submit", "done", "give_up"]

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "click_index": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
}

SUCCESS_RE = re.compile(
    r"thank you for (applying|your application|your interest in)|thanks for applying|application (has been |was )?"
    r"(received|submitted)|(successfully|been) submitted|submitted successfully|we('ve| have) received your application",
    re.I)
SUBMIT_TEXT_RE = re.compile(r"\bsubmit\b|send application|finish application|complete application", re.I)
# On an application form these words mean "send it" ("Apply now", "Send my application"); on a posting page
# "Apply" just opens the form, so they only count once application fields are present.
FINAL_WORD_RE = re.compile(r"\b(apply|send)\b|^\s*(finish|complete|confirm|done)\s*$", re.I)
APPLY_NAV_RE = re.compile(r"apply(?:\s+(?:now|for this job(?: online)?|manually|without (?:an? )?account))?"
                          r"|start (?:my |your |an? )?application|continue as guest", re.I)
# Navigation and account labels used when classifying non-submit controls.
NAV_TEXT_RE = re.compile(r"^\s*(next|continue|save( (and|&) continue)?|back|previous|search|upload|attach|add( another)?"
                         r"|sign in|log ?in|create account|register|sign up|verify|send (a )?code|resend)\b", re.I)
ADVANCE_TEXT_RE = re.compile(r"^\s*(next|continue)\b", re.I)
REVIEW_PATH_RE = re.compile(r"/(?:review|review[-_]application|application[-_]review|review[-_]and[-_]submit)(?:/|$)", re.I)
REVIEW_TEXT_RE = re.compile(r"\b(?:review (?:your |my |the )?application|application review|review (?:and|&) submit)\b", re.I)
REVIEW_BACK_RE = re.compile(r"^\s*(?:back|previous|edit)\b", re.I)
REVIEW_ACTION_RE = re.compile(r"\b(?:next|continue|submit|apply|confirm|save|finish|complete|done|send)\b", re.I)
REVIEW_SUBMIT_HREF_RE = re.compile(r"(?:^|[/_?&=.#-])(?:submit|apply|send|finish|complete|confirm)(?:$|[/_?&=.#-])", re.I)
APP_FIELD_RE = re.compile(r"\bname\b|e-?mail|phone|resume|\bcv\b|linkedin", re.I)
FORBIDDEN_TEXT_RE = re.compile(r"\b(pay|payment|purchase|checkout|buy now|subscribe)\b", re.I)
RESUME_LABEL_RE = re.compile(r"resume|résumé|\bcv\b|curriculum", re.I)
_RESUME_UPLOAD_RE = re.compile(r"^(?:(?:resume|cv)\s+)?(?:upload|attach|choose file|browse)"
                               r"(?:\s+(?:resume|cv|file))?$", re.I)
_PRIOR_RESUME_RE = re.compile(r"previous|prior|existing|uploaded|select|choose", re.I)
_EMPLOYMENT_HISTORY_RE = re.compile(r"ever (?:been )?(?:employed|worked)|previously (?:been )?employed"
                                    r"|current (?:or|and) former employee|former employee", re.I)
_DECLINE_RE = re.compile(r"no(?:\b.*)?|false|decline(?:\b.*)?|opt out|unsubscribe"
                          r"|i (?:do not|don't) (?:agree|consent|wish|want)(?:\b.*)?", re.I)
_MESSAGING_CHANNEL_RE = re.compile(r"\bsms\b|text messages?|whats\s*app|e-?mail|news|talent communit", re.I)
_MESSAGING_INTENT_RE = re.compile(r"send|receiv|contact|opt.?in|subscrib|communicat|updates|consent", re.I)
_FUTURE_PERMISSION_RE = re.compile(r"future.?opportunit|future.?consideration|other positions|additional job", re.I)

_RESUME_UPLOAD_CONTEXT_JS = r"""
el => {
  const clean = x => (x || '').replace(/\s+/g, ' ').replace(/[\s*✱:]+$/g, '').trim();
  const visible = x => {
    const r = x.getBoundingClientRect(), s = getComputedStyle(x);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility === 'visible';
  };
  const section = /^(?:upload\s+)?(resume|résumé|resume\s*\/\s*cv|cv|curriculum vitae)$/i;
  const other = /^(cover letter|upload additional files|additional files|attachments)$/i;
  const labels = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,label,legend,strong,span,div,p')]
    .filter(x => visible(x) && (section.test(clean(x.textContent)) || other.test(clean(x.textContent)))
      && ![...x.children].some(c => section.test(clean(c.textContent)) || other.test(clean(c.textContent))));
  for (let parent = el.parentElement, depth = 0; parent && depth < 5; parent = parent.parentElement, depth++) {
    const inside = labels.filter(x => parent.contains(x));
    if (inside.length && inside.every(x => section.test(clean(x.textContent)))) return true;
    if (inside.some(x => other.test(clean(x.textContent)))) break;
  }
  const r = el.getBoundingClientRect();
  const before = labels.filter(x => {
    const b = x.getBoundingClientRect();
    return b.top <= r.bottom && b.bottom >= r.top - 280
      && Math.abs(b.left - r.left) < 550;
  }).sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
  return before.length > 0 && section.test(clean(before[0].textContent));
}
"""

_RESUME_FILE_MENU_JS = r"""
rect => {
  const visible = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility === 'visible';
  };
  const text = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  const items = [...document.querySelectorAll('button,a,[role=menuitem],[role=option],li,div,span')]
    .filter(e => visible(e) && text(e) === 'File'
      && ![...e.children].some(c => visible(c) && text(c) === 'File'))
    .map(e => e.closest('button,a,[role=menuitem],[role=option],li') || e);
  const ranked = [...new Set(items)].map(e => {
    const r = e.getBoundingClientRect();
    return {e, d: Math.abs(r.left - rect.x) + Math.abs(r.top - (rect.y + rect.height))};
  }).filter(x => x.d < 420).sort((a, b) => a.d - b.d);
  if (ranked.length !== 1 && (!ranked.length || ranked[1].d - ranked[0].d < 40)) return false;
  ranked[0].e.setAttribute('data-aa-resume-file-menu', 'true');
  return true;
}
"""

SUBMIT_TEXTS = ["Submit application", "Submit Application", "Submit", "Send application", "Finish", "Complete"]
def _verification_filters(url: str, company: str, email: str | None) -> tuple[str | None, str | None]:
    hostname = (urlparse(url).hostname or "").lower()
    host = hostname.split(".")
    hint = host[-2] if len(host) >= 2 else None
    if hostname == "oraclecloud.com" or hostname.endswith(".oraclecloud.com"):
        hint = "oracle"
    successfactors = any(hostname == domain or hostname.endswith("." + domain)
                         for domain in ("successfactors.com", "successfactors.eu"))
    if successfactors and company.strip().lower() == "celestica":
        hint = "celestica"
        if email and "@" in email:
            local, domain = email.rsplit("@", 1)
            if domain.lower() in ("gmail.com", "googlemail.com"):
                email = local.split("+", 1)[0] + "@" + domain
    return hint, email
ACCOUNT_ACTION_RE = re.compile(r"^\s*(?:create\s+(?:an?\s+)?account|sign\s+up|register|sign\s+in|log\s*in|next|continue)\s*$", re.I)

_CLICKABLES_JS = r"""
(start) => {
  const sel = 'a[href], button, ukg-button, [role=button], [role=link], [role=tab], [role=menuitem], [role=option], [role=combobox], ' +
              'input[type=submit], input[type=button], input[type=image], [onclick], summary, label';
  const out = [];
  let i = start;
  for (const el of document.querySelectorAll('[data-aa-click]')) el.removeAttribute('data-aa-click');
  const priority = el => {
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (/^(apply(?:\b|$)|submit\b|next\b|continue\b|review\b|upload\b|attach\b)/i.test(label)) return 0;
    if (el.closest('form,[role=dialog]') || el.matches('[role=option],[role=combobox]')) return 1;
    return el.matches('a[href]') ? 3 : 2;
  };
  for (const el of [...document.querySelectorAll(sel)].sort((a, b) => priority(a) - priority(b))) {
    if (out.length >= %d) break;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (el.closest('[hidden],[inert],[aria-hidden=true]') || !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    const text = (el.innerText || el.value || el.getAttribute('title') || el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim();
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (!text && !aria) continue;
    el.setAttribute('data-aa-click', String(i));
    out.push({index: i, tag: el.tagName.toLowerCase(), text: text.slice(0, 80), aria: aria.slice(0, 80),
              href: (el.getAttribute('href') || '').slice(0, 120),
              in_view: r.top < window.innerHeight && r.bottom > 0,
              is_submit: (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit') === 'submit' && !!el.form)
                         || (el.tagName === 'INPUT' && ['submit', 'image'].includes(el.type)),
              role: el.getAttribute('role') || el.tagName.toLowerCase()});
    i++;
  }
  return out;
}
""" % MAX_CLICKABLES

_EMAIL_SEL = ("input[type=email], input[autocomplete=email], input[autocomplete=username], "
              "input[name*=email i], input[id*=email i], input[name*=user i], input[id*=login i]")
_CODE_SEL = ("input[autocomplete=one-time-code], input[name*=code i], input[id*=code i], input[name*=otp i], "
             "input[id*=otp i], input[placeholder*=code i], input[name*=verif i], input[id*=verif i], input[name*=pin i]")
_OTP_SEL = ("input[maxlength='1'], " + _CODE_SEL + ", input[aria-label*=code i], "
            "input[aria-label*=otp i], input[aria-label*=passcode i]")
_VERIFY_BUTTON_RE = re.compile(r"^\s*(?:verify(?:\s+(?:code|email|identity|otp|passcode))?|"
                               r"confirm(?:\s+(?:code|email|identity|otp|passcode))?|"
                               r"validate(?:\s+(?:code|otp|passcode))?|next|continue|submit)\s*$", re.I)
_REQUEST_CODE_RE = re.compile(r"^\s*(?:re[ -]?send|send)(?: (?:me|a|another|new|verification|security|email))*"
                               r" (?:code|otp|passcode)\s*$", re.I)
_CODE_PROMPT_RE = re.compile(r"verification code|security code|one[ -]time code|passcode|"
                              r"check your e-?mail|confirm your identity|enter (?:the |your |a |\d+[ -]digit )?code", re.I)


@dataclass
class Clickable:
    index: int
    frame: Frame
    tag: str
    text: str
    aria: str
    href: str
    in_view: bool
    is_submit: bool = False
    role: str = ""

    def __post_init__(self):
        if not self.role:
            self.role = self.tag

    def describe(self) -> str:
        bits = [f"[{self.index}] <{self.tag}> {self.text or self.aria}"]
        if self.aria and self.aria != self.text:
            bits.append(f"(aria: {self.aria})")
        if self.href and not self.href.startswith("javascript"):
            bits.append(f"-> {_redacted_url(self.href)}")
        if not self.in_view:
            bits.append("(below fold)")
        if self.frame.parent_frame is not None:
            bits.append(f"(embedded frame: {urlparse(self.frame.url).path[:100]})")
        return " ".join(bits)


@dataclass
class State:
    url: str
    title: str
    screenshot: bytes | None
    scopes: list[tuple[Page | Frame, list[Field]]]
    clickables: list[Clickable]
    text: str

    @property
    def fields(self) -> list[Field]:
        return [f for _, fs in self.scopes for f in fs]

    def signature(self) -> tuple:
        fs = tuple((f.label, f.kind, f.current_value) for f in self.fields)
        clicks = tuple((c.text, c.aria, c.role, c.href) for c in self.clickables)
        documents = tuple(scope.url for scope, _ in self.scopes)
        return (self.url, documents, fs, clicks, self.text)


@dataclass
class Run:
    job: Job
    ctx: ApplyContext
    page: Page
    deadline: float
    job_text: str = ""
    submitted: bool = False
    confirmation_baseline: Counter[str] = field(default_factory=Counter)
    history: list[str] = field(default_factory=list)
    filled_fields: dict[str, Field] = field(default_factory=dict)
    filled_answers: dict[str, Answer] = field(default_factory=dict)
    application_field_keys: set[str] = field(default_factory=set)
    failed_fields: set[str] = field(default_factory=set)
    retried_fields: set[str] = field(default_factory=set)
    account: "vault.Account | None" = None
    account_ts: float = 0.0
    started_ts: float = field(default_factory=time.time)
    resume_uploaded: bool = False
    otp_requested_ts: float = 0.0
    pending_otp_request_ts: float = 0.0
    otp_entered_request_ts: float = 0.0
    popup_source: Page | None = None
    popup_baseline: list[Page] = field(default_factory=list)
    popup_started_ts: float = 0.0


class GenericAdapter:
    name = "generic"

    def can_handle(self, job: Job) -> bool:
        return True

    def apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        run = Run(job=job, ctx=ctx, page=ctx.page, deadline=time.time() + BUDGET_SECONDS)
        try:
            result = self._run(run)
        except Exception as e:  # never let a page problem crash the runner
            log.exception("generic adapter crashed on %s", job.url)
            result = ApplyResult(ApplyStatus.FAILED, f"generic adapter error: {type(e).__name__}: {e}"[:500])
        result.screenshot = _screenshot(run.page, job, result.status.value) or result.screenshot
        result.final_url = _safe_url(run.page) or result.final_url
        return result

    def _run(self, run: Run) -> ApplyResult:
        if not _llm_ok():
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "LLM unavailable")
        page = run.page
        try:
            page.goto(run.job.url, wait_until="domcontentloaded")
        except Exception as e:
            return ApplyResult(ApplyStatus.FAILED, f"could not open job page: {type(e).__name__}: {e}"[:300])
        _settle(page)
        try:
            run.job_text = form.page_text(page)
        except Exception:
            run.job_text = ""

        seen: Counter = Counter()
        bad_replies = 0
        last_sig = None
        consecutive_stuck = 0
        for step in range(1, MAX_STEPS + 1):
            if time.time() > run.deadline:
                return self._out_of_budget(run, f"time budget ({BUDGET_SECONDS}s) exhausted after {step - 1} steps")
            state = _collect(run)
            if _new_confirmation(run, state.text):
                return self._success(run, "confirmation text seen")
            sig = state.signature()
            seen[sig] += 1
            if sig == last_sig:
                consecutive_stuck += 1
            else:
                consecutive_stuck = 1
                last_sig = sig
            if consecutive_stuck >= LOOP_REPEATS or seen[sig] >= 5:
                return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                    f"stuck: same page state {LOOP_REPEATS} times at {_redacted_url(state.url)}")

            try:
                reply = llm.generate_json(_prompt(run, state, step), ACTION_SCHEMA,
                                          images=[state.screenshot] if state.screenshot else None,
                                          model=llm.vision_model())
            except Exception as e:  # LLMUnavailable, missing key, client errors
                log.warning("vision LLM failed: %s", e)
                kind = "LLM unavailable" if isinstance(e, llm.LLMUnavailable) else "LLM error"
                return self._result(run, ApplyStatus.NEEDS_REVIEW, f"{kind}: {e}"[:300])

            action = reply.get("action") if isinstance(reply, dict) else None
            reason = str(reply.get("reason", "")) if isinstance(reply, dict) else ""
            if action not in ACTIONS:
                bad_replies += 1
                run.history.append(f"step {step}: invalid reply {str(reply)[:80]}")
                if bad_replies >= 2:
                    return self._result(run, ApplyStatus.NEEDS_REVIEW, "LLM returned invalid actions")
                continue
            log.info("generic step %d: %s (%s)", step, action, reason[:120])
            run.history.append(f"step {step}: {action}"
                               + (f" #{reply.get('click_index')}" if action == "click" else "") + f" - {reason[:100]}")

            out = self._execute(run, state, action, reply.get("click_index"), reason)
            if out is not None:
                return out
            human_pause(0.4, 1.2)
            _settle(run.page)

        return self._out_of_budget(run, f"step cap ({MAX_STEPS}) reached")

    def _execute(self, run: Run, state: State, action: str, click_index, reason: str) -> ApplyResult | None:
        if _adopt_popup(run):
            return None
        if action == "fill_form":
            if not state.fields:
                run.history.append("  (no fields found to fill)")
            else:
                self._fill(run, state)
            return None
        if action in ("click", "submit"):
            target = _find_clickable(state, click_index)
            if target is not None and not run.submitted and _ambiguous_account_target(state, target):
                return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                    "account action may submit an application; manual review required")
            if action == "submit":
                if target is not None and _posting_apply_navigation(run, state, target):
                    _click(run, target)
                    return None
                return self._submit(run, state, target, reason)
            if target is None:
                run.history.append(f"  (no clickable with index {click_index})")
                return None
            label = f"{target.text} {target.aria}".strip()
            if FORBIDDEN_TEXT_RE.search(label):
                run.history.append(f"  (refused to click payment-like element {target.text!r})")
                return None
            if _final_like(run, state, target, label):
                return self._submit(run, state, target, reason)
            if _resume_upload_button(run, target):
                ok, detail = _upload_resume(run, target)
                if not ok:
                    return self._result(run, ApplyStatus.NEEDS_REVIEW, detail)
                return None
            field_key = _target_field(state, target)
            if field_key is not None:
                self._fill(run, state, only_keys={field_key})
                answer = run.filled_answers.get(field_key)
                if (field_key in run.failed_fields or answer is None or answer.source == "unknown"
                        or answer.value in (None, "", [])):
                    return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                        f"field choice needs a verified answer: {run.filled_fields[field_key].label}")
                return None
            if target.role == "option" or re.fullmatch(r"yes|no", (target.text or target.aria).strip(), re.I):
                return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                    "choice has no verified form field; manual review required")
            _click(run, target)
            return None
        if action in ("create_account", "sign_in"):
            return self._account(run, state, action)
        if action in ("need_email_code", "need_email_link"):
            return self._verify(run, state, "code" if action == "need_email_code" else "link")
        if action == "done":
            text = _page_text(run.page)
            if run.submitted:
                if _new_confirmation(run, text):
                    return self._success(run, "confirmation text seen")
                return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                    f"submit clicked but no confirmation text ({reason[:120]})")
            if SUCCESS_RE.search(text):
                return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                    "page shows a confirmation but this run did not submit (applied before?)")
            return self._result(run, ApplyStatus.NEEDS_REVIEW, f"LLM said done without submitting: {reason[:200]}")
        return self._result(run, ApplyStatus.NEEDS_REVIEW, f"LLM gave up: {reason[:250]}")

    def _fill(self, run: Run, state: State, only_keys: set[str] | None = None) -> list[str]:
        """Answer and fill every scope's fields; remember them for the submit decision."""
        failed: list[str] = []
        path = urlparse(state.url).path
        on_application = _application_page_context(run, state)
        for n, (scope, all_fields) in enumerate(state.scopes):
            fields = [f for f in all_fields if only_keys is None or _field_id(path, n, f) in only_keys]
            if not fields:
                continue
            try:
                ans = answers.answer_fields(fields, run.job, run.ctx.profile, job_text=run.job_text)
            except Exception as e:
                log.warning("answer_fields failed: %s", e)
                ans = [Answer(f.key, None, "unknown", note="answer error") for f in fields]
            ans = _guard_choices(fields, ans, run.ctx.profile)
            ans = _guard_files(fields, ans, run.ctx.profile)
            try:
                scope_failed = set(form.fill_fields(scope, fields, ans))
                for f in fields:
                    a = next((a for a in ans if a.key == f.key), None)
                    if (f.kind in ("select", "combobox", "radio", "checkbox") and a
                            and a.source != "unknown" and a.value not in (None, "", [])):
                        if _verified_choice(scope, f, a):
                            scope_failed.discard(f.key)
                        else:
                            scope_failed.add(f.key)
            except Exception as e:
                log.warning("fill_fields failed: %s", e)
                scope_failed = {f.key for f in fields}
            failed.extend(scope_failed)
            by_key = {a.key: a for a in ans}
            for f in fields:
                k = _field_id(path, n, f)
                run.filled_fields[k] = replace(f, key=k)
                a = by_key.get(f.key)
                run.filled_answers[k] = replace(a, key=k) if a else Answer(k, None, "unknown")
                if f.key in scope_failed:
                    run.failed_fields.add(k)
                else:
                    run.failed_fields.discard(k)
                    if on_application:
                        run.application_field_keys.add(k)
                    if f.kind == "file" and a and a.source == "profile" and a.value:
                        run.resume_uploaded = True
        if failed:
            run.history.append(f"  (failed to fill: {', '.join(failed[:8])})")
        return failed

    def _submit(self, run: Run, state: State, target: Clickable | None, reason: str) -> ApplyResult | None:
        if run.submitted:
            if _new_confirmation(run, _page_text(run.page)):
                return self._success(run, "confirmation text seen")
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "submit already clicked this run; confirmation still missing")
        if (target is not None and _apply_now_label(target)
                and not _application_page_context(run, state)):
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "Apply Now may submit without verified application fields; manual review required")
        review_page = _application_review(state)
        if review_page:
            label = f"{target.text} {target.aria}" if target is not None else ""
            if target is None or _review_back_action(target) or not (
                    target.is_submit or SUBMIT_TEXT_RE.search(label) or FINAL_WORD_RE.search(label) or _advance_like(target)):
                return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                    f"ambiguous review action: {target.text or target.aria if target else 'missing target'!r}")
        path = urlparse(state.url).path
        to_fill = {_field_id(path, n, f) for n, (_, fs) in enumerate(state.scopes) for f in fs
                   if _field_id(path, n, f) not in run.filled_fields or _messaging_field(f)}
        if to_fill:
            self._fill(run, state, only_keys=to_fill)
        pending = {k for k in run.failed_fields if run.filled_fields[k].required}
        visible = {_field_id(path, n, f) for n, (_, fs) in enumerate(state.scopes) for f in fs}
        retry = (pending & visible) - run.retried_fields
        if retry:
            run.retried_fields.update(retry)
            self._fill(run, state, only_keys=retry)
        mismatches = []
        for n, (scope, fs) in enumerate(state.scopes):
            for f in fs:
                key = _field_id(path, n, f)
                answer = run.filled_answers.get(key)
                if (f.kind in ("select", "combobox", "radio", "checkbox") and answer
                        and answer.source != "unknown" and answer.value not in (None, "", [])
                        and not _verified_choice(scope, f, answer)):
                    run.failed_fields.add(key)
                    mismatches.append(f.label or f.key)
        if mismatches:
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                f"selected values do not match verified answers: {', '.join(mismatches[:8])}")
        failed_required = [run.filled_fields[k].label or k for k in sorted(run.failed_fields)
                           if k in run.filled_fields and run.filled_fields[k].required]
        if failed_required:
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                f"required fields failed to fill: {', '.join(failed_required[:8])}")
        for key, f in run.filled_fields.items():
            a = run.filled_answers.get(key)
            if (_messaging_field(f) and (key in run.failed_fields or a is None
                                        or a.source == "unknown" or a.value in (None, "", []))):
                return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                    f"could not confirm messaging opt-out: {f.label}")
        fields, ans = list(run.filled_fields.values()), _effective_answers(run)
        if review_page and not _validated_app_answers(run):
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "review page reached without validated applicant answers")
        review_ready = _review_submit_ready(state, target)
        if not fields and not review_ready:
            return self._result(run, ApplyStatus.NEEDS_REVIEW, "no application fields or verified review page")
        ok, why = submit_decision(fields, ans, run.ctx.submit_mode, run.ctx.dry_run)
        if not ok:
            if run.ctx.dry_run and why == "dry run" and review_ready:
                return self._result(run, ApplyStatus.DRY_RUN_OK, "not submitted: dry run")
            return blocked_result(fields, ans, f"not submitted: {why}", run.ctx.dry_run, None, _safe_url(run.page))
        if target is not None and FORBIDDEN_TEXT_RE.search(f"{target.text} {target.aria}"):
            return self._result(run, ApplyStatus.NEEDS_REVIEW, f"final button looks like a payment: {target.text!r}")
        outcome = captcha.solve_if_present(run.page, run.ctx.config, run.job.id)
        if captcha.blocks_submit(outcome, run.ctx.config):
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                f"captcha {outcome.status} ({outcome.kind}) {outcome.detail}".strip()[:300])
        if target is not None and not target.is_submit and SUBMIT_TEXT_RE.search(f"{target.text} {target.aria}") is None \
                and not re.search(r"apply|send|finish|complete|confirm|done|continue|next", f"{target.text} {target.aria}", re.I):
            target = None
        run.confirmation_baseline = _confirmation_counts(_page_text(run.page))
        try:
            run.ctx.mark_submit_attempt(run.job.id)
        except Exception as e:
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                f"could not persist submit intent: {type(e).__name__}: {e}"[:300])
        clicked = _click(run, target) if target is not None else False
        if not clicked and not review_page:
            clicked = any(_click_text(scope, SUBMIT_TEXTS) for scope, _ in state.scopes or [(run.page, [])])
        if not clicked:
            return self._result(run, ApplyStatus.NEEDS_REVIEW, "could not find the submit button")
        run.submitted = True
        _settle(run.page, idle_ms=8000)
        if _new_confirmation(run, _page_text(run.page)):
            return self._success(run, "confirmation text seen")
        return None

    def _account(self, run: Run, state: State, action: str) -> ApplyResult | None:
        if (action == "create_account" and run.ctx.submit_mode == "fill_only"
                and not run.ctx.config.get("accounts", {}).get("create_in_fill_only", False)):
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "account required (not created in fill_only mode)")
        url = run.page.url
        try:
            tenant = vault.tenant_key_for(url)
            acct = vault.get(tenant)
            if acct is None:
                if action == "sign_in":
                    run.history.append("  (no stored account for this site; create one or continue as guest)")
                    return None
                if run.ctx.dry_run and not run.ctx.config.get("accounts", {}).get("create_in_dry_run", False):
                    return self._result(run, ApplyStatus.NEEDS_REVIEW, "account required (not created in dry run)")
                acct = vault.create(tenant, run.job.company, url)
                run.account_ts = time.time() - 30
            elif not run.account_ts:
                run.account_ts = time.time() - 30
        except Exception as e:
            return self._result(run, ApplyStatus.NEEDS_REVIEW, f"account vault error: {e}"[:300])
        run.account = acct
        scope = _scope_with(state, "input[type=password]") or run.page
        if not _fill_first(scope, _EMAIL_SEL, acct.email):
            run.history.append("  (no email/username input found)")
            return None
        pw = scope.locator("input[type=password]")
        try:
            for i in range(pw.count()):
                if pw.nth(i).is_visible():
                    pw.nth(i).fill(acct.password)
        except Exception:
            pass
        others = [(s, [f for f in fs if f.kind != "email" and not re.search(r"e-?mail|password|user ?name", f.label, re.I)])
                  for s, fs in state.scopes]
        if any(fs for _, fs in others):
            self._fill(run, State(state.url, state.title, None, others, [], ""))
        return self._result(run, ApplyStatus.NEEDS_REVIEW,
                            "account form filled; action may submit an application, manual review required")

    def _verify(self, run: Run, state: State, want: str) -> ApplyResult | None:
        if want != "code":
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "email verification link required; manual review required")
        scope = _code_scope(run.page)
        if scope is None:
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "email verification code field not found")
        request_ts = run.otp_requested_ts or run.started_ts
        if run.otp_entered_request_ts == request_ts:
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "email verification did not advance; request a fresh code before retrying")
        try:
            if not mail.available():
                return self._result(run, ApplyStatus.NEEDS_REVIEW, "Gmail verification unavailable")
            email = (run.account.email if run.account
                     else run.ctx.profile.get("personal", {}).get("email"))
            hint, recipient = _verification_filters(state.url, run.job.company, email)
            timeout = min(float(run.ctx.config.get("email", {}).get("verification_timeout_seconds", 60)),
                          max(0.0, run.deadline - time.time()))
            if timeout <= 0:
                return self._out_of_budget(run, "time budget exhausted before email verification")
            verification = mail.wait_for_verification(
                request_ts, sender_hint=hint, to_alias=recipient, timeout=timeout,
                want="code", consume=False)
        except Exception as exc:
            log.warning("generic email verification unavailable: %s", type(exc).__name__)
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                f"email verification unavailable: {type(exc).__name__}")
        if verification is None:
            return self._result(run, ApplyStatus.NEEDS_REVIEW, "no verification code email arrived")
        code = verification.code or ""
        if not re.fullmatch(r"[A-Za-z0-9]{4,8}", code) or not _enter_code(scope, code):
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "verification email found but complete code entry could not be verified; message not consumed")
        state = _collect(run)
        if not _code_complete(scope, code):
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                "verification code entry changed before confirmation; message not consumed")
        try:
            mail.acknowledge_verification(verification, want="code")
        except Exception as exc:
            return self._result(run, ApplyStatus.NEEDS_REVIEW,
                                f"could not acknowledge verification email: {type(exc).__name__}")
        run.otp_entered_request_ts = request_ts
        run.history.append("  (complete email verification code entry confirmed; message acknowledged)")
        # This button only confirms the OTP; the final application submission remains gated.
        for target in state.clickables:
            if (target.frame == (scope.main_frame if isinstance(scope, Page) else scope)
                    and _verification_button_safe(target) and any(
                    _VERIFY_BUTTON_RE.fullmatch(label.strip())
                    for label in (target.text, target.aria) if label.strip())):
                _click(run, target)
                run.history.append("  (clicked verification code confirmation)")
                break
        return None

    def _success(self, run: Run, reason: str) -> ApplyResult:
        return self._result(run, ApplyStatus.SUBMITTED, reason)

    def _out_of_budget(self, run: Run, reason: str) -> ApplyResult:
        if run.submitted:
            return self._result(run, ApplyStatus.NEEDS_REVIEW, f"submit clicked but unconfirmed; {reason}")
        return self._result(run, ApplyStatus.NEEDS_REVIEW, reason)

    def _result(self, run: Run, status: ApplyStatus, reason: str) -> ApplyResult:
        fields, ans = list(run.filled_fields.values()), _effective_answers(run)
        by_key = {a.key: a for a in ans}
        missing = lambda a: a is None or a.source == "unknown" or a.value in (None, "", [])
        return ApplyResult(
            status=status, reason=reason, final_url=_safe_url(run.page), fields_total=len(fields),
            unanswered_required=[f.label or f.key for f in fields if f.required and missing(by_key.get(f.key))],
            generated_answers=[f.label or f.key for f in fields if (a := by_key.get(f.key)) and a.source == "generated"],
            answers=ans,
        )


def _llm_ok() -> bool:
    try:
        return bool(llm.available())
    except Exception:
        return False


def _field_id(path: str, scope_index: int, field: Field) -> str:
    return f"{path}#{scope_index}#{field.key}#{field.name}#{field.label}"


def _effective_answers(run: Run) -> list[Answer]:
    return [replace(a, value=None, source="unknown", note="failed to fill")
            if k in run.failed_fields else a for k, a in run.filled_answers.items()]


def _confirmation_counts(text: str) -> Counter[str]:
    return Counter(" ".join(m.group(0).lower().split()) for m in SUCCESS_RE.finditer(text))


def _new_confirmation(run: Run, text: str) -> bool:
    return run.submitted and any(
        count > run.confirmation_baseline[phrase]
        for phrase, count in _confirmation_counts(text).items()
    )


def _collect(run: Run) -> State:
    _adopt_popup(run)
    page = run.page
    try:
        shot = page.screenshot(type="png", full_page=False, timeout=15000, mask=_code_masks(page))
    except Exception:
        shot = None
    scopes: list[tuple[Page | Frame, list[Field]]] = []
    try:
        main = _scope_fields(page)
    except Exception as e:
        log.debug("extract_fields failed on main frame: %s", e)
        main = []
    scopes.append((page, main))
    visible_frames: list[Frame] = []
    for fr in page.frames[1:]:
        if (fr.is_detached() or _frame_definitely_hidden(fr)
                or re.search(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare", fr.url or "")):
            continue
        visible_frames.append(fr)
        try:
            if fr.locator("form, input:not([type=hidden]), select, textarea, [role=combobox], [role=radiogroup]").count() == 0:
                continue
            fs = _scope_fields(fr)
        except Exception:
            continue
        scopes.append((fr, fs))
    groups: list[list[Clickable]] = []
    offset = 0
    for scope in [page.main_frame] + visible_frames:
        try:
            group = [Clickable(c["index"], scope, c["tag"], c["text"], c["aria"], c["href"], c["in_view"],
                               bool(c.get("is_submit")), role=c.get("role") or c["tag"])
                     for c in scope.evaluate(_CLICKABLES_JS, offset)]
            groups.append(group)
            offset += len(group)
        except Exception:
            continue
    # Site branding must not use the entire budget before an embedded application gets a turn.
    clickables = [c for row in zip_longest(*groups) for c in row if c is not None][:MAX_CLICKABLES]
    try:
        title = page.title()
    except Exception:
        title = ""
    state = State(page.url, title, shot, scopes, clickables, _page_text(page))
    if run.pending_otp_request_ts and _CODE_PROMPT_RE.search(state.text) and _code_scope(page) is not None:
        run.otp_requested_ts = run.pending_otp_request_ts
        run.pending_otp_request_ts = 0.0
    return state


def _scope_fields(scope: Page | Frame) -> list[Field]:
    fields = _active_fields(scope, form.extract_fields(scope))
    code_keys = {loc.get_attribute("data-aa-key") for loc in _code_controls(scope)}
    fields = [f for f in fields if f.key not in code_keys]
    for f in fields:
        try:
            loc = scope.locator(f'[data-aa-key="{f.key}"]').first
            if f.kind == "file" and not RESUME_LABEL_RE.search(f"{f.label} {f.name}"):
                if loc.get_attribute("data-aa-resume-upload") == "true" or loc.evaluate(_RESUME_UPLOAD_CONTEXT_JS):
                    f.label = "Resume"
            elif f.kind == "combobox":
                value = _combo_value(loc)
                if value is not None:
                    f.current_value = value
        except Exception:
            continue
    return fields


def _combo_value(loc) -> str | None:
    return loc.evaluate(r"""el => {
      if (el.matches('input,select,textarea')) return null;
      const copy = el.cloneNode(true);
      for (const menu of copy.querySelectorAll('[role=listbox],[role=option]')) menu.remove();
      const text = (copy.textContent || '').replace(/\s+/g, ' ').trim();
      return /^(?:please\s+)?(?:select|choose)(?:\b|\.{3})/i.test(text) ? '' : text;
    }""")


def _verified_combo(scope: Page | Frame, f: Field, answer: Answer | None) -> bool:
    if f.kind != "combobox" or answer is None or answer.source == "unknown" or not answer.value:
        return False
    try:
        loc = scope.locator(f'[data-aa-key="{f.key}"]').first
        if loc.get_attribute("aria-expanded") == "true":
            return False
        value = _combo_value(loc)
        expected = str(answer.value).strip()
        if value and value.casefold() == expected.casefold():
            return True
        # Some country selectors display only the calling code after a country was selected.
        code = re.search(r"\((\+\d{1,4})\)$", expected)
        return bool(code and re.search(r"country.*code|phone.*country", f.label, re.I)
                    and value == code.group(1) and expected in f.options)
    except Exception:
        return False


def _verified_choice(scope: Page | Frame, f: Field, answer: Answer) -> bool:
    try:
        if f.kind in ("checkbox", "radio"):
            got = scope.evaluate(form._READ_JS, [f.key, f.kind])
            if f.kind == "checkbox" and not f.options:
                expected = answer.value is True or str(answer.value).strip().lower() in ("true", "yes", "1", "checked")
                return got.get("checked") is expected
            selected = [text for text, checked in got.get("states", []) if checked]
            wanted = answer.value if isinstance(answer.value, list) else [str(answer.value)]
            return len(selected) == len(wanted) and all(
                form.option_matches(value, shown) for value, shown in zip(sorted(wanted), sorted(selected)))
        verify = getattr(form, "_verify", None)
        if verify and verify(scope, f, str(answer.value)):
            return True
        if f.kind == "combobox":
            return _verified_combo(scope, f, answer)
        selected = scope.locator(f'[data-aa-key="{f.key}"] option:checked').all_text_contents()
        expected = " ".join(str(answer.value).split()).casefold()
        return len(selected) == 1 and " ".join(selected[0].split()).casefold() == expected
    except Exception:
        return False


def _frame_definitely_hidden(frame: Frame) -> bool:
    """A child document's layout can be visible even if an embedding frame is hidden."""
    current = frame
    while current.parent_frame is not None:
        try:
            if not current.frame_element().evaluate("""el =>
                !el.closest('[hidden],[inert],[aria-hidden=true]') &&
                el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})"""):
                return True
            current = current.parent_frame
        except Exception:
            # Unknown visibility must not silently remove potentially required fields.
            return False
    return False


def _active_fields(scope: Page | Frame, fields: list[Field]) -> list[Field]:
    """Exclude inactive wizard controls that still have nonzero layout bounds."""
    if not fields:
        return fields
    try:
        visible = set(scope.evaluate("""fields => fields.filter(f => {
          const el = document.querySelector('[data-aa-key="' + CSS.escape(f.key) + '"]');
          if (!el || !el.isConnected) return false;
          const proxy = f.kind === 'checkbox' && el.matches('input[type=checkbox]')
            ? [...(el.labels || [])].find(label => {
                const r = label.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && label.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
              }) : null;
          // File inputs are commonly hidden behind a visible upload control.
          if (f.kind !== 'file' && !proxy && !el.checkVisibility({checkVisibilityCSS: true})) return false;
          for (let node = proxy || (f.kind === 'file' ? el.parentElement : el); node; node = node.parentElement) {
            if (node.hidden || node.hasAttribute('inert') || node.getAttribute('aria-hidden') === 'true') return false;
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible' || style.contentVisibility === 'hidden') return false;
            if (style.opacity === '0' && (node !== el || f.kind !== 'combobox')) return false;
          }
          return true;
        }).map(f => f.key)""", [{"key": f.key, "kind": f.kind} for f in fields]))
    except Exception as e:
        log.debug("active-field visibility check failed: %s", e)
        return fields
    return [f for f in fields if f.key in visible]


def _prompt(run: Run, state: State, step: int) -> str:
    job = run.job
    fields = "\n".join(
        f"- {f.label or f.name or f.key} [{f.kind}{', required' if f.required else ''}]"
        + (f" = {f.current_value[:40]!r}" if f.current_value else " (empty)")
        + (f" options: {', '.join(f.options[:8])}" if f.options else "")
        for f in state.fields[:60]) or "(none)"
    clicks = "\n".join(c.describe() for c in state.clickables) or "(none)"
    history = "\n".join(run.history[-12:]) or "(none yet)"
    return f"""You are operating a web browser to submit an internship application for this exact job:
company: {job.company}
title: {job.title}
posting: {job.url}

Choose ONE next action. Actions:
- fill_form: fill every visible form field on the page with the applicant's information (done for you).
- click: click the numbered element given in click_index (navigation, "Apply", "Next", "Continue", tabs, cookie banners).
- create_account: the site requires a new account; the email/password form is visible now.
- sign_in: a login form is visible and we may already have an account for this site.
- need_email_code: the page asks for a code that was emailed.
- need_email_link: the page says to click a link sent by email.
- submit: the application form is complete and the final submit button is visible (give its click_index).
- done: the application was submitted and a confirmation is shown.
- give_up: the posting is closed, requires payment, the wrong job, or cannot be completed.

Rules:
- Prefer "Apply", "Apply manually", "Apply now", "Continue as guest", or "Apply without account" over importing from
  LinkedIn, Indeed, Google, or other SSO / "Apply with" buttons. Never use social sign-in.
- Never pay or enter payment details. Never upload anything except the resume (handled automatically).
- Never invent applicant facts or choose dropdown/radio answers yourself; use fill_form for form controls.
- Decline SMS, email updates, WhatsApp and future-opportunity messaging. Never opt into messages.
- Use "submit" (never "click") for the final submit / send application button. Fill the form first.
- If fields are visible and empty, use fill_form before clicking Next/Continue/Submit.
- Close cookie banners or popups only if they block the page.
- If the same action did not change the page, try something different or give_up.

Current state (step {step} of {MAX_STEPS}):
url: {_redacted_url(state.url)}
title: {state.title}
form fields:
{fields}
clickable elements:
{clicks}
visible text (truncated):
{state.text[:2500]}

Previous actions:
{history}

Reply with JSON: {{"action": ..., "click_index": <int, for click/submit>, "reason": <short>}}."""


def _has_app_form(run: Run, state: State) -> bool:
    """True once we're on an actual application (not a posting page with a search or newsletter box)."""
    fs = state.fields
    applicant_fields = sum(1 for f in fs if f.kind != "file"
                           and (f.kind in ("email", "tel") or APP_FIELD_RE.search(f.label)))
    if applicant_fields >= 2 or (applicant_fields >= 1 and any(f.kind == "file" for f in fs)):
        return True
    # Later wizard steps (questions, review) have no contact fields; a talent-community box is 1-2 fields.
    return bool(run.application_field_keys)


def _review_submit_ready(state: State, target: Clickable | None) -> bool:
    """Recognize a completed review step that has no editable fields."""
    path = urlparse(state.url).path.rstrip("/").lower().rsplit("/", 1)[-1]
    if path not in ("review", "review-application", "application-review"):
        return False
    if not re.search(r"review (your |my |the )?application|application review", state.text, re.I):
        return False
    if not re.search(r"\b(first name|last name|full name|e-?mail|phone|resume)\s*:", state.text, re.I):
        return False
    buttons = [target] if target is not None else state.clickables
    return any(c is not None and (
        (c.is_submit and SUBMIT_TEXT_RE.search(f"{c.text} {c.aria}"))
        or _advance_like(c)
        or SUBMIT_TEXT_RE.search(f"{c.text} {c.aria}")
        or FINAL_WORD_RE.search(f"{c.text} {c.aria}")
    ) for c in buttons)


def _advance_like(target: Clickable) -> bool:
    return any(ADVANCE_TEXT_RE.search(value) for value in (target.text, target.aria))


def _application_review(state: State) -> bool:
    """Review steps can have no editable fields or form-associated buttons."""
    path = urlparse(state.url).path
    if REVIEW_PATH_RE.search(path) or REVIEW_TEXT_RE.search(state.title) or REVIEW_TEXT_RE.match(state.text):
        return True
    return bool(REVIEW_TEXT_RE.search(state.text[:600]) and re.search(
        r"(?:^|/)(?:apply|application|applications)(?:[-/]|$)", path, re.I))


def _review_back_action(target: Clickable) -> bool:
    label = f"{target.text} {target.aria}".strip()
    return bool(REVIEW_BACK_RE.match(target.text or target.aria)
                and not REVIEW_ACTION_RE.search(label))


def _review_action_button(state: State, target: Clickable) -> bool:
    if not _application_review(state) or _review_back_action(target):
        return False
    if target.tag == "a" and target.href and not target.href.startswith(("#", "javascript")):
        label = f"{target.text} {target.aria}"
        return bool(REVIEW_ACTION_RE.search(label) or REVIEW_SUBMIT_HREF_RE.search(target.href))
    return True


def _validated_app_answers(run: Run) -> bool:
    for key, field in run.filled_fields.items():
        answer = run.filled_answers.get(key)
        if (key in run.application_field_keys and key not in run.failed_fields and answer is not None
                and answer.source in ("profile", "bank")
                and isinstance(answer.value, str) and answer.value.strip()
                and (field.kind in ("email", "tel", "file")
                     or APP_FIELD_RE.search(f"{field.label} {field.name}"))):
            return True
    return False


def _application_page_context(run: Run, state: State) -> bool:
    if _has_app_form(run, state) or _application_review(state):
        return True
    if not state.fields:
        return False
    path = urlparse(state.url).path
    return bool(re.search(r"(?:^|/)(?:apply|application)(?:-[^/]*)?(?:/|$)", path, re.I)
                or re.search(r"\bapplication\b", state.title, re.I)
                or re.search(r"^\s*(?:job )?application\b", state.text, re.I))


def _application_context(run: Run, state: State) -> bool:
    return bool(run.filled_fields) or _application_page_context(run, state)


def _ambiguous_account_target(state: State, target: Clickable) -> bool:
    """Account actions alongside passwords can also submit an application if applicant fields are present."""
    labels = [s.strip() for s in (target.text, target.aria) if s.strip()]
    if not labels or not any(ACCOUNT_ACTION_RE.fullmatch(s) for s in labels) or _application_review(state):
        return False
    # Only ambiguous if the form also contains applicant profile/application fields (combined form)
    applicant_fields = [f for f in state.fields if f.kind != "password"
                        and not re.search(r"e-?mail|password|user ?name", f.label or f.name, re.I)]
    if not applicant_fields:
        return False
    try:
        return bool(target.frame.evaluate("""() => {
          return [...document.querySelectorAll('input[type=password]')].some(input => {
            const style = getComputedStyle(input), rect = input.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          });
        }"""))
    except Exception:
        return False


def _final_like(run: Run, state: State, target: Clickable, label: str) -> bool:
    """Whether clicking `target` could send the application (then it must go through submit_decision)."""
    if _posting_apply_navigation(run, state, target):
        return False
    if _advance_like(target):
        return True
    is_link = target.tag == "a" and bool(target.href) and not target.href.startswith(("#", "javascript"))
    app_form = _application_page_context(run, state)
    if target.is_submit:
        return True
    if _review_action_button(state, target):
        return True
    if SUBMIT_TEXT_RE.search(label):
        # "Submit your resume" links on a posting page only navigate; submit buttons are always gated.
        return app_form or not is_link
    if NAV_TEXT_RE.search(target.text or target.aria) or is_link:
        return False
    return (app_form or _application_review(state)) and bool(FINAL_WORD_RE.search(label))


def _apply_now_label(target: Clickable) -> bool:
    labels = [value.strip() for value in (target.text, target.aria) if value.strip()]
    return bool(labels) and all(APPLY_NAV_RE.fullmatch(label) for label in labels)


def _posting_apply_navigation(run: Run, state: State, target: Clickable) -> bool:
    """Apply Now on a posting can open the actual form even beside a resume matching widget."""
    return (not target.is_submit and _apply_now_label(target)
            and not _application_page_context(run, state))


def _find_clickable(state: State, index) -> Clickable | None:
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    return next((c for c in state.clickables if c.index == idx), None)


def _resume_upload_button(run: Run, target: Clickable) -> bool:
    label = (target.text or target.aria).strip()
    if run.resume_uploaded:
        return False
    if target.tag == "label":
        try:
            if not target.frame.locator(f'[data-aa-click="{target.index}"]').evaluate(
                    "el => el.control && el.control.type === 'file'"):
                return False
        except Exception:
            return False
    if re.match(r"^(?:upload|attach)(?: your)? (?:resume|résumé|cv)\b", label, re.I):
        return not re.search(r"cover letter|additional files", label, re.I)
    if not _RESUME_UPLOAD_RE.fullmatch(label):
        return False
    try:
        return bool(target.frame.locator(f'[data-aa-click="{target.index}"]').evaluate(
            _RESUME_UPLOAD_CONTEXT_JS))
    except Exception:
        return False


def _target_field(state: State, target: Clickable) -> str | None:
    try:
        key = target.frame.locator(f'[data-aa-click="{target.index}"]').first.evaluate(r"""el => {
          const owner = node => node && node.closest('[data-aa-key]');
          let field = owner(el);
          if (!field && el.matches('label')) field = owner(el.control || el.querySelector('input,select,textarea'));
          if (!field && el.matches('[role=option],option')) {
            const combos = [...document.querySelectorAll('[data-aa-key][role=combobox]')].filter(combo =>
              (combo.getAttribute('aria-controls') || combo.getAttribute('aria-owns') || '').split(/\s+/)
                .some(id => document.getElementById(id)?.contains(el)));
            if (combos.length === 1) field = combos[0];
          }
          return field && field.getAttribute('data-aa-key');
        }""")
    except Exception:
        return None
    path = urlparse(state.url).path
    for n, (scope, fs) in enumerate(state.scopes):
        if target.frame == (scope.main_frame if isinstance(scope, Page) else scope):
            return next((_field_id(path, n, f) for f in fs if f.key == key), None)
    return None


def _upload_resume(run: Run, target: Clickable) -> tuple[bool, str]:
    path = Path(str(run.ctx.profile.get("resume_path") or "")).expanduser()
    if not path.is_file():
        return False, "resume upload blocked: configured resume file is missing"

    scope = target.frame
    origin = scope.locator(f'[data-aa-click="{target.index}"]').first
    try:
        rect = origin.evaluate("el => { const r = el.getBoundingClientRect(); "
                               "return r.width && r.height ? {x:r.x,y:r.y,width:r.width,height:r.height} : null; }")
    except Exception:
        rect = None
    if rect is None:
        return False, "resume upload blocked: Resume upload control is not visible"

    chooser = None
    try:
        with run.page.expect_file_chooser(timeout=2000) as event:
            if not _click(run, target):
                return False, "resume upload blocked: could not open Resume upload control"
        chooser = event.value
    except PlaywrightTimeoutError:
        try:
            if scope.evaluate(_RESUME_FILE_MENU_JS, rect):
                menu = scope.locator('[data-aa-resume-file-menu="true"]').first
                with run.page.expect_file_chooser(timeout=3500) as event:
                    menu.click(timeout=4000)
                chooser = event.value
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            return False, f"resume upload blocked: File menu interaction failed ({type(e).__name__})"
    except Exception as e:
        return False, f"resume upload blocked: Upload interaction failed ({type(e).__name__})"

    if chooser is not None:
        try:
            chooser.element.evaluate("el => el.setAttribute('data-aa-resume-upload', 'true')")
            chooser.set_files(str(path), timeout=10000)
        except Exception as e:
            return False, f"resume upload blocked: file chooser rejected resume ({type(e).__name__})"
    else:
        try:
            inputs = scope.locator('input[type="file"]')
            matched = [inputs.nth(i) for i in range(inputs.count())
                       if inputs.nth(i).evaluate(_RESUME_UPLOAD_CONTEXT_JS)]
            if len(matched) != 1:
                return False, "resume upload blocked: could not identify a unique Resume file input"
            matched[0].evaluate("el => el.setAttribute('data-aa-resume-upload', 'true')")
            matched[0].set_input_files(str(path), timeout=10000)
        except Exception as e:
            return False, f"resume upload blocked: Resume file input failed ({type(e).__name__})"

    for _ in range(8):
        try:
            attached = scope.evaluate("""filename =>
              [...document.querySelectorAll('input[type=file]')].some(el =>
                [...(el.files || [])].some(f => f.name === filename))
            """, path.name)
            if not attached:
                names = scope.get_by_text(path.name, exact=True)
                attached = any(names.nth(i).is_visible()
                               and not names.nth(i).evaluate("el => !!el.closest('select,option,[role=listbox],[role=option]')")
                               and names.nth(i).evaluate(_RESUME_UPLOAD_CONTEXT_JS)
                               for i in range(min(names.count(), 10)))
            if attached:
                run.resume_uploaded = True
                run.history.append("  (configured resume attached to Resume upload)")
                return True, "resume attached"
        except Exception:
            pass
        try:
            run.page.wait_for_timeout(500)
        except Exception:
            break
    return False, "resume upload needs review: selected file was not confirmed on the application page"


def _adopt_popup(run: Run) -> bool:
    browser, source = run.ctx.browser, run.popup_source
    if browser is None or source is None:
        return False
    try:
        new = [p for p in browser.pages if p not in run.popup_baseline and not p.is_closed()]
        if not new or time.time() - run.popup_started_ts < 0.25:
            return False
        owned = [p for p in new if p.opener() == source]
        related = owned or [p for p in new if p.opener() is None]
        if any(p.url == "about:blank" for p in related) and time.time() - run.popup_started_ts < 0.75:
            return False
        candidates = [p for p in related
                      if urlparse(p.url).scheme in ("https", "http")
                      and urlparse(p.url).path.rstrip("/").rsplit("/", 1)[-1].lower()
                      not in ("null", "undefined")]
        if len(candidates) != 1:
            return False
        run.page = candidates[0]
        run.popup_source = None
        run.popup_baseline = []
        _settle(run.page)
        run.history.append(f"  (click opened a new tab: {_redacted_url(run.page.url)[:100]})")
        return True
    except Exception as e:
        log.debug("could not adopt popup: %s", type(e).__name__)
        return False


def _click(run: Run, target: Clickable) -> bool:
    browser = run.ctx.browser
    before = list(browser.pages) if browser is not None else []
    source, source_url = run.page, _safe_url(run.page)
    loc = target.frame.locator(f'[data-aa-click="{target.index}"]').first
    has_code = _code_scope(run.page) is not None and _CODE_PROMPT_RE.search(_page_text(run.page))
    explicit_request = any(_REQUEST_CODE_RE.fullmatch(label) for label in (target.text, target.aria))
    requesting_code = explicit_request or (not has_code and any(
        ACCOUNT_ACTION_RE.fullmatch(label) for label in (target.text, target.aria)))
    try:
        loc.scroll_into_view_if_needed(timeout=5000)
        requested_ts = time.time()
        loc.click(timeout=10000)
    except Exception:
        try:
            requested_ts = time.time()
            loc.evaluate("el => el.click()")
        except Exception as e:
            run.history.append(f"  (click #{target.index} failed: {type(e).__name__})")
            return False
    if requesting_code:
        if explicit_request:
            run.otp_requested_ts = requested_ts
            run.pending_otp_request_ts = 0.0
        else:
            # Confirm the resulting code screen in _collect before accepting an ambiguous Next.
            run.pending_otp_request_ts = requested_ts
    if browser is not None and run.popup_source != source:
        run.popup_source, run.popup_baseline = source, before
        run.popup_started_ts = requested_ts
    human_pause(0.8, 1.5)
    if browser is not None:
        wait_ms = POPUP_WAIT_MS if _apply_now_label(target) and not target.is_submit else 300
        wait_ms = min(wait_ms, max(0, int((run.deadline - time.time()) * 1000)))
        for attempt in range((wait_ms + 199) // 200 + 1):
            if _adopt_popup(run) or _safe_url(source) != source_url or source.is_closed():
                break
            if attempt * 200 >= wait_ms:
                break
            try:
                source.wait_for_timeout(200)
            except Exception:
                break
    return True


def _click_text(scope: Page | Frame, texts: list[str]) -> bool:
    try:
        return bool(form.click_text(scope, texts, roles=("button",)))
    except Exception:
        return False


def _settle(page: Page, idle_ms: int = 5000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=idle_ms)
    except Exception:
        pass


def _page_text(page: Page) -> str:
    try:
        text = page.inner_text("body", timeout=5000)
    except Exception:
        text = ""
    main = text
    embedded: list[str] = []
    for fr in page.frames[1:]:
        try:
            if not fr.is_detached() and not _frame_definitely_hidden(fr):
                text = fr.inner_text("body", timeout=2000)
                if text.strip():
                    embedded.append(text)
        except Exception:
            continue
    # Embedded career documents often sit below thousands of characters of site navigation.
    texts = embedded + ([main] if main.strip() else [])
    limit = 8000 // max(1, len(texts))
    return " ".join(" ".join(text.split())[:limit] for text in texts)


def _messaging_field(f: Field) -> bool:
    text = f"{f.label} {f.name}"
    return bool((_MESSAGING_CHANNEL_RE.search(text) and _MESSAGING_INTENT_RE.search(text))
                or _FUTURE_PERMISSION_RE.search(text))


def _guard_choices(fields: list[Field], ans: list[Answer], profile: dict) -> list[Answer]:
    by_key = {a.key: a for a in ans}
    out = []
    for f in fields:
        a = by_key.get(f.key) or Answer(f.key, None, "unknown")
        if _messaging_field(f):
            question = f.label.split("?", 1)[0][:200]
            reversed_choice = re.search(r"opt.?out|unsubscrib|(?:do not|don't|not like to) (?:wish|want|receive|send)",
                                        question, re.I)
            value = False if f.kind == "checkbox" and not f.options and not reversed_choice else None
            if not reversed_choice:
                declines = [o for o in f.options if _DECLINE_RE.fullmatch(o.strip())]
                value = next((o for o in declines if o.strip().lower() == "no"),
                             declines[0] if len(declines) == 1 else value)
            a = Answer(f.key, value, "profile" if value is not None else "unknown",
                       "messages declined per user instruction" if value is not None else "no verified messaging opt-out")
        elif f.kind in ("select", "combobox", "radio", "checkbox"):
            if a.source == "generated":
                a = Answer(f.key, None, "unknown", "choice needs applicant evidence")
            prior = (profile.get("preferences") or {}).get("previously_employed_here")
            if (_EMPLOYMENT_HISTORY_RE.search(f.label)
                    and (prior is False or str(prior).strip().lower() in ("no", "false"))):
                negatives = [o for o in f.options if re.fullmatch(
                    r"no|i am not (?:a )?(?:current or former|former|current) employee", o.strip(), re.I)]
                a = Answer(f.key, negatives[0] if len(negatives) == 1 else None,
                           "profile" if len(negatives) == 1 else "unknown", "profile employment history")
        out.append(a)
    return out


def _guard_files(fields: list[Field], ans: list[Answer], profile: dict) -> list[Answer]:
    """Only the resume may be uploaded; other file inputs (cover letter, transcript) stay unanswered."""
    kinds = {f.key: f for f in fields}
    out = []
    for a in ans:
        f = kinds.get(a.key)
        if f is not None and f.kind == "file":
            if RESUME_LABEL_RE.search(f"{f.label} {f.name}") and profile.get("resume_path"):
                a = replace(a, value=profile["resume_path"], source="profile")
            else:
                a = replace(a, value=None, source="unknown", note="non-resume upload")
        elif (f is not None and f.kind in ("select", "combobox", "radio")
              and RESUME_LABEL_RE.search(f"{f.label} {f.name}")
              and (_PRIOR_RESUME_RE.search(f.label) or any(re.search(r"\.(?:pdf|docx?|rtf|txt)\b", o, re.I)
                                                         for o in f.options))):
            filename = Path(str(profile.get("resume_path") or "")).name.casefold()
            matches = [o for o in f.options if filename and o.strip().casefold() == filename]
            a = replace(a, value=matches[0] if len(matches) == 1 else None,
                        source="profile" if len(matches) == 1 else "unknown",
                        note="configured resume filename" if len(matches) == 1 else "upload the configured resume instead")
        out.append(a)
    return out


def _scope_with(state: State, selector: str) -> Page | Frame | None:
    for scope, _ in state.scopes:
        try:
            if scope.locator(selector).count():
                return scope
        except Exception:
            continue
    return None


def _code_scope(page: Page) -> Page | Frame | None:
    for scope in [page] + [fr for fr in page.frames[1:] if not fr.is_detached()
                           and not _frame_definitely_hidden(fr)
                           and not re.search(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare", fr.url or "")]:
        try:
            if _code_controls(scope):
                return scope
        except Exception:
            continue
    return None


def _code_masks(page: Page) -> list:
    return [loc for scope in [page] + list(page.frames[1:])
            for loc in _code_controls(scope, include_inactive=True)]


def _code_controls(scope: Page | Frame, length: int | None = None, *, include_inactive: bool = False) -> list:
    try:
        inputs = scope.locator("input")
        indices = inputs.evaluate_all(r"""(inputs, arg) => {
          const prompt = /verification code|security code|one[ -]time (?:code|pass(?:word|code)?)|passcode|check your e-?mail|confirm your identity|enter (?:the |your |a |\d+[ -]digit )?code/i;
          const context = prompt.test(document.body?.innerText || '');
          const groups = new Map(), whole = [];
          inputs.forEach((el, index) => {
            if ((!arg.include_inactive && (el.disabled || el.readOnly)) ||
                !['text','tel','number','password'].includes(el.type)) return;
            if (el.closest('[hidden],[inert],[aria-hidden=true]') ||
                !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return;
            const label = [...(el.labels || [])].map(x => x.innerText).join(' ');
            const identity = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), label]
              .filter(Boolean).join(' ').replace(/[_-]/g, ' ');
            if (/postal|postcode|zip|country|employee|referral|promotion/i.test(identity)) return;
            const explicit = el.autocomplete === 'one-time-code' ||
              /\b(?:otp|passcode)\b|(?:verification|security)\s*code|one[ -]time/i.test(identity);
            const generic = /\b(?:code|pin)\b/i.test(identity);
            if (!explicit && !(context && (generic || el.maxLength === 1))) return;
            if (el.maxLength === 1) {
              const group = el.closest('fieldset,[role=group]') || el.form || document.body;
              if (!groups.has(group)) groups.set(group, []);
              groups.get(group).push(index);
            } else if (el.maxLength < 0 || !arg.length || el.maxLength >= arg.length) whole.push(index);
          });
          const split = [...groups.values()].filter(g => g.length >= 4 && g.length <= 8 &&
            (!arg.length || g.length === arg.length));
          if (split.length === 1 && whole.length === 0) return split[0];
          if (whole.length === 1 && split.length === 0) return whole;
          return [];
        }""", {"length": length, "include_inactive": include_inactive})
        return [inputs.nth(i) for i in indices]
    except Exception:
        return []


def _verification_button_safe(target: Clickable) -> bool:
    try:
        controls = _code_controls(target.frame)
        if not controls:
            return False
        indices = [loc.evaluate("el => [...document.querySelectorAll('input')].indexOf(el)")
                   for loc in controls]
        return bool(target.frame.locator(f'[data-aa-click="{target.index}"]').evaluate("""(button, indices) => {
          const form = button.form || button.closest('form') || document.body;
          const all = [...document.querySelectorAll('input')];
          if (!indices.every(i => form.contains(all[i]))) return false;
          if (form.querySelector('select,textarea,input[type=file]')) return false;
          return [...form.querySelectorAll('input')].every(input => {
            if (['hidden','button','submit'].includes(input.type) || input.disabled ||
                !input.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return true;
            return ['checkbox','radio'].includes(input.type) || indices.includes(all.indexOf(input));
          });
        }""", indices))
    except Exception:
        return False


def _fill_first(scope: Page | Frame, selector: str, value: str) -> bool:
    loc = scope.locator(selector)
    try:
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible() and (el.get_attribute("type") or "text") not in ("hidden", "checkbox", "radio", "password"):
                el.fill(value)
                return True
    except Exception:
        pass
    return False


def _enter_code(scope: Page | Frame, code: str) -> bool:
    page = scope if isinstance(scope, Page) else scope.page
    if not re.fullmatch(r"[A-Za-z0-9]{4,8}", code):
        return False
    for attempt in range(2):
        controls = _code_controls(scope, len(code))
        if len(controls) not in (1, len(code)):
            return False
        values = [code] if len(controls) == 1 else list(code)
        for index, value in enumerate(values):
            try:
                # Reacquire after every input: controlled components can replace all of the boxes.
                current = _code_controls(scope, len(code))
                if len(current) != len(controls):
                    return False
                loc = current[index]
                if loc.input_value(timeout=1000) != value:
                    if attempt == 0:
                        loc.fill("", timeout=2000)
                        current = _code_controls(scope, len(code))
                        if len(current) != len(controls):
                            return False
                        current[index].press_sequentially(value, delay=40, timeout=2000)
                    else:
                        loc.fill(value, timeout=2000)
                    page.wait_for_timeout(75)
            except Exception:
                break
        try:
            current = _code_controls(scope, len(code))
            current[-1].blur(timeout=1000)
            page.wait_for_timeout(250)
            if _code_complete(scope, code):
                return True
        except Exception:
            return False
    return False


def _code_complete(scope: Page | Frame, code: str) -> bool:
    try:
        controls = _code_controls(scope, len(code))
        return (len(controls) in (1, len(code))
                and "".join(loc.input_value(timeout=1000) for loc in controls) == code)
    except Exception:
        return False


def _redacted_url(url: str) -> str:
    parsed = urlparse(url)
    query = [(key, "[redacted]" if re.fullmatch(
        r"code|otp|pin|passcode|password|verification[_-]?code|d\d|digit\d", key, re.I) else value)
             for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunparse(parsed._replace(query=urlencode(query)))


def _safe_url(page: Page) -> str | None:
    try:
        return _redacted_url(page.url)
    except Exception:
        return None


def _screenshot(page: Page, job: Job, tag: str) -> str | None:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{job.company}_{job.id}_generic_{tag}")[:120]
    path = SCREENSHOT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=True, timeout=15000, mask=_code_masks(page))
        return str(path)
    except Exception:
        return None


ADAPTER = GenericAdapter()
