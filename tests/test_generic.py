"""Offline tests for the generic vision adapter: scripted LLM, fake form/answers/mail/vault, local pages
served through Playwright routing (no network)."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
import types
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright

# Sibling modules are built in parallel; stub any that do not exist yet so this module imports.
for _name in ("form", "answers", "captcha", "mail", "vault"):
    if importlib.util.find_spec(f"agent.{_name}") is None:
        sys.modules[f"agent.{_name}"] = types.ModuleType(f"agent.{_name}")

from agent import db, llm as real_llm  # noqa: E402
from agent.ats import base, generic  # noqa: E402
from agent.models import Answer, ApplyStatus, Field, Job, normalize_url  # noqa: E402
from agent.filters import policy_fingerprint  # noqa: E402

HOST = "https://jobs.example.test"

PAGES = {
    "/job": """<h1>Software Engineering Intern</h1><p>Build things. Summer 2027.</p>
        <a href="https://www.linkedin.com/jobs/apply">Apply with LinkedIn</a>
        <a href="/apply">Apply</a>""",
    "/ukg_job": """<h1>Software Engineering Intern</h1><p>Summer 2027.</p>
        <ukg-button data-bind="click: apply" data-tag-name="button">Apply now</ukg-button>
        <script>document.querySelector('ukg-button').addEventListener('click', () => location.href='/apply');</script>""",
    "/nvidia_job": """<h1>NVIDIA 2027 Internships: Software Engineering</h1>
        <button type="button" onclick="location.href='/nvidia_step'">Apply Now</button>
        <section><p>Find out how well you match with this job</p>
          <label for="match">Upload Your Resume</label><input id="match" type="file">
        </section><h2>Job Description</h2><p>Summer 2027.</p>""",
    "/nvidia_step": """<h1>Start your candidate profile</h1>
        <button type="button" onclick="location.href='/apply'">Apply Now</button>""",
    "/clearcompany_resume": """<h1>Upload Documents</h1>
        <section><h3>Resume</h3><button type="button" onclick="toggle('resume')">Upload</button>
          <div id="resume-menu" style="display:none"><button type="button" onclick="pick('resume')">File</button>
            <button type="button">Dropbox</button><button type="button">Google Drive</button></div>
          <input id="resume-file" type="file" style="display:none">
          <p id="resume-name"></p></section>
        <section><h3>Cover Letter</h3><button type="button" onclick="toggle('cover')">Upload</button>
          <div id="cover-menu" style="display:none"><button type="button" onclick="pick('cover')">File</button></div>
          <input id="cover-file" type="file" style="display:none"></section>
        <section><h3>Upload Additional Files</h3><button type="button" onclick="toggle('extra')">Upload</button>
          <div id="extra-menu" style="display:none"><button type="button" onclick="pick('extra')">File</button></div>
          <input id="extra-file" type="file" style="display:none"></section>
        <script>
        function toggle(which) { document.getElementById(which + '-menu').style.display = 'block'; }
        function pick(which) { document.getElementById(which + '-file').click(); }
        document.getElementById('resume-file').addEventListener('change', e => {
          document.getElementById('resume-name').textContent = e.target.files[0].name;
        });
        </script>""",
    "/clearcompany_sms": """<h1>Contact information</h1><form method="post" action="/thanks">
        <label for="email">Email</label><input id="email" type="email">
        <fieldset><legend>May we use this number to send you text messages from ETC that pertain to your job application?</legend>
          <label><input type="radio" name="sms-opt-in" value="yes">Yes</label>
          <label><input type="radio" name="sms-opt-in" value="no">No</label></fieldset>
        <button type="submit">Submit application</button></form>""",
    "/employment_choice": """<h1>Application</h1><form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" required>
        <label for="email">Email</label><input id="email" type="email" required>
        <span id="history-choice" role="combobox" tabindex="0" aria-required="true"
          aria-label="Have you ever been employed by this company?" aria-controls="history-options" aria-expanded="false"
          onclick="toggle()" onkeydown="if(event.key === 'Escape') closeOptions()">Please select</span>
        <div id="history-options" role="listbox" hidden>
          <div role="option" onclick="choose(this)">I am a current employee</div>
          <div role="option" onclick="choose(this)">I am a former employee</div>
          <div role="option" onclick="choose(this)">I am NOT a current or former employee</div></div>
        <button type="submit">Submit application</button></form>
        <script>
        const combo = document.getElementById('history-choice'), menu = document.getElementById('history-options');
        function closeOptions() { menu.hidden = true; combo.setAttribute('aria-expanded', 'false'); }
        function toggle() { menu.hidden = !menu.hidden; combo.setAttribute('aria-expanded', String(!menu.hidden)); }
        function choose(el) { combo.textContent = el.textContent; closeOptions(); }
        </script>""",
    "/posting_submit_button": """<h1>Software Engineering Intern</h1><h2>Job Description</h2>
        <label for="match">Resume match</label><input id="match" name="match">
        <form action="/thanks" method="post"><button type="submit">Apply Now</button></form>""",
    "/posting_danger": """<h1>Software Engineering Intern</h1><h2>Job Description</h2>
        <form action="/thanks" method="post"><label for="fn">First name</label>
        <input id="fn" name="first_name" required><button type="submit">Submit application</button></form>""",
    "/posting_js_submit": """<h1>Software Engineering Intern</h1><h2>Job Description</h2>
        <form method="post" action="/thanks"><input type="hidden" name="job" value="123"></form>
        <button type="button" onclick="document.querySelector('form').requestSubmit()">Apply Now</button>""",
    "/posting_js_mixed": """<h1>Software Engineering Intern</h1><h2>Job Description</h2>
        <form method="post" action="/thanks"></form>
        <button type="button" onclick="location.href='/apply'; document.querySelector('form').requestSubmit()">Apply Now</button>""",
    "/posting_href_handler": """<h1>Software Engineering Intern</h1><h2>Job Description</h2>
        <form method="post" action="/thanks"></form>
        <a href="/apply" onclick="document.querySelector('form').requestSubmit()">Apply Now</a>""",
    "/apply": """<h1>Application</h1>
        <form method="post" action="/thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <label for="em">Email</label><input id="em" type="email" name="email" required>
          <button type="submit">Submit application</button>
        </form>""",
    "/application_consent": """<h1>Application</h1>
        <form method="post" action="/thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <label for="em">Email</label><input id="em" type="email" name="email" required>
          <button type="submit">Submit application</button>
        </form>
        <div role="dialog" aria-modal="true" style="position:fixed;inset:0;background:white">
          <form method="post" action="/consent_saved">
            <h2>Privacy consent</h2><p>Choose how profile data is used before continuing.</p>
            <button type="submit">Submit</button>
          </form>
        </div>""",
    "/consent_saved": """<h1>Application</h1><form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <label for="em">Email</label><input id="em" type="email" name="email" required>
        <button type="submit">Submit application</button></form>""",
    "/thanks": "<h1>Thank you for applying!</h1><p>We will be in touch.</p>",
    "/blank_thanks": "<h1>We will be in touch.</h1><p>Your information was received.</p>",
    "/apply_btn": """<h1>Application</h1>
        <form method="post" action="/thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <button type="submit">Apply</button>
        </form>""",
    "/go_btn": """<h1>Application</h1>
        <form method="post" action="/thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <input type="submit" value="Go">
        </form>""",
    "/react_form": """<h1>Application</h1>
        <div><label for="fn">First name</label><input id="fn" name="first_name">
          <label for="em">Email</label><input id="em" type="email" name="email">
          <div role="button" tabindex="0" onclick="fetch('/thanks', {method: 'POST'})">Apply now</div></div>""",
    "/landing": """<h1>Careers</h1><input aria-label="Search jobs" type="text">
        <a href="/apply">Submit your resume</a>""",
    "/review": """<h1>Review your application</h1><p>First name: Ada</p>
        <form method="post" action="/thanks"><button type="submit">Submit application</button></form>""",
    "/stuck": """<h1>Careers</h1><button onclick="void 0">Nothing</button>""",
    "/popup_job": """<h1>Software Engineering Intern</h1>
        <a href="/apply" target="_blank">Apply</a>""",
    "/framed_link_job": """<h1>Software Engineering Intern</h1>
        <iframe src="/framed_link_only" width="600" height="400"></iframe>""",
    "/framed_link_only": """<h2>Ready to apply?</h2>
        <a href="/apply">Apply</a>""",
    "/framed": """<h1>Careers portal</h1><iframe src="/apply" width="800" height="400"></iframe>""",
    "/frame_privacy": """<h1>Application</h1><form method="post" action="/thanks">
        <label for="em">Email</label><input id="em" type="email" name="email" required>
        <button type="submit">Submit application</button></form>
        <iframe id="profile" style="visibility:hidden" src="/frame_shell" width="600" height="300"></iframe>""",
    "/frame_shell": """<iframe src="/frame_names" width="400" height="200"></iframe>""",
    "/frame_names": """<form><label for="fn">First Name</label><input id="fn" required>
        <label for="ln">Last Name</label><input id="ln" required>
        <button type="button">Hidden action</button></form>""",
    "/icims_privacy": """<h1>Enter Your Information</h1><form>
        <label for="em">Email</label><input id="em" type="email" required>
        <label><input type="checkbox" checked> I accept</label>
        <section id="future" style="opacity:0;pointer-events:none">
          <label for="fn">First Name</label><input id="fn" required>
          <label for="ln">Last Name</label><input id="ln" required>
          <label for="phone">Phone Number</label><input id="phone">
        </section>
        <button type="button" onclick="document.querySelector('#future').style.cssText='';this.remove()">Next</button>
        </form>""",
    "/account": """<h1>Create your candidate account</h1>
        <form method="post" action="/verify">
          <label for="e">Email</label><input id="e" type="email" name="email">
          <label for="p">Password</label><input id="p" type="password" name="password">
          <button type="submit">Create account</button>
        </form>""",
    "/account_separate": """<h1>Apply for this job</h1>
        <form method="post" action="/thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <button type="submit">Submit application</button></form>
        <h2>Create your account</h2><form method="post" action="/verify">
          <label for="e">Email</label><input id="e" type="email" name="email">
          <label for="p">Password</label><input id="p" type="password" name="password">
          <button type="submit">Create account</button></form>""",
    "/sign_in": """<h1>Sign in to your existing account</h1>
        <form method="post" action="/login">
          <label for="e">Email</label><input id="e" type="email" name="email">
          <label for="p">Password</label><input id="p" type="password" name="password">
          <button type="submit">Sign in</button></form>""",
    "/login": "<h1>Signed in</h1>",
    "/account_unsafe_action": """<h1>Create your account</h1>
        <form method="post" action="/thanks">
          <label for="e">Email</label><input id="e" type="email" name="email">
          <label for="p">Password</label><input id="p" type="password" name="password">
          <button type="submit">Create account</button></form>""",
    "/account_unsafe_js": """<h1>Create your account</h1>
        <form method="post" action="/verify">
          <label for="e">Email</label><input id="e" type="email" name="email">
          <label for="p">Password</label><input id="p" type="password" name="password">
          <button type="button" onclick="fetch('/thanks',{method:'POST'})">Create account</button></form>""",
    "/account_pending": """<h1>Create an Account</h1>
        <form method="get" action="/verify" onsubmit="return window.accepted === true">
          <label for="e">Email</label><input id="e" type="email" name="email">
          <label for="p">Password</label><input id="p" type="password" name="password">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <button type="button" onclick="window.accepted = true; this.textContent = 'Accepted'">Accept privacy</button>
          <button type="submit">Create Account</button>
        </form>""",
    "/account_and_submit": """<h1>Application</h1><form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <label for="p">Password</label><input id="p" type="password" name="password">
        <button type="submit">Create account and submit application</button></form>""",
    "/application_create_account": """<h1>Application</h1><form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <button type="submit">Create Account</button></form>""",
    "/combined_account": """<h1>Apply for this job</h1><form method="post" action="/thanks">
        <label for="e">Email</label><input id="e" type="email" name="email" required>
        <label for="p">Password</label><input id="p" type="password" name="password">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <label for="q">Why do you want this job?</label><input id="q" name="question" required>
        <button type="submit">Create Account</button></form>""",
    "/verify": """<h1>Check your email</h1>
        <form method="get" action="/apply">
          <label for="c">Verification code</label><input id="c" name="code">
          <button type="submit">Verify</button>
        </form>""",
    "/application_otp_final": """<h1>Application verification</h1>
        <form method="post" action="/thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <label for="em">Email</label><input id="em" type="email" name="email" required>
          <label for="code">Verification code</label><input id="code" name="code">
          <button type="submit">Submit application</button>
        </form>""",
    "/verify_boxes": """<h1>Enter 6-digit email verification code</h1>
        <form method="get" action="/otp_done">
          <label for="fn">First name</label><input id="fn" name="first_name">
          <input maxlength="1" name="d0" aria-label="Digit 1">
          <input maxlength="1" name="d1" aria-label="Digit 2">
          <input maxlength="1" name="d2" aria-label="Digit 3">
          <input maxlength="1" name="d3" aria-label="Digit 4">
          <input maxlength="1" name="d4" aria-label="Digit 5">
          <input maxlength="1" name="d5" aria-label="Digit 6">
          <button type="submit">Verify code</button>
        </form>""",
    "/oracle_verify": """<h1>Confirm Your Identity</h1><p>Enter the 6-digit code sent to your email.</p>
        <form method="get" action="/otp_done">
          <input maxlength="1" aria-label="Digit 1" name="d0">
          <input maxlength="1" aria-label="Digit 2" name="d1">
          <input maxlength="1" aria-label="Digit 3" name="d2">
          <input maxlength="1" aria-label="Digit 4" name="d3">
          <input maxlength="1" aria-label="Digit 5" name="d4">
          <input maxlength="1" aria-label="Digit 6" name="d5">
          <button type="submit">Verify</button>
        </form>""",
    "/otp_done": "<h1>Account verified. Continue application.</h1>",
    "/verify_framed": """<h1>Verification</h1>
        <iframe src="/verify" width="600" height="350"></iframe>""",
    "/verify_wrong_input": """<h1>Check your email</h1>
        <form method="get" action="/otp_done">
          <label for="fn">First name</label><input id="fn" name="first_name">
          <button type="submit">Verify</button>
        </form>""",
    "/wizard1": """<h1>Application</h1><p>Step 1 of 2</p>
        <form method="post" action="/wizard2">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <label for="em">Email</label><input id="em" type="email" name="email" required>
          <button type="submit">Next</button>
        </form>""",
    "/wizard2": """<h1>Additional Information</h1>
        <form method="post" action="/thanks">
          <label for="city">City</label><input id="city" name="city">
          <button type="submit">Submit application</button>
        </form>""",
    "/wizard_continue1": """<h1>Application</h1><p>Step 1 of 2</p>
        <form method="post" action="/wizard2">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <button type="submit">Continue</button>
        </form>""",
    "/apply_to_review": """<h1>Application</h1><p>Step 1 of 2</p>
        <form>
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <label for="em">Email</label><input id="em" type="email" name="email" required>
        </form><a href="/review_continue">Review application</a>""",
    "/review_continue": """<h1>Review your application</h1><p>First name: Ada</p>
        <form method="post" action="/thanks"><button type="submit">Continue</button></form>""",
    "/final_next": """<h1>Application</h1><form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <button type="submit">Next</button></form>""",
    "/final_continue": """<h1>Application</h1><p>Step 2 of 2</p><form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <button type="submit">Continue</button></form>""",
    "/final_next_aria": """<h1>Application</h1><p>Step 1 of 2</p><form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <button type="submit" aria-label="Submit application">Next</button></form>""",
    "/final_next_conflict": """<h1>Application</h1><p>Step 1 of 2</p><p>Step 2 of 2</p>
        <form method="post" action="/thanks"><label for="fn">First name</label>
        <input id="fn" name="first_name" required><button type="submit">Next</button></form>""",
    "/misleading_step": """<h1>Application</h1><p>Step 1 of 2</p>
        <form method="post" action="/thanks"><label for="fn">First name</label>
        <input id="fn" name="first_name" required><button type="submit">Next</button></form>""",
}


class FakeForm:
    _JS = """
    () => {
      const out = [];
      let i = 0;
      for (const el of document.querySelectorAll('input, select, textarea')) {
        const t = (el.getAttribute('type') || 'text').toLowerCase();
        if (['hidden', 'submit', 'button', 'password'].includes(t)) continue;
        const key = 'f' + (i++);
        el.setAttribute('data-aa-key', key);
        const lab = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
        out.push({key, label: lab ? lab.innerText.trim() : (el.name || ''), kind: t === 'email' ? 'email' : 'text',
                  required: el.required, name: el.name || '', value: el.value || ''});
      }
      return out;
    }
    """

    @staticmethod
    def extract_fields(scope, root=None):
        return [Field(d["key"], d["label"], d["kind"], d["required"], name=d["name"], current_value=d["value"])
                for d in scope.evaluate(FakeForm._JS)]

    @staticmethod
    def fill_fields(scope, fields, answers):
        failed = []
        for a in answers:
            if isinstance(a.value, str):
                try:
                    scope.locator(f'[data-aa-key="{a.key}"]').fill(a.value, timeout=2000)
                except Exception:
                    failed.append(a.key)
        return failed

    @staticmethod
    def page_text(page, max_chars=8000):
        return page.inner_text("body")[:max_chars]

    @staticmethod
    def click_text(scope, texts, roles=("button", "link")):
        for t in texts:
            for role in roles:
                loc = scope.get_by_role(role, name=t, exact=True)
                if loc.count():
                    loc.first.click()
                    return True
        return False


class FakeAnswers:
    unknown_labels: set[str] = set()

    @classmethod
    def answer_fields(cls, fields, job, profile, *, job_text="", extra_context=""):
        out = []
        for f in fields:
            if f.label in cls.unknown_labels:
                out.append(Answer(f.key, None, "unknown"))
            elif f.kind == "email":
                out.append(Answer(f.key, "ada@example.com", "profile"))
            else:
                out.append(Answer(f.key, "Ada", "profile"))
        return out


@dataclass
class Account:
    tenant_key: str
    email: str
    password: str
    company: str
    login_url: str
    verified: bool = False


class FakeVault:
    def __init__(self):
        self.accounts: dict[str, Account] = {}
        self.created: list[str] = []
        self.verified: list[str] = []

    def tenant_key_for(self, url):
        return urlparse(url).netloc

    def get(self, tenant_key):
        return self.accounts.get(tenant_key)

    def create(self, tenant_key, company, login_url, *, plus_alias=True):
        acct = Account(tenant_key, "ada+acme@example.com", "S3cret-pass-123!", company, login_url)
        self.accounts[tenant_key] = acct
        self.created.append(tenant_key)
        return acct

    def mark_verified(self, tenant_key):
        self.verified.append(tenant_key)


class FakeMail:
    def __init__(self, code="123456"):
        self.code = code
        self.calls: list[dict] = []
        self.acknowledged: list[str] = []

    def available(self):
        return True

    def wait_for_verification(self, after_ts, *, sender_hint=None, to_alias=None, timeout=None, want="any", consume=True):
        self.calls.append({"after_ts": after_ts, "sender_hint": sender_hint,
                           "to_alias": to_alias, "want": want, "timeout": timeout, "consume": consume})
        return SimpleNamespace(code=self.code, link=None, message_id="m1", subject="Your code", sender="no-reply")

    def acknowledge_verification(self, verification, *, want="any"):
        assert want == "code"
        self.acknowledged.append(verification.message_id)


class FakeLLM:
    LLMUnavailable = real_llm.LLMUnavailable

    def __init__(self, script, up=True, fail_with=None):
        self.script = list(script)
        self.up = up
        self.fail_with = fail_with
        self.prompts: list[str] = []
        self.images: list = []

    def available(self):
        return self.up

    def vision_model(self):
        return "fake-vision"

    def generate_json(self, prompt, schema, *, images=None, model=None, **kw):
        assert model == "fake-vision"
        self.prompts.append(prompt)
        self.images.append(images)
        if self.fail_with:
            raise self.fail_with
        if not self.script:
            return {"action": "give_up", "reason": "script exhausted"}
        step = self.script.pop(0)
        return step(prompt) if callable(step) else step


def act(action, reason="scripted"):
    return {"action": action, "reason": reason}


def click(text, action="click"):
    def pick(prompt):
        m = re.search(rf"^\[(\d+)\] <[\w-]+> {re.escape(text)}(?= \(| ->|$)", prompt, re.M)
        assert m, f"no clickable {text!r} in prompt"
        return {"action": action, "click_index": int(m.group(1)), "reason": f"{action} {text}"}
    return pick


@pytest.fixture(scope="module")
def chromium():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def site(chromium):
    ctx = chromium.new_context(viewport={"width": 1000, "height": 800})
    hits: list[tuple[str, str, str]] = []

    def handle(route):
        req = route.request
        u = urlparse(req.url)
        hits.append((req.method, u.path, u.query or (req.post_data or "")))
        body = PAGES.get(u.path)
        if body is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=f"<html><body>{body}</body></html>")

    ctx.route("**/*", lambda route: route.abort())
    ctx.route(f"{HOST}/**", handle)
    ctx.route("https://test.clearcompany.com/**", handle)
    page = ctx.new_page()
    yield SimpleNamespace(ctx=ctx, page=page, hits=hits)
    ctx.close()


@pytest.fixture(autouse=True)
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    vault, mail = FakeVault(), FakeMail()
    FakeAnswers.unknown_labels = set()
    monkeypatch.setattr(generic, "form", FakeForm)
    monkeypatch.setattr(generic, "answers", FakeAnswers)
    monkeypatch.setattr(generic, "vault", vault)
    monkeypatch.setattr(generic, "mail", mail)
    monkeypatch.setattr(generic, "captcha",
                        SimpleNamespace(solve_if_present=lambda page, config, job_id=None:
                                        SimpleNamespace(status="none", kind="", cost=0.0, detail=""),
                                        blocks_submit=lambda outcome, config=None:
                                        outcome.status in ("failed", "budget", "unsupported")))
    monkeypatch.setattr(generic, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(generic, "human_pause", lambda *a, **k: None)
    return SimpleNamespace(vault=vault, mail=mail, shots=tmp_path)


def run(site, monkeypatch, script, path="/job", *, dry_run=False, mode="full", host_override=None,
        profile_override=None, config_override=None, **llm_kw):
    fake = FakeLLM(script, **llm_kw)
    monkeypatch.setattr(generic, "llm", fake)
    job = Job(id="test:1", company="Acme", title="Software Engineering Intern", url=(host_override or HOST) + path,
              locations=["Remote"], category="Software", date_posted=0)
    db.upsert_jobs([job])
    config = {"email": {"verification_timeout_seconds": 30}, "filters": {
        "terms": ["Summer 2027"], "us_only": True, "software_categories": ["Software"],
        "ai_categories": [], "ai_title_keywords": [], "exclude_title_keywords": [],
    }}
    config.update(config_override or {})
    evidence = {
        "policy_fingerprint": policy_fingerprint(config), "posting_id": job.id,
        "normalized_url": normalize_url(job.url), "url": job.url, "source": job.source,
        "company": job.company, "title": job.title, "category": job.category,
        "terms": ["Summer 2027"], "locations": list(job.locations), "location_context_us": True,
    }
    db.kv_set(f"filter_policy:{job.id}", policy_fingerprint(config))
    db.kv_set(f"filter_evidence:{job.id}", json.dumps(evidence, sort_keys=True))
    run_id = db.start_run("apply-dry" if dry_run else "apply")
    if not dry_run:
        previous_status = db.get_job_status(job.id) or "new"
        assert db.mark_job_in_progress(job.id, run_id, previous_status=previous_status)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config=config,
                            profile=profile_override or {"resume_path": "/tmp/resume.pdf"},
                            submit_mode=mode, dry_run=dry_run,
                            run_id=run_id)
    result = generic.ADAPTER.apply(job, ctx)
    if not dry_run:
        db.record_application(job, result, run_id)
        db.set_job_status(job.id, result.status.value)
    db.end_run(run_id, submitted=int(result.status == ApplyStatus.SUBMITTED))
    return result, fake


def posted_thanks(site):
    return any(m == "POST" and p == "/thanks" for m, p, _ in site.hits)


APPLY_FLOW = [click("Apply"), act("fill_form"), click("Submit application", action="submit")]


def test_ukg_unverified_event_listener_fails_closed(site, monkeypatch):
    script = [click("Apply now")]
    res, _ = run(site, monkeypatch, script, path="/ukg_job", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "unverified" in res.reason
    assert not any(method == "GET" and path == "/apply" for method, path, _ in site.hits)
    assert not posted_thanks(site)


@pytest.mark.parametrize("first_action,second_action", [("click", "click"), ("click", "submit"),
                                                        ("submit", "click")])
def test_posting_apply_now_navigates_past_resume_matcher_without_submit_intent(
        site, monkeypatch, first_action, second_action):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    script = [click("Apply Now", action=first_action), click("Apply Now", action=second_action),
              act("fill_form"), click("Submit application", action="submit")]
    res, fake = run(site, monkeypatch, script, path="/nvidia_job", mode="fill_only")
    assert res.status == ApplyStatus.NEEDS_REVIEW and "fill_only" in res.reason, res.reason
    assert len(fake.prompts) == 4
    assert any(path == "/nvidia_step" for _, path, _ in site.hits)
    assert any(path == "/apply" for _, path, _ in site.hits)
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_form_associated_apply_now_without_application_fields_is_blocked(site, monkeypatch):
    res, _ = run(site, monkeypatch, [click("Apply Now")], path="/posting_submit_button")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_posting_markers_never_exempt_submit_button_from_gate(site, monkeypatch):
    res, _ = run(site, monkeypatch, [click("Submit application", action="submit")],
                 path="/posting_danger", dry_run=True)
    assert res.status in (ApplyStatus.DRY_RUN_OK, ApplyStatus.NEEDS_REVIEW)
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_adapter_contract():
    assert generic.ADAPTER.name == "generic"
    assert generic.ADAPTER.can_handle(Job("x:1", "Co", "Intern", "https://anything.example/job", [], "", 0))


def test_verification_link_returns_without_accessing_mail(site, monkeypatch, fakes):
    def unexpected_mail_access(*args, **kwargs):
        pytest.fail("verification must not access mail while manual review is required")

    monkeypatch.setattr(fakes.mail, "available", unexpected_mail_access)
    monkeypatch.setattr(fakes.mail, "wait_for_verification", unexpected_mail_access)
    res, _ = run(site, monkeypatch, [act("need_email_link")], path="/verify")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "manual review" in res.reason
    assert not db.mail_is_seen("test-message")


def test_verification_filters_do_not_broaden_other_sites_or_companies():
    email = "person+alias@gmail.com"
    assert generic._verification_filters("https://example.com/login", "Celestica", email) == ("example", email)
    assert generic._verification_filters("https://career.successfactors.com/login", "Other", email) == ("successfactors", email)
    assert generic._verification_filters("https://career.successfactors.eu/login", "Other", email) == ("successfactors", email)
    assert generic._verification_filters("https://successfactors.com.example/login", "Celestica", email) == ("com", email)
    assert generic._verification_filters("https://successfactors.eu.example/login", "Celestica", email) == ("eu", email)
    assert generic._verification_filters("https://career.successfactors.com/login", "Celestica", email) == ("celestica", "person@gmail.com")
    assert generic._verification_filters("https://career.successfactors.com/login", "Celestica", "person+alias@example.com") == ("celestica", "person+alias@example.com")
    assert generic._verification_filters("https://icfcjb.fa.ocs.oraclecloud.com/job/1", "Aerospace", email) == ("oracle", email)
    assert generic._verification_filters("https://fa-erqb-saasfaprod1.fa.ocs.oraclecloud.com/job/1", "ADT", email) == ("oracle", email)


def test_success(site, monkeypatch, fakes):
    res, fake = run(site, monkeypatch, APPLY_FLOW)
    assert res.status == ApplyStatus.SUBMITTED, res.reason
    assert posted_thanks(site)
    assert "first_name=Ada" in next(q for m, p, q in site.hits if p == "/thanks")
    assert res.final_url.endswith("/thanks")
    assert res.screenshot and res.screenshot.startswith(str(fakes.shots))
    assert res.fields_total == 2 and not res.unanswered_required
    assert all(imgs and imgs[0][:4] == b"\x89PNG" for imgs in fake.images)
    assert "Apply with LinkedIn" in fake.prompts[0] and "Software Engineering Intern" in fake.prompts[0]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_success_with_real_form_module(site, monkeypatch):
    real_form = sys.modules.get("agent.form")
    if not hasattr(real_form, "extract_fields"):
        pytest.skip("agent/form.py not built yet")
    monkeypatch.setattr(generic, "form", real_form)
    res, _ = run(site, monkeypatch, APPLY_FLOW)
    assert res.status == ApplyStatus.SUBMITTED, res.reason
    assert "first_name=Ada" in next(q for m, p, q in site.hits if p == "/thanks")


def test_computer_use_field_repair_still_submits_local_application(site, monkeypatch):
    from agent import llm as vision_llm

    real_form = sys.modules.get("agent.form")
    monkeypatch.setattr(generic, "form", real_form)
    original_fill = real_form._fill_one

    def fail_first_name(scope, field, value):
        if field.label.casefold() == "first name":
            return False
        return original_fill(scope, field, value)

    monkeypatch.setattr(real_form, "_fill_one", fail_first_name)
    observed = {}

    def choose_field(prompt, schema, **kwargs):
        observed["prompt"] = prompt
        observed["images"] = kwargs.get("images")
        box = site.page.locator('input[name="first_name"]').bounding_box()
        viewport = site.page.viewport_size
        point = [round((box["y"] + box["height"] / 2) / viewport["height"] * 1000),
                 round((box["x"] + box["width"] / 2) / viewport["width"] * 1000)]
        return {"action": "type", "point": point, "text": "unapproved model value", "reason": "found first name"}

    monkeypatch.setattr(vision_llm, "generate_json", choose_field)
    result, _ = run(site, monkeypatch, APPLY_FLOW,
                    config_override={"computer_use": {"enabled": True}})

    assert result.status == ApplyStatus.SUBMITTED, result.reason
    assert posted_thanks(site)
    assert "first_name=Ada" in next(q for m, p, q in site.hits if p == "/thanks")
    assert observed["images"] and observed["images"][0][:2] == b"\xff\xd8"
    assert "Page and target context" in observed["prompt"]
    assert "First name" in observed["prompt"]


@pytest.mark.parametrize("final", ["submit", "click"])
def test_dry_run_never_clicks_submit(site, monkeypatch, final):
    res, _ = run(site, monkeypatch, APPLY_FLOW[:2] + [click("Submit application", action=final)], dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    assert not posted_thanks(site)
    assert res.final_url.endswith("/apply")
    assert res.screenshot


def test_dry_run_review_page_without_fields(site, monkeypatch):
    res, _ = run(site, monkeypatch, [click("Submit application")], path="/review", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW, res.reason
    assert "without validated applicant answers" in res.reason
    assert not posted_thanks(site)


def test_missing_click_index_cannot_use_vision_to_submit(site, monkeypatch):
    from agent import vision_agent

    vision_clicks = []
    monkeypatch.setattr(vision_agent, "locate_and_act",
                        lambda *args, **kwargs: vision_clicks.append(kwargs) or True)
    script = [{"action": "click", "click_index": 999, "reason": "Click Submit application"},
              act("give_up")]
    res, _ = run(site, monkeypatch, script, path="/apply", dry_run=True,
                 host_override="https://test.clearcompany.com")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert not vision_clicks
    assert not posted_thanks(site)


def test_stuck_recovery_cannot_bypass_submit_gate_with_vision(site, monkeypatch):
    from agent import vision_agent

    vision_clicks = []
    monkeypatch.setattr(vision_agent, "locate_and_act",
                        lambda *args, **kwargs: vision_clicks.append(kwargs) or True)
    script = [{"action": "click", "click_index": 999, "reason": "Click Submit application"}] * 8
    res, _ = run(site, monkeypatch, script, path="/apply", dry_run=True,
                 host_override="https://test.clearcompany.com")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "stuck" in res.reason
    assert not vision_clicks
    assert not posted_thanks(site)


def test_fill_only_blocks_submit(site, monkeypatch):
    res, _ = run(site, monkeypatch, APPLY_FLOW, mode="fill_only")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "fill_only" in res.reason
    assert not posted_thanks(site)


def test_unknown_required_blocks_submit(site, monkeypatch):
    FakeAnswers.unknown_labels = {"First name"}
    res, _ = run(site, monkeypatch, APPLY_FLOW)
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert res.unanswered_required == ["First name"]
    assert not posted_thanks(site)


def test_loop_detection_gives_up(site, monkeypatch):
    res, fake = run(site, monkeypatch, [click("Nothing")] * 10, path="/stuck")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "stuck" in res.reason
    assert len(fake.prompts) == 2
    assert res.screenshot


def test_llm_unavailable_before_start(site, monkeypatch):
    t0 = time.time()
    res, fake = run(site, monkeypatch, APPLY_FLOW, up=False)
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert res.reason == "LLM unavailable"
    assert not fake.prompts
    assert time.time() - t0 < 10


def test_llm_unavailable_mid_run(site, monkeypatch):
    res, fake = run(site, monkeypatch, APPLY_FLOW, fail_with=real_llm.LLMUnavailable("ConnectTimeout"))
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert res.reason.startswith("LLM unavailable")
    assert len(fake.prompts) == 1
    assert res.screenshot


def test_success_inside_iframe(site, monkeypatch):
    res, fake = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")], path="/framed")
    assert res.status == ApplyStatus.SUBMITTED, res.reason
    assert "First name" in fake.prompts[0]
    assert posted_thanks(site)


def test_apply_link_in_new_tab_becomes_active_application_page(site, monkeypatch):
    res, fake = run(site, monkeypatch,
                    [click("Apply"), act("fill_form"), click("Submit application", action="submit")],
                    path="/popup_job", mode="fill_only")
    assert "fill_only" in res.reason, res.reason
    assert len(site.ctx.pages) == 2
    assert site.ctx.pages[-1].url == HOST + "/apply"
    assert "First name" in fake.prompts[-1]
    assert not posted_thanks(site)


def test_delayed_javascript_apply_popup_requires_review_without_click(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/delayed_popup", """<h1>Software Engineering Intern</h1>
        <button type="button" onclick="window.applyClicks=(window.applyClicks||0)+1;setTimeout(()=>window.open('/apply'),1200)">
          Apply Now</button>""")
    existing = site.ctx.new_page()
    existing.goto(HOST + "/account")
    result, fake = run(site, monkeypatch, [click("Apply Now"), act("fill_form"),
                                          click("Submit application", action="submit")],
                        path="/delayed_popup", mode="fill_only")
    assert result.status == ApplyStatus.NEEDS_REVIEW and "unverified" in result.reason, result.reason
    assert site.page.evaluate("window.applyClicks") is None
    assert result.final_url == HOST + "/delayed_popup"
    assert len(fake.prompts) == 1
    assert len(site.ctx.pages) == 2
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_ambiguous_javascript_apply_popups_require_review_without_click(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/multi_popup", """<h1>Software Engineering Intern</h1>
        <button type="button" onclick="window.clicks=(window.clicks||0)+1;
          window.open('null');setTimeout(()=>window.open('/apply'),350)">Apply Now</button>""")
    res, fake = run(site, monkeypatch, [click("Apply Now"), act("fill_form"),
                                       click("Submit application", action="submit")],
                    path="/multi_popup", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "unverified" in res.reason, res.reason
    assert res.final_url == HOST + "/multi_popup"
    assert site.page.evaluate("window.clicks") is None
    assert len(fake.prompts) == 1
    assert len(site.ctx.pages) == 1
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_two_real_application_popups_require_review(site, monkeypatch):
    monkeypatch.setattr(generic, "POPUP_WAIT_MS", 500)
    monkeypatch.setitem(PAGES, "/ambiguous_popup", """<h1>Software Engineering Intern</h1>
        <button type="button" onclick="window.open('/apply');window.open('/apply_btn')">Apply Now</button>""")
    res, _ = run(site, monkeypatch, [click("Apply Now"), act("give_up")],
                 path="/ambiguous_popup", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert res.final_url == HOST + "/ambiguous_popup"
    assert not posted_thanks(site)


def test_late_popup_discards_stale_apply_action(site, monkeypatch):
    monkeypatch.setattr(generic, "POPUP_WAIT_MS", 100)
    monkeypatch.setitem(PAGES, "/late_popup", """<h1>Software Engineering Intern</h1>
        <button type="button" onclick="window.applyClicks=(window.applyClicks||0)+1;setTimeout(()=>window.open('/apply'),700)">
          Apply Now</button>""")
    site.page.goto(HOST + "/late_popup")
    job = Job("test:popup", "Acme", "Intern", HOST + "/late_popup", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={},
                            submit_mode="fill_only", dry_run=True)
    run_state = generic.Run(job, ctx, site.page, time.time() + 30)
    stale = generic._collect(run_state)
    target = next(c for c in stale.clickables if c.text == "Apply Now")
    first = generic.ADAPTER._execute(run_state, stale, "click", target.index, "open application")
    second = generic.ADAPTER._execute(run_state, stale, "click", target.index, "stale retry")
    assert first.status == second.status == ApplyStatus.NEEDS_REVIEW
    assert "unverified" in first.reason
    assert site.page.evaluate("window.applyClicks") is None
    assert run_state.page.url == HOST + "/late_popup"
    assert not posted_thanks(site)
    assert not run_state.submit_attempted


def test_fieldless_embedded_application_link_is_clickable(site, monkeypatch):
    res, fake = run(site, monkeypatch,
                    [click("Apply"), act("fill_form"), click("Submit application", action="submit")],
                    path="/framed_link_job", mode="fill_only")
    assert "fill_only" in res.reason, res.reason
    assert site.page.frames[1].url == HOST + "/apply"
    assert "First name" in fake.prompts[-1]
    assert not posted_thanks(site)


def test_embedded_job_content_precedes_outer_branding_in_prompt(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/iframe_branding", "<nav>" + "Brand navigation " * 400 + "</nav>"
                        + '<iframe src="/embedded_posting" width="800" height="500"></iframe>')
    monkeypatch.setitem(PAGES, "/embedded_posting", """<h1>Software Engineering Intern</h1>
        <p>Embedded role details</p><a href="/apply" target="_top">Apply for this job online</a>""")
    res, fake = run(site, monkeypatch, [click("Apply for this job online"), act("fill_form"),
                                       click("Submit application", action="submit")],
                    path="/iframe_branding", dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    assert "Embedded role details" in fake.prompts[0]
    assert "(embedded frame:" in fake.prompts[0]
    assert not posted_thanks(site)


def test_hidden_ancestor_frame_fields_are_ignored_until_visible(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    res, fake = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/frame_privacy", dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    assert all("First Name" not in prompt and "Last Name" not in prompt and "Hidden action" not in prompt
               for prompt in fake.prompts)
    assert not posted_thanks(site)

    site.page.locator("#profile").evaluate("el => el.style.visibility = 'visible'")
    job = Job("test:1", "Acme", "Intern", HOST + "/frame_privacy", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={}, submit_mode="full")
    state = generic._collect(generic.Run(job, ctx, site.page, time.time() + 30))
    labels = {field.label for field in state.fields if field.required}
    assert {"Email", "First Name", "Last Name"} <= labels
    assert any(c.text == "Hidden action" for c in state.clickables)


def test_inactive_icims_wizard_fields_are_not_filled_until_next_step(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    site.page.goto(HOST + "/icims_privacy")
    job = Job("test:1", "Acme", "Intern", HOST + "/icims_privacy", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={}, submit_mode="fill_only")
    run_state = generic.Run(job, ctx, site.page, time.time() + 30)

    assert {f.label for f in real_form.extract_fields(site.page)} >= {"Email", "First Name", "Last Name"}
    privacy = generic._collect(run_state)
    assert {f.label for f in privacy.fields} == {"Email", "I accept"}
    generic.ADAPTER._fill(run_state, privacy)
    assert site.page.locator("#em").input_value() == "ada@example.com"
    assert not site.page.locator("#fn").input_value()
    assert not run_state.failed_fields

    site.page.get_by_role("button", name="Next").click()
    profile = generic._collect(run_state)
    assert {"First Name", "Last Name", "Phone Number"} <= {f.label for f in profile.fields}
    generic.ADAPTER._fill(run_state, profile)
    assert site.page.locator("#fn").input_value() == "Ada"
    assert site.page.locator("#ln").input_value() == "Ada"
    assert not run_state.failed_fields


def test_create_account_fills_but_does_not_submit_ambiguous_form(site, monkeypatch, fakes):
    res, _ = run(site, monkeypatch, [act("create_account")], path="/account")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "manual review required" in res.reason
    assert fakes.vault.created == ["jobs.example.test"]
    assert site.page.locator('#e').input_value() == "ada+acme@example.com"
    assert not any(p == "/verify" for _, p, _ in site.hits)
    assert not posted_thanks(site)


@pytest.mark.parametrize("action", ["click", "submit"])
def test_account_button_with_existing_account_and_combined_form_stops(site, monkeypatch, action):
    site.page.goto(HOST + "/combined_account")
    job = Job("test:1", "Acme", "Intern", HOST + "/combined_account", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={}, submit_mode="full")
    run_state = generic.Run(job, ctx, site.page, time.time() + 30)
    run_state.account = Account("jobs.example.test", "ada@example.com", "password", "Acme", HOST)
    state = generic._collect(run_state)
    target = next(c for c in state.clickables if c.text == "Create Account")
    res = generic.ADAPTER._execute(run_state, state, action, target.index, "")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "manual review" in res.reason
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["create_account", "sign_in"])
def test_account_helper_does_not_submit_combined_form(site, monkeypatch, fakes, action):
    if action == "sign_in":
        fakes.vault.create("jobs.example.test", "Acme", HOST)
    res, _ = run(site, monkeypatch, [act(action)], path="/combined_account")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "manual review" in res.reason
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_verification_code_advances_otp_only_and_does_not_auto_submit(site, monkeypatch, fakes):
    res, _ = run(site, monkeypatch, [act("need_email_code")], path="/verify")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert any(path == "/apply" and "code=123456" in query for _, path, query in site.hits)
    assert fakes.mail.calls[0]["want"] == "code"
    assert fakes.mail.calls[0]["consume"] is False
    assert fakes.mail.acknowledged == ["m1"]
    assert fakes.mail.calls[0]["sender_hint"] == "example"
    assert time.time() - fakes.mail.calls[0]["after_ts"] < 90
    assert not posted_thanks(site)
    assert fakes.vault.verified == []
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_verification_otp_boxes_cannot_overwrite_first_name_or_submit(site, monkeypatch, fakes):
    res, _ = run(site, monkeypatch, [act("need_email_code")], path="/verify_boxes")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert [site.page.locator(f'input[name="d{i}"]').input_value() for i in range(6)] == list("123456")
    assert site.page.locator('input[name="first_name"]').input_value() == ""
    assert fakes.mail.calls and fakes.mail.calls[0]["want"] == "code"
    assert not any(path == "/otp_done" for _, path, _ in site.hits)
    assert not posted_thanks(site)


def test_verification_otp_only_form_clicks_verify_and_advances(site, monkeypatch, fakes):
    res, _ = run(site, monkeypatch, [act("need_email_code"), act("give_up")], path="/oracle_verify")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert any(path == "/otp_done" and all(f"d{i}={i+1}" in query for i in range(6))
               for _, path, query in site.hits)
    assert fakes.mail.calls[0]["want"] == "code"
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_incomplete_split_otp_never_acknowledges_or_clicks_verify(site, monkeypatch, fakes):
    monkeypatch.setitem(PAGES, "/dropping_digit", PAGES["/oracle_verify"] + """<script>
        document.querySelector('[name=d3]').oninput=e=>{e.target.value=''};
        </script>""")
    res, _ = run(site, monkeypatch, [act("need_email_code")], path="/dropping_digit", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "message not consumed" in res.reason
    assert fakes.mail.acknowledged == []
    assert len(fakes.mail.calls) == 1 and fakes.mail.calls[0]["consume"] is False
    assert not any(path == "/otp_done" for _, path, _ in site.hits)
    assert not posted_thanks(site)


def test_verification_code_in_iframe_advances_inside_iframe(site, monkeypatch, fakes):
    res, _ = run(site, monkeypatch, [act("need_email_code")], path="/verify_framed")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert any(path == "/apply" and "code=123456" in query for _, path, query in site.hits)
    assert not posted_thanks(site)


@pytest.mark.parametrize("event", ["input", "keyup"])
def test_controlled_split_otp_is_reacquired_and_verified_before_acknowledgement(site, monkeypatch, fakes, event):
    body = """<h1>Confirm Your Identity</h1>
        <form action="/otp_done" id="verify"><div id="digits"></div><button type="submit" disabled>Verify</button></form>
        <script>
        const values=Array(6).fill('');
        function render() {
          document.getElementById('digits').innerHTML=values.map((v,i)=>
            `<input maxlength="1" name="d${i}" aria-label="Digit ${i+1}" value="${v}">`).join('');
          document.querySelectorAll('#digits input').forEach((el,i)=>el.oninput=()=>{
            values[i]=el.value; render();
          });
          document.querySelector('button').disabled=values.some(v=>!v);
        }
        render();
        </script>"""
    monkeypatch.setitem(PAGES, "/controlled_otp", body.replace("el.oninput", "el.on" + event))
    acknowledge = fakes.mail.acknowledge_verification

    def confirmed_entry(verification, *, want):
        assert [site.page.locator(f'[name=d{i}]').input_value() for i in range(6)] == list(fakes.mail.code)
        assert not any(path == "/otp_done" for _, path, _ in site.hits)
        acknowledge(verification, want=want)

    monkeypatch.setattr(fakes.mail, "acknowledge_verification", confirmed_entry)
    res, fake = run(site, monkeypatch, [act("need_email_code"), act("give_up")],
                    path="/controlled_otp", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert fakes.mail.acknowledged == ["m1"]
    assert any(path == "/otp_done" and all(f"d{i}={i+1}" in query for i in range(6))
               for _, path, query in site.hits)
    assert all(fakes.mail.code not in prompt for prompt in fake.prompts)
    assert not posted_thanks(site)


@pytest.fixture
def deferred_mail(monkeypatch):
    from agent import mail as real_mail

    messages, queries = [], []
    monkeypatch.setattr(generic, "mail", real_mail)
    monkeypatch.setattr(real_mail, "available", lambda: True)
    monkeypatch.setattr(real_mail.llm, "available", lambda: False)
    monkeypatch.setattr(real_mail, "POLL_SECONDS", 0.002)
    monkeypatch.setattr(real_mail, "_gmail_list", lambda query: queries.append(query) or list(messages))
    return SimpleNamespace(module=real_mail, messages=messages, queries=queries)


@pytest.mark.parametrize("retry", ["repair", "expired", "resend"])
def test_partial_otp_is_not_consumed_and_retry_rechecks_mail(site, monkeypatch, deferred_mail, retry, caplog):
    monkeypatch.setitem(PAGES, "/partial_otp", """<h1>Confirm Your Identity</h1>
        <form action="/otp_done"><label for="code">Verification code</label>
        <input id="code" name="code" oninput="this.value=this.value.slice(0,3)">
        <button type="submit">Verify</button>
        <button type="button" onclick="fetch('/request_new_code',{method:'POST'})">Resend code</button></form>""")
    site.page.goto(HOST + "/partial_otp")
    now = time.time()
    clock = [now]
    monkeypatch.setattr(generic, "time", SimpleNamespace(time=lambda: clock[0], strftime=time.strftime))
    monkeypatch.setattr(deferred_mail.module, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep))
    msg = deferred_mail.module._Msg("fresh-otp", "Verification code", "jobs@example.test", "ada@example.com",
                                    now, "Your verification code is 123456. It expires in 60 seconds.", "")
    deferred_mail.messages.append(msg)
    job = Job("test:otp", "Acme", "Intern", HOST + "/partial_otp", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx,
                            config={"email": {"verification_timeout_seconds": 0.08}},
                            profile={"personal": {"email": "ada@example.com"}},
                            submit_mode="fill_only", dry_run=True)
    run_state = generic.Run(job, ctx, site.page, now + 300, started_ts=now - 600, otp_requested_ts=now)
    with caplog.at_level("INFO"):
        res = generic.ADAPTER._verify(run_state, generic._collect(run_state), "code")
    assert res.status == ApplyStatus.NEEDS_REVIEW and "message not consumed" in res.reason
    assert not db.mail_is_seen(msg.id)
    assert not run_state.otp_entered_request_ts
    assert not any(path == "/otp_done" for _, path, _ in site.hits)
    assert "123456" not in caplog.text
    site.page.locator("#code").evaluate("el => el.removeAttribute('oninput')")

    if retry == "expired":
        clock[0] += 61
    elif retry == "resend":
        clock[0] += 40
        state = generic._collect(run_state)
        resend = next(c for c in state.clickables if c.text == "Resend code")
        generic.ADAPTER._execute(run_state, state, "click", resend.index, "request fresh code")
        assert run_state.otp_requested_ts == clock[0]
        deferred_mail.messages.append(deferred_mail.module._Msg(
            "resent-otp", "Verification code", "jobs@example.test", "ada@example.com", clock[0],
            "Your verification code is 654321. It expires in 60 seconds.", ""))

    res = generic.ADAPTER._verify(run_state, generic._collect(run_state), "code")
    if retry == "expired":
        assert res.status == ApplyStatus.NEEDS_REVIEW and "no verification code" in res.reason
        assert not db.mail_is_seen(msg.id)
        assert not any(path == "/otp_done" for _, path, _ in site.hits)
    else:
        expected = "654321" if retry == "resend" else "123456"
        assert res is None
        assert any(path == "/otp_done" and f"code={expected}" in query for _, path, query in site.hits)
        assert db.mail_is_seen("resent-otp" if retry == "resend" else msg.id)
        if retry == "resend":
            assert not db.mail_is_seen(msg.id)
        assert deferred_mail.queries[-1].startswith(f"after:{int(run_state.otp_requested_ts)}")
    assert not run_state.submit_attempted
    assert not posted_thanks(site)


def test_completed_otp_that_does_not_advance_is_not_polled_again(site, monkeypatch, fakes):
    monkeypatch.setitem(PAGES, "/stalled_otp", """<h1>Verification code</h1>
        <form><input name="code"><button type="button">Verify</button></form>""")
    res, _ = run(site, monkeypatch, [act("need_email_code"), act("need_email_code")],
                 path="/stalled_otp", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "request a fresh code" in res.reason
    assert len(fakes.mail.calls) == 1
    assert fakes.mail.acknowledged == ["m1"]
    assert not posted_thanks(site)


def test_verification_mail_cutoff_tracks_request_and_resend(site, monkeypatch, fakes):
    monkeypatch.setitem(PAGES, "/request_code", """<h1>Verify your email</h1>
        <button type="button" onclick="location.href='/code_resend'">Send verification code</button>""")
    monkeypatch.setitem(PAGES, "/code_resend", """<h1>Check your email</h1><p>Enter the verification code</p>
        <input id="code" name="code"><button type="button" onclick="fetch('/request_new_code', {method:'POST'})">Resend code</button>""")
    now = time.time()
    clock = [now]
    monkeypatch.setattr(generic, "time", SimpleNamespace(time=lambda: clock[0], strftime=time.strftime))
    site.page.goto(HOST + "/request_code")
    job = Job("test:otp", "Acme", "Intern", HOST + "/request_code", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={},
                            submit_mode="fill_only", dry_run=True)
    run_state = generic.Run(job, ctx, site.page, now + 300, started_ts=now - 600, account_ts=now - 900)
    state = generic._collect(run_state)
    send = next(c for c in state.clickables if c.text == "Send verification code")
    generic.ADAPTER._execute(run_state, state, "click", send.index, "request email code")
    state = generic._collect(run_state)
    assert run_state.otp_requested_ts == now

    clock[0] += 40
    generic.ADAPTER._verify(run_state, state, "code")
    assert fakes.mail.calls[-1]["after_ts"] == now

    state = generic._collect(run_state)
    resend = next(c for c in state.clickables if c.text == "Resend code")
    generic.ADAPTER._execute(run_state, state, "click", resend.index, "request fresh code")
    state = generic._collect(run_state)
    assert run_state.otp_requested_ts == now + 40
    clock[0] += 20
    generic.ADAPTER._verify(run_state, state, "code")
    assert fakes.mail.calls[-1]["after_ts"] == now + 40
    assert any(method == "POST" and path == "/request_new_code" for method, path, _ in site.hits)
    assert not posted_thanks(site)
    assert not run_state.submit_attempted


def test_verification_without_observed_request_does_not_use_older_account_time(site, monkeypatch, fakes):
    site.page.goto(HOST + "/code_resend")
    site.page.set_content('<h1>Verification code</h1><input name="code">')
    now = time.time()
    job = Job("test:otp", "Acme", "Intern", HOST + "/verify", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={},
                            submit_mode="fill_only", dry_run=True)
    run_state = generic.Run(job, ctx, site.page, now + 30, started_ts=now, account_ts=now - 900)
    generic.ADAPTER._verify(run_state, generic._collect(run_state), "code")
    assert fakes.mail.calls[-1]["after_ts"] == now
    assert not run_state.submit_attempted


def test_verification_missing_code_input_never_reads_mail_or_touches_name(site, monkeypatch, fakes):
    res, _ = run(site, monkeypatch, [act("need_email_code")], path="/verify_wrong_input")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "code field not found" in res.reason
    assert site.page.locator("#fn").input_value() == ""
    assert fakes.mail.calls == []
    assert not posted_thanks(site)


def test_verification_link_requires_review(site, monkeypatch, fakes):
    monkeypatch.setattr(fakes.mail, "wait_for_verification", lambda *a, **kw: SimpleNamespace(
        code=None, link=HOST + "/thanks", message_id="m2", subject="Verify", sender="no-reply"))
    res, _ = run(site, monkeypatch, [act("need_email_link")], path="/verify")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "manual review" in res.reason
    assert not any(path == "/thanks" for _, path, _ in site.hits)
    assert fakes.vault.verified == []


@pytest.mark.parametrize("action", ["click", "submit"])
def test_combined_account_and_application_submit_remains_gated(site, monkeypatch, action):
    script = [act("fill_form"), click("Create account and submit application", action=action)]
    res, _ = run(site, monkeypatch, script, path="/account_and_submit", dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_create_account_label_without_password_form_still_uses_submit_gate(site, monkeypatch):
    res, _ = run(site, monkeypatch, [act("fill_form"), click("Create Account")],
                 path="/application_create_account", dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    assert not posted_thanks(site)


def test_direct_account_click_requires_account_setup(site, monkeypatch):
    res, _ = run(site, monkeypatch, [click("Create Account")], path="/account_pending")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "manual review required" in res.reason
    assert not any(path == "/verify" for _, path, _ in site.hits)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


@pytest.mark.parametrize("path,label", [("/apply_btn", "Apply"), ("/go_btn", "Go")])
def test_final_button_with_plain_label_goes_through_submit_gate(site, monkeypatch, path, label):
    res, _ = run(site, monkeypatch, [act("fill_form"), click(label)], path=path, dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    assert not posted_thanks(site)
    res, _ = run(site, monkeypatch, [act("fill_form"), click(label)], path=path, mode="fill_only")
    assert res.status == ApplyStatus.NEEDS_REVIEW and not posted_thanks(site)


def test_apply_now_outside_form_is_gated(site, monkeypatch):
    res, _ = run(site, monkeypatch, [act("fill_form"), click("Apply now")], path="/react_form", dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    site.page.wait_for_timeout(300)
    assert not posted_thanks(site)


def test_submit_link_on_landing_page_just_navigates(site, monkeypatch):
    script = [click("Submit your resume"), act("fill_form"), click("Submit application", action="submit")]
    res, _ = run(site, monkeypatch, script, path="/landing")
    assert res.status == ApplyStatus.SUBMITTED, res.reason
    assert any(p == "/apply" for _, p, _ in site.hits)
    assert "first_name=Ada" in next(q for m, p, q in site.hits if p == "/thanks")


def test_dry_run_does_not_create_accounts(site, monkeypatch, fakes):
    res, _ = run(site, monkeypatch, [act("create_account")], path="/account", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "account required" in res.reason
    assert fakes.vault.created == []
    assert not any(p == "/verify" for _, p, _ in site.hits)


def test_done_without_submit_is_not_success(site, monkeypatch):
    res, _ = run(site, monkeypatch, [act("done", "looks finished")], path="/thanks")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert not posted_thanks(site)


def test_file_guard_only_uploads_resume():
    fields = [Field("a", "Resume/CV", "file", True), Field("b", "Cover letter", "file", False)]
    ans = [Answer("a", "/tmp/other.pdf", "generated"), Answer("b", "/tmp/cover.pdf", "generated")]
    out = generic._guard_files(fields, ans, {"resume_path": "/tmp/resume.pdf"})
    assert out[0].value == "/tmp/resume.pdf" and out[0].source == "profile"
    assert out[1].value is None and out[1].source == "unknown"


def test_file_guard_never_uses_resume_for_non_resume_input():
    fields = [Field("cover", "Cover letter", "file", False),
              Field("extra", "Additional files", "file", False)]
    ans = [Answer(f.key, "/tmp/resume.pdf", "profile") for f in fields]
    guarded = generic._guard_files(fields, ans, {"resume_path": "/tmp/resume.pdf"})
    assert all(a.value is None and a.source == "unknown" for a in guarded)


@pytest.mark.parametrize("host", [HOST, "https://test.clearcompany.com"])
def test_resume_menu_upload_is_scoped(site, monkeypatch, tmp_path, host):
    resume = tmp_path / "candidate_resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    result, fake = run(
        site, monkeypatch, [click("Upload"), act("give_up", "test reached after resume")],
        path="/clearcompany_resume", host_override=host,
        profile_override={"resume_path": str(resume)}, dry_run=True)
    assert result.status == ApplyStatus.NEEDS_REVIEW
    assert "test reached after resume" in result.reason
    assert site.page.locator("#resume-file").evaluate("el => el.files[0].name") == resume.name
    assert site.page.locator("#resume-name").inner_text() == resume.name
    assert site.page.locator("#cover-file").evaluate("el => el.files.length") == 0
    assert site.page.locator("#extra-file").evaluate("el => el.files.length") == 0
    assert len(fake.prompts) == 2


def test_hidden_resume_file_input_is_labeled_from_its_section(site, monkeypatch, tmp_path):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    resume = tmp_path / "candidate_resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    result, _ = run(site, monkeypatch, [act("fill_form"), act("give_up", "prefilled")],
                    path="/clearcompany_resume", dry_run=True, profile_override={"resume_path": str(resume)})
    assert result.status == ApplyStatus.NEEDS_REVIEW and "prefilled" in result.reason, result.reason
    assert site.page.locator("#resume-file").evaluate("el => el.files[0].name") == resume.name
    assert site.page.locator("#cover-file").evaluate("el => el.files.length") == 0
    assert site.page.locator("#extra-file").evaluate("el => el.files.length") == 0
    assert any(a.value == str(resume) and a.source == "profile" for a in result.answers)


def test_clearcompany_cover_letter_upload_does_not_receive_resume(site, monkeypatch, tmp_path):
    resume = tmp_path / "candidate_resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")

    def click_cover(prompt):
        found = re.findall(r"^\[(\d+)\] <button> Upload(?: \(.*)?$", prompt, re.M)
        assert len(found) == 3
        return {"action": "click", "click_index": int(found[1]), "reason": "open cover letter"}

    result, _ = run(site, monkeypatch, [click_cover, act("give_up")], path="/clearcompany_resume",
                    host_override="https://test.clearcompany.com",
                    profile_override={"resume_path": str(resume)}, dry_run=True)
    assert result.status == ApplyStatus.NEEDS_REVIEW
    assert site.page.locator("#cover-menu").is_visible()
    assert site.page.locator("#resume-file").evaluate("el => el.files.length") == 0
    assert site.page.locator("#cover-file").evaluate("el => el.files.length") == 0


@pytest.mark.parametrize("preference", [None, True])
@pytest.mark.parametrize("host", [HOST, "https://test.clearcompany.com"])
def test_sms_declined_without_preferring_opt_in(site, monkeypatch, preference, host):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    profile = {"preferences": {"sms_opt_in": preference}} if preference is not None else {}
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/clearcompany_sms", host_override=host, dry_run=True,
                    profile_override=profile)
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert site.page.locator('input[name="sms-opt-in"][value="no"]').is_checked()
    assert not site.page.locator('input[name="sms-opt-in"][value="yes"]').is_checked()
    assert not posted_thanks(site)


def test_explicit_application_submit_confirms_when_page_context_is_sparse(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)

    def check_intent_before_post(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1
        route.fallback()

    site.ctx.route(HOST + "/thanks", check_intent_before_post)
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/clearcompany_sms")
    assert result.status == ApplyStatus.SUBMITTED, result.reason
    assert posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_messaging_defaults_and_direct_yes_click_both_decline(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    monkeypatch.setitem(PAGES, "/messages", """<h1>Application</h1><form method="post" action="/thanks">
        <label for="email">Email</label><input id="email" type="email" required>
        <fieldset><legend>Would you like to receive company news via email?</legend>
          <label><input type="radio" name="emailOptIn" value="yes" checked>Yes</label>
          <label><input type="radio" name="emailOptIn" value="no">No</label></fieldset>
        <fieldset><legend>We may use SMS during the hiring process. Do you give us permission to text you?</legend>
          <label><input type="radio" name="smsPermission" value="yes" checked>Yes</label>
          <label><input type="radio" name="smsPermission" value="no">No</label></fieldset>
        <label><input id="sms" type="checkbox" checked>I consent to receive SMS messages</label>
        <label><input id="whatsapp" type="checkbox" checked>I want to receive messages via WhatsApp</label>
        <fieldset><legend>May we use your information for additional job opportunities and communicate with you?
          You may unsubscribe from emails at any time.</legend>
          <label><input type="radio" name="futureOpportunitiesConsent" value="yes" checked>Yes</label>
          <label><input type="radio" name="futureOpportunitiesConsent" value="no">No</label></fieldset>
        <button type="submit">Submit application</button></form>""")
    result, _ = run(site, monkeypatch, [click("Yes"), act("fill_form"), click("Submit application", action="submit")],
                    path="/messages", dry_run=True)
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert site.page.locator('input[name="emailOptIn"][value="no"]').is_checked()
    assert site.page.locator('input[name="smsPermission"][value="no"]').is_checked()
    assert site.page.locator('input[name="futureOpportunitiesConsent"][value="no"]').is_checked()
    assert not site.page.locator("#sms").is_checked()
    assert not site.page.locator("#whatsapp").is_checked()
    assert not posted_thanks(site)


def test_profile_reuse_consent_defaults_to_current_role_only():
    field = Field("reuse", "Use my profile data for ANY ROLES GLOBALLY", "radio", False, options=[
        "Use my profile data for ANY ROLES GLOBALLY, including roles I have not applied to",
        "Use my profile data for ONLY THE ROLE I AM APPLYING FOR",
    ])
    result = generic._guard_choices([field], [Answer("reuse", None, "unknown")], {})
    assert result[0].value == "Use my profile data for ONLY THE ROLE I AM APPLYING FOR"
    assert result[0].source == "profile"
    assert result[0].note == "data use scoped to this application"


def test_fatal_server_error_stops_without_calling_vision(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/server_error", """<main><h1>Page Error</h1>
        <p>Error: An unexpected server error occurred. Please try again later.</p></main>""")
    result, fake = run(site, monkeypatch, [], path="/server_error", dry_run=True)
    assert result.status == ApplyStatus.NEEDS_REVIEW
    assert "server error" in result.reason
    assert fake.prompts == []


def test_visible_aria_hidden_dialog_is_actionable_and_hides_blocked_fields(site, monkeypatch, tmp_path):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    monkeypatch.setitem(PAGES, "/visible_dialog", '''
      <div id="screen" style="position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:100"></div>
      <div role="dialog" aria-hidden="true" style="position:fixed;inset:10px;z-index:101;background:white">
        <h2>Resume</h2>
        <input id="resume-file" type="file" style="display:none"
          onchange="document.querySelector('[role=dialog]').style.display='none';document.getElementById('screen').style.display='none'">
        <button onclick="document.getElementById('resume-file').click()">Upload Resume</button>
      </div>
      <form method="post" action="/thanks">
        <label for="fn">First name</label><input id="fn" name="first_name" required>
        <label for="em">Email</label><input id="em" name="email" type="email" required>
        <button type="submit">Submit application</button>
      </form>''')
    resume = tmp_path / "configured-resume.pdf"
    resume.write_bytes(b"resume fixture")
    result, fake = run(site, monkeypatch,
                       [click("Upload Resume"), act("fill_form"), click("Submit application", action="submit")],
                       path="/visible_dialog", dry_run=True,
                       profile_override={"resume_path": str(resume)})
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert site.page.locator("#resume-file").evaluate("e => e.files[0].name") == resume.name
    assert not site.page.get_by_role("dialog").is_visible()
    assert "Upload Resume" in fake.prompts[0]
    assert not re.search(r"\[\d+\] <button> Submit application", fake.prompts[0])
    assert not posted_thanks(site)


def test_optional_messaging_with_no_decline_choice_blocks_readiness(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    monkeypatch.setitem(PAGES, "/message_no_decline", """<h1>Application</h1><form method="post" action="/thanks">
        <label for="email">Email</label><input id="email" type="email" required>
        <fieldset><legend>May we send you SMS messages?</legend>
          <label><input type="radio" name="sms" value="yes" checked>Yes</label></fieldset>
        <button type="submit">Submit application</button></form>""")
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/message_no_decline", dry_run=True)
    assert result.status == ApplyStatus.NEEDS_REVIEW
    assert "messaging opt-out" in result.reason
    assert not posted_thanks(site)


def test_checkbox_proxies_keep_required_privacy_and_decline_messages(site, monkeypatch):
    from agent import answers as real_answers, form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    monkeypatch.setattr(generic, "answers", real_answers)
    monkeypatch.setitem(PAGES, "/checkbox_proxies", """<h1>Application</h1><form method="post" action="/thanks">
        <label for="email">Email</label><input id="email" type="email" required>
        <input id="privacy" type="checkbox" style="opacity:0" required>
        <label for="privacy">I agree to the privacy policy</label>
        <input id="sms" type="checkbox" style="opacity:0" checked>
        <label for="sms">I consent to receive SMS messages</label>
        <button type="submit">Submit application</button></form>""")
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/checkbox_proxies", dry_run=True,
                    profile_override={"personal": {"email": "ada@example.com"}})
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert site.page.locator("#privacy").is_checked()
    assert not site.page.locator("#sms").is_checked()
    assert not posted_thanks(site)


def test_dropdown_click_uses_explicit_employment_history(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    result, _ = run(site, monkeypatch, [click("Please select"), act("fill_form"),
                                       click("Submit application", action="submit")],
                    path="/employment_choice", dry_run=True,
                    profile_override={"preferences": {"previously_employed_here": "No"}})
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert site.page.locator("#history-choice").inner_text() == "I am NOT a current or former employee"
    assert not result.unanswered_required
    assert not posted_thanks(site)


def test_dropdown_click_cannot_guess_employment_history(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    FakeAnswers.unknown_labels = {"Have you ever been employed by this company?"}
    result, _ = run(site, monkeypatch, [click("Please select")], path="/employment_choice", dry_run=True)
    assert result.status == ApplyStatus.NEEDS_REVIEW
    assert "verified answer" in result.reason
    assert site.page.locator("#history-choice").inner_text() == "Please select"
    assert not posted_thanks(site)


def test_country_combo_displaying_only_code_is_verified(site, monkeypatch):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    body = PAGES["/employment_choice"].replace("Have you ever been employed by this company?", "Country Code")
    body = body.replace("I am NOT a current or former employee", "United States (+1)")
    body = body.replace("I am a current employee", "United Kingdom (+44)")
    body = body.replace("I am a former employee", "Canada (+1)")
    body = body.replace("combo.textContent = el.textContent;", "combo.textContent = el.textContent.match(/\\+\\d+/)[0];")
    monkeypatch.setitem(PAGES, "/phone_country", body)
    original_answers = FakeAnswers.answer_fields

    def answer_fields(fields, *args, **kw):
        return [Answer(a.key, "United States (+1)", "profile")
                if next(f for f in fields if f.key == a.key).label == "Country Code" else a
                for a in original_answers(fields, *args, **kw)]

    monkeypatch.setattr(FakeAnswers, "answer_fields", answer_fields)
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/phone_country", dry_run=True)
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert site.page.locator("#history-choice").inner_text() == "+1"
    assert not result.unanswered_required
    assert not posted_thanks(site)


@pytest.mark.parametrize("changed,timing", [("school", "fill"), ("month", "submit"), ("year", "submit")])
def test_dropdown_dom_mismatch_blocks_readiness(site, monkeypatch, changed, timing):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    monkeypatch.setitem(PAGES, "/verified_choices", """<h1>Application</h1><form action="/thanks" method="post">
        <label for="email">Email</label><input id="email" type="email" required>
        <label for="school">School</label><select id="school" required>
          <option value="">Select</option><option>Stony Brook University</option><option>Aston University</option></select>
        <label for="month">Graduation month</label><select id="month" required>
          <option value="">Select</option><option>May</option><option>January</option></select>
        <label for="year">Graduation year</label><select id="year">
          <option value="">Select</option><option>2028</option><option>2026</option></select>
        <button type="submit">Submit application</button></form>""")
    intended = {"Email": "ada@example.com", "School": "Stony Brook University",
                "Graduation month": "May", "Graduation year": "2028"}
    wrong = {"school": "Aston University", "month": "January", "year": "2026"}
    monkeypatch.setattr(FakeAnswers, "answer_fields", lambda fields, *a, **kw: [
        Answer(f.key, intended[f.label], "profile") for f in fields])
    if timing == "fill":
        fill = real_form.fill_fields

        def silently_wrong(scope, fields, answers):
            fill(scope, fields, answers)
            scope.locator("#" + changed).select_option(label=wrong[changed])
            return []

        monkeypatch.setattr(real_form, "fill_fields", silently_wrong)

    def final_action(prompt):
        if timing == "submit":
            site.page.locator("#" + changed).select_option(label=wrong[changed])
        return click("Submit application", action="submit")(prompt)

    res, _ = run(site, monkeypatch, [act("fill_form"), final_action],
                 path="/verified_choices", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "selected values do not match" in res.reason
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


@pytest.mark.parametrize("matching", [True, False])
def test_prior_resume_dropdown_uses_only_configured_filename(site, monkeypatch, tmp_path, matching):
    from agent import form as real_form

    monkeypatch.setattr(generic, "form", real_form)
    resume = tmp_path / "candidate_resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    option = f'<option value="current">{resume.name}</option>' if matching else ""
    monkeypatch.setitem(PAGES, "/prior_resume", f"""<h1>Application</h1><form method="post" action="/thanks">
        <label for="email">Email</label><input id="email" type="email" required>
        <label for="prior">Choose a previously uploaded resume</label><select id="prior" required>
          <option value="">Please select</option><option value="old">old_resume.pdf</option>{option}</select>
        <button type="submit">Submit application</button></form>""")
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/prior_resume", dry_run=True, profile_override={"resume_path": str(resume)})
    assert result.status == (ApplyStatus.DRY_RUN_OK if matching else ApplyStatus.NEEDS_REVIEW), result.reason
    assert site.page.locator("#prior").input_value() == ("current" if matching else "")
    assert not posted_thanks(site)


def test_nested_cross_origin_apply_link_survives_outer_control_limit(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/crowded", """<h1>Careers</h1>"""
                        + "".join(f'<a href="/unused/{i}">Brand link {i}</a>' for i in range(100))
                        + '<iframe src="https://test.clearcompany.com/portal" width="800" height="500"></iframe>')
    monkeypatch.setitem(PAGES, "/portal", f'<iframe src="{HOST}/framed_link_only" width="700" height="400"></iframe>')
    result, fake = run(site, monkeypatch, [click("Apply"), act("fill_form"),
                                          click("Submit application", action="submit")],
                        path="/crowded", mode="fill_only")
    assert result.status == ApplyStatus.NEEDS_REVIEW and "fill_only" in result.reason, result.reason
    assert "Apply" in fake.prompts[0]
    assert any(fr.url == HOST + "/apply" for fr in site.page.frames)
    assert not posted_thanks(site)


def test_oracle_style_posting_apply_label_navigates_without_submit_intent(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/oracle_posting", """<h1>Software Engineering Intern</h1><p>Summer 2027</p>
        <button onclick="location.href='/apply'">Apply for this job</button>""")
    result, _ = run(site, monkeypatch, [click("Apply for this job", action="submit"), act("fill_form"),
                                       click("Submit application", action="submit")],
                    path="/oracle_posting", dry_run=True)
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert site.page.url == HOST + "/apply"
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_choice_list_changes_do_not_trigger_false_loop(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/choice_progress", """<h1>Careers</h1>"""
                        + "".join(f'<button type="button">Control {i}</button>' for i in range(20))
                        + """<button type="button" onclick="this.textContent='Show application';this.onclick=()=>location.href='/apply'">
                          Open section</button>""")
    result, _ = run(site, monkeypatch, [click("Open section"), click("Show application"), act("fill_form"),
                                       click("Submit application", action="submit")],
                    path="/choice_progress", dry_run=True)
    assert result.status == ApplyStatus.DRY_RUN_OK, result.reason
    assert not posted_thanks(site)


@pytest.mark.parametrize("path,label,action,dry_run,mode", [
    ("/final_next", "Next", "click", True, "full"),
    ("/final_continue", "Continue", "submit", True, "full"),
    ("/final_continue", "Continue", "click", False, "fill_only"),
    ("/final_next_aria", "Next", "click", True, "full"),
    ("/final_next_aria", "Next", "click", False, "full"),
    ("/final_next_conflict", "Next", "click", True, "full"),
    ("/misleading_step", "Next", "click", True, "full"),
    ("/misleading_step", "Next", "submit", False, "fill_only"),
    ("/misleading_step", "Next", "click", False, "full"),
    ("/final_next", "Next", "submit", False, "full"),
    ("/final_continue", "Continue", "click", False, "full"),
])
def test_terminal_advance_requires_confirmation_and_persists_intent(site, monkeypatch, path, label, action, dry_run, mode):
    if not dry_run and mode == "full":
        def check_intent_before_post(route):
            with db.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1
            route.fallback()
        site.ctx.route(HOST + "/thanks", check_intent_before_post)
    res, _ = run(site, monkeypatch, [act("fill_form"), click(label, action=action)],
                 path=path, dry_run=dry_run, mode=mode)
    if not dry_run and mode == "full":
        assert res.status == ApplyStatus.SUBMITTED, res.reason
        assert posted_thanks(site)
    else:
        assert res.status == (ApplyStatus.DRY_RUN_OK if dry_run else ApplyStatus.NEEDS_REVIEW), res.reason
        assert not posted_thanks(site)
    with db.connect() as conn:
        expected_intent = int(not dry_run and mode == "full")
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == expected_intent


@pytest.mark.parametrize("path,label,action", [
    ("/wizard1", "Next", "click"), ("/wizard1", "Next", "submit"),
    ("/wizard_continue1", "Continue", "click"),
])
@pytest.mark.parametrize("dry_run,mode", [(True, "full"), (False, "fill_only")])
def test_numbered_wizard_cannot_advance_in_restricted_modes(site, monkeypatch, path, label, action, dry_run, mode):
    res, _ = run(site, monkeypatch, [act("fill_form"), click(label, action=action)],
                 path=path, dry_run=dry_run, mode=mode)
    assert res.status == (ApplyStatus.DRY_RUN_OK if dry_run else ApplyStatus.NEEDS_REVIEW), res.reason
    assert not any(p == "/wizard2" for _, p, _ in site.hits)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_wizard_does_not_advance_with_missing_required_answer(site, monkeypatch):
    FakeAnswers.unknown_labels = {"First name"}
    res, _ = run(site, monkeypatch, [act("fill_form"), click("Next")], path="/wizard1")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert not any(path == "/wizard2" for _, path, _ in site.hits)


def test_wizard_navigation_does_not_consume_final_submit_intent(site, monkeypatch):
    def check_step_intent(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0
        route.fallback()

    def check_final_intent(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1
        route.fallback()

    site.ctx.route(HOST + "/wizard2", check_step_intent)
    site.ctx.route(HOST + "/thanks", check_final_intent)
    script = [act("fill_form"), click("Next", action="submit"), act("fill_form"), click("Submit application", action="submit")]
    res, _ = run(site, monkeypatch, script, path="/wizard1")
    assert res.status == ApplyStatus.SUBMITTED, res.reason
    assert any(m == "POST" and p == "/wizard2" for m, p, _ in site.hits)
    assert posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_consent_submit_advances_without_consuming_application_submit(site, monkeypatch):
    def check_no_submit_intent(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0
        route.fallback()

    def check_final_intent(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1
        route.fallback()

    site.ctx.route(HOST + "/consent_saved", check_no_submit_intent)
    site.ctx.route(HOST + "/thanks", check_final_intent)
    script = [click("Submit", action="submit"), act("fill_form"), click("Submit application", action="submit")]
    result, _ = run(site, monkeypatch, script, path="/application_consent")
    assert result.status == ApplyStatus.SUBMITTED, result.reason
    assert any(method == "POST" and path == "/consent_saved" for method, path, _ in site.hits)
    assert posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_final_submit_without_confirmation_is_uncertain_and_never_retried(site, monkeypatch):
    def check_intent_before_post(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1
        route.fallback()

    site.ctx.route(HOST + "/blank_thanks", check_intent_before_post)
    monkeypatch.setitem(PAGES, "/no_confirmation", """<h1>Application</h1>
        <form method="post" action="/blank_thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <label for="em">Email</label><input id="em" type="email" name="email" required>
          <button type="submit">Submit application</button></form>""")
    result, fake = run(site, monkeypatch,
                       [act("fill_form"), click("Submit application", action="submit"), act("done")],
                       path="/no_confirmation")
    assert result.status == ApplyStatus.NEEDS_REVIEW, result.reason
    assert "no confirmation" in result.reason or "not observed" in result.reason
    assert sum(method == "POST" and path == "/blank_thanks" for method, path, _ in site.hits) == 1
    assert len(fake.prompts) == 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_terminal_continue_without_confirmation_is_uncertain_and_never_retried(site, monkeypatch):
    monkeypatch.setitem(PAGES, "/thanks", "<h1>Information received.</h1>")
    monkeypatch.setitem(PAGES, "/terminal_continue", """<h1>Application</h1>
        <form method="post" action="/thanks">
          <label for="fn">First name</label><input id="fn" name="first_name" required>
          <label for="em">Email</label><input id="em" type="email" name="email" required>
          <button type="submit">Continue</button></form>""")

    def check_intent_before_post(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1
        route.fallback()

    site.ctx.route(HOST + "/thanks", check_intent_before_post)
    result, _ = run(site, monkeypatch,
                    [act("fill_form"), click("Continue", action="submit"), act("done")],
                    path="/terminal_continue")
    assert result.status == ApplyStatus.NEEDS_REVIEW, result.reason
    assert "no confirmation" in result.reason or "not observed" in result.reason
    assert sum(method == "POST" and path == "/thanks" for method, path, _ in site.hits) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_final_click_exception_after_dispatch_does_not_retry(site, monkeypatch):
    click_attempts = []

    def dispatch_then_report_uncertain(run_state, target, *, allow_dom_fallback=True):
        click_attempts.append(allow_dom_fallback)
        target.frame.locator(f'[data-aa-click="{target.index}"]').first.evaluate("el => el.click()")
        return False

    monkeypatch.setattr(generic, "_click", dispatch_then_report_uncertain)
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/apply")
    assert click_attempts == [False]
    assert result.status == ApplyStatus.SUBMITTED, result.reason
    assert sum(method == "POST" and path == "/thanks" for method, path, _ in site.hits) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_review_page_continue_is_navigation_until_confirmation(site, monkeypatch):
    script = [act("fill_form"), click("Review application"), click("Continue")]
    res, _ = run(site, monkeypatch, script, path="/apply_to_review")
    assert res.status == ApplyStatus.SUBMITTED, res.reason
    assert any(m == "GET" and p == "/review_continue" for m, p, _ in site.hits)
    assert posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_explicit_application_submit_inside_verification_form_persists_intent(site, monkeypatch):
    def check_intent_before_post(route):
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1
        route.fallback()

    site.ctx.route(HOST + "/thanks", check_intent_before_post)
    result, _ = run(site, monkeypatch, [act("fill_form"), click("Submit application", action="submit")],
                    path="/application_otp_final")
    assert result.status == ApplyStatus.SUBMITTED, result.reason
    assert posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 1


def test_review_submit_requires_each_known_application_answer_verified(site, monkeypatch):
    site.page.goto(HOST + "/review")
    job = Job("test:review", "Acme", "Intern", HOST + "/apply", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={}, submit_mode="full")
    run_state = generic.Run(job, ctx, site.page, time.time() + 30)
    fields = [Field("first", "First name", "text", True, name="first_name"),
              Field("email", "Email", "email", True, name="email")]
    keys = [generic._field_id("/apply", 0, field) for field in fields]
    for key, field, value in zip(keys, fields, ("Ada", "ada@example.com")):
        run_state.filled_fields[key] = Field(key, field.label, field.kind, field.required, name=field.name)
        run_state.filled_answers[key] = Answer(key, value, "profile")
        run_state.application_field_keys.add(key)
    run_state.verified_field_keys.add(keys[0])
    state = generic._collect(run_state)
    target = next(c for c in state.clickables if c.text == "Submit application")

    result = generic.ADAPTER._submit(run_state, state, target, "submit review")

    assert result.status == ApplyStatus.NEEDS_REVIEW
    assert "validated applicant answers" in result.reason or "lack verification" in result.reason
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_review_submit_blocks_changed_summary_value(site, monkeypatch):
    site.page.goto(HOST + "/review")
    site.page.locator("body").evaluate("el => el.querySelector('p').textContent = 'First name: Grace'")
    job = Job("test:review-changed", "Acme", "Intern", HOST + "/apply", [], "Software", 0)
    ctx = base.ApplyContext(page=site.page, browser=site.ctx, config={}, profile={}, submit_mode="full")
    run_state = generic.Run(job, ctx, site.page, time.time() + 30)
    field = Field("first", "First name", "text", True, name="first_name")
    key = generic._field_id("/apply", 0, field)
    run_state.filled_fields[key] = Field(key, field.label, field.kind, field.required, name=field.name)
    run_state.filled_answers[key] = Answer(key, "Ada", "profile")
    run_state.application_field_keys.add(key)
    run_state.verified_field_keys.add(key)
    state = generic._collect(run_state)
    target = next(c for c in state.clickables if c.text == "Submit application")

    result = generic.ADAPTER._submit(run_state, state, target, "submit review")

    assert result.status == ApplyStatus.NEEDS_REVIEW
    assert "selected values do not match" in result.reason
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_review_page_continue_is_gated_in_dry_run(site, monkeypatch):
    script = [act("fill_form"), click("Review application"), click("Continue")]
    res, _ = run(site, monkeypatch, script, path="/apply_to_review", dry_run=True)
    assert res.status == ApplyStatus.DRY_RUN_OK, res.reason
    assert any(m == "GET" and p == "/review_continue" for m, p, _ in site.hits)
    assert not posted_thanks(site)


def test_register_and_submit_preference_allows_account_action_and_proceeds(site, monkeypatch, fakes):
    prof = {"preferences": {"register_and_submit": "true"}, "resume_path": "/tmp/resume.pdf"}
    script = [act("create_account"), act("need_email_code")]
    res, _ = run(site, monkeypatch, script, path="/account", profile_override=prof)
    assert fakes.vault.created == ["jobs.example.test"]
    assert any(p == "/verify" for _, p, _ in site.hits)


def test_register_preference_cannot_submit_combined_application(site, monkeypatch):
    prof = {"preferences": {"register_and_submit": "true"}, "resume_path": "/tmp/resume.pdf"}
    res, _ = run(site, monkeypatch, [act("create_account")], path="/combined_account",
                 profile_override=prof)
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "manual review" in res.reason
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


def test_posting_apply_now_with_search_and_matcher_navigates_on_submit_action(site, monkeypatch):
    from agent import form as real_form
    monkeypatch.setattr(generic, "form", real_form)
    script = [click("Apply Now", action="submit"), click("Apply Now", action="submit"),
              act("fill_form"), click("Submit application", action="submit")]
    res, fake = run(site, monkeypatch, script, path="/nvidia_job", mode="fill_only")
    assert res.status == ApplyStatus.NEEDS_REVIEW and "fill_only" in res.reason, res.reason
    assert any(path == "/nvidia_step" for _, path, _ in site.hits)
    assert any(path == "/apply" for _, path, _ in site.hits)


def test_consecutive_stuck_with_validation_error_halts_with_error_message(site, monkeypatch):
    PAGES["/validation_stuck"] = """<h1>Apply</h1>
        <form><input id="fn" required><div role="alert">First name is required</div>
        <button type="button" onclick="void 0">Save</button></form>"""
    script = [click("Save")] * 5
    res, _ = run(site, monkeypatch, script, path="/validation_stuck")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "form validation error" in res.reason


@pytest.mark.parametrize("path", ["/posting_js_submit", "/posting_js_mixed", "/posting_href_handler"])
@pytest.mark.parametrize("action", ["click", "submit"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unverified_posting_apply_never_executes_javascript_submission(site, monkeypatch, path, action, dry_run):
    res, _ = run(site, monkeypatch, [click("Apply Now", action=action)],
                 path=path, dry_run=dry_run)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "unverified" in res.reason, res.reason
    assert not posted_thanks(site)
    assert not any(path == "/apply" for _, path, _ in site.hits)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


@pytest.mark.parametrize("path", ["/account_unsafe_action", "/account_unsafe_js", "/combined_account"])
@pytest.mark.parametrize("action", ["create_account", "click", "submit"])
def test_register_preference_never_exempts_unsafe_account_actions(site, monkeypatch, path, action):
    prof = {"preferences": {"register_and_submit": True}, "resume_path": "/tmp/resume.pdf"}
    script = [act("create_account")] if action == "create_account" else [click("Create Account" if path == "/combined_account" else "Create account", action=action)]
    res, _ = run(site, monkeypatch, script, path=path, profile_override=prof)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "manual review" in res.reason, res.reason
    assert not posted_thanks(site)
    assert not any(p == "/verify" for _, p, _ in site.hits)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["create_account", "click", "submit"])
def test_register_preference_preserves_separate_account_form(site, monkeypatch, action):
    prof = {"preferences": {"register_and_submit": True}, "resume_path": "/tmp/resume.pdf"}
    script = [act("create_account")] if action == "create_account" else [click("Create account", action=action)]
    res, _ = run(site, monkeypatch, script + [act("give_up")], path="/account_separate",
                 profile_override=prof)
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert any(m == "POST" and p == "/verify" for m, p, _ in site.hits)
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["create_account", "click", "submit"])
def test_dry_run_never_posts_account_even_when_creation_is_enabled(site, monkeypatch, action):
    prof = {"preferences": {"register_and_submit": True}, "resume_path": "/tmp/resume.pdf"}
    script = [act("create_account")] if action == "create_account" else [click("Create account", action=action)]
    res, _ = run(site, monkeypatch, script, path="/account",
                 profile_override=prof, dry_run=True,
                 config_override={"accounts": {"create_in_dry_run": True}})
    assert res.status == ApplyStatus.NEEDS_REVIEW and "dry run" in res.reason, res.reason
    assert not any(p == "/verify" for _, p, _ in site.hits)
    assert not posted_thanks(site)


@pytest.mark.parametrize("action", ["sign_in", "click", "submit"])
def test_existing_account_sign_in_uses_verified_login_without_register_preference(site, monkeypatch, fakes, action):
    fakes.vault.create("jobs.example.test", "Acme", HOST)
    script = [act("sign_in")] if action == "sign_in" else [click("Sign in", action=action)]
    res, _ = run(site, monkeypatch, script + [act("give_up")], path="/sign_in")
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert any(method == "POST" and path == "/login" for method, path, _ in site.hits)
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["sign_in", "click", "submit"])
def test_dry_run_blocks_even_verified_existing_account_login(site, monkeypatch, fakes, action):
    fakes.vault.create("jobs.example.test", "Acme", HOST)
    script = [act("sign_in")] if action == "sign_in" else [click("Sign in", action=action)]
    res, _ = run(site, monkeypatch, script, path="/sign_in", dry_run=True)
    assert res.status == ApplyStatus.NEEDS_REVIEW and "dry run" in res.reason, res.reason
    assert not any(method == "POST" and path == "/login" for method, path, _ in site.hits)
    assert not posted_thanks(site)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM submit_intents").fetchone()[0] == 0
