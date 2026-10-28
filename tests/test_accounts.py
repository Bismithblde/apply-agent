import base64
import time

import pytest
from cryptography.fernet import Fernet

from agent import db, llm, mail, vault
from agent.ats import workday


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APPLY_EMAIL", "jane.doe@gmail.com")
    monkeypatch.setattr(vault, "load_config", lambda: {"accounts": {"password_length": 20},
                                                       "email": {"plus_alias": True}})
    return tmp_path


# ---------- vault ----------

def test_vault_round_trip(tmp_db):
    tk = "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    acct = vault.create(tk, "NVIDIA", "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
    assert acct.email == "jane.doe+nvidia@gmail.com"
    row = db.get_account_row(tk)
    assert acct.password not in row["password_enc"]
    got = vault.get(tk)
    assert got.password == acct.password and got.email == acct.email and not got.verified
    vault.mark_verified(tk)
    vault.update_email(tk, "jane.doe@gmail.com")
    got = vault.get(tk)
    assert got.verified and got.email == "jane.doe@gmail.com" and got.password == acct.password
    vault.delete(tk)
    assert vault.get(tk) is None


def test_vault_plain_email_when_alias_off(tmp_db):
    acct = vault.create("x.wd1.myworkdayjobs.com/Ext", "X Corp", "", plus_alias=False)
    assert acct.email == "jane.doe@gmail.com"


def test_vault_wrong_key_and_missing_key(tmp_db, monkeypatch):
    vault.create("a.wd1.myworkdayjobs.com/S", "A", "")
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    with pytest.raises(vault.VaultError):
        vault.get("a.wd1.myworkdayjobs.com/S")
    monkeypatch.delenv("VAULT_KEY")
    with pytest.raises(vault.VaultError, match="VAULT_KEY missing"):
        vault.get("a.wd1.myworkdayjobs.com/S")


def test_alias_email_slug(monkeypatch):
    monkeypatch.setenv("APPLY_EMAIL", "me+old@gmail.com")
    assert vault.alias_email("AT&T Inc.") == "me+attinc@gmail.com"


def test_password_policy():
    for _ in range(300):
        p = vault.generate_password(20)
        assert len(p) == 20
        assert any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p)
        assert any(c in "!@#$%^&*" for c in p)
        assert not set(p) & set("0O1lI|")
        assert all(c in vault.UPPER + vault.LOWER + vault.DIGITS + vault.SYMBOLS for c in p)
    assert len(vault.generate_password(4)) == 12


@pytest.mark.parametrize("url,key", [
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Intern_JR1",
     "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/x_JR1",
     "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"),
    ("https://intel.wd1.myworkdayjobs.com/en-us/external/job/US-California/AI_JR0287533",
     "intel.wd1.myworkdayjobs.com/external"),
    ("https://KLA.wd1.myworkdayjobs.com/UR/job/Milpitas-CA/Software-Engineering-Intern_2642189?source=x",
     "kla.wd1.myworkdayjobs.com/UR"),
    ("https://american.wd1.myworkdayjobs.com/au/job/Main-Campus/RA_R5120-1", "american.wd1.myworkdayjobs.com/au"),
    ("https://kla.wd1.myworkdayjobs.com/en-US/UR/job/Milpitas%2C-CA/Intern_2642189/apply/applyManually",
     "kla.wd1.myworkdayjobs.com/UR"),
    ("https://wd1.myworkdaysite.com/recruiting/acme/External/job/NY/Intern_R1",
     "wd1.myworkdaysite.com/acme/External"),
    ("https://boards.greenhouse.io/stripe/jobs/123", "boards.greenhouse.io"),
])
def test_tenant_key_for(url, key):
    assert vault.tenant_key_for(url) == key


# ---------- mail extraction ----------

WORKDAY_HTML = """<html><body><p>Hi,</p><p>Please verify your account by clicking the link below.</p>
<a href="https://kla.wd1.myworkdayjobs.com/UR/activate/abc123def?redirect=%2FUR">Verify Account</a>
<a href="https://www.workday.com/privacy">Privacy</a><a href="https://x.com/unsubscribe">Unsubscribe</a>
</body></html>"""


def test_extract_link_from_html():
    code, link = mail.parse_message("Verify your candidate account", "", WORKDAY_HTML, use_llm=False)
    assert link == "https://kla.wd1.myworkdayjobs.com/UR/activate/abc123def?redirect=%2FUR"
    assert code is None


@pytest.mark.parametrize("text,code", [
    ("Your verification code is 482913. It expires in 10 minutes.", "482913"),
    ("Use code: A7K2QZ to confirm your email.", "A7K2QZ"),
    ("839201 is your one-time passcode", "839201"),
    ("Enter this code in the next 2026 seconds: 5521", "5521"),
    ("Thanks for applying in 2026! No code here.", None),
])
def test_extract_code(text, code):
    assert mail.extract_code(text) == code


def test_llm_fallback_validates_output(monkeypatch):
    body = "Hello! Your token for sign up: zz99zz . Thanks"
    monkeypatch.setattr(mail, "extract_code", lambda t: None)
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {"code": "zz99zz", "link": "https://evil.example/verify"})
    assert mail.parse_message("Welcome", body) == ("zz99zz", None)

    def boom(*a, **k):
        raise llm.LLMUnavailable("down")
    monkeypatch.setattr(llm, "generate_json", boom)
    assert mail.parse_message("Welcome", body) == (None, None)


def test_build_query():
    assert mail.build_query(1700000000.7, "myworkday", "me+kla@gmail.com") == \
        "after:1700000000 from:myworkday to:me+kla@gmail.com"
    assert mail.build_query(5, None, None) == "after:5"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class _FakeGmail:
    def __init__(self, messages):
        self.messages_data = messages
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, includeSpamTrash):
        self.queries.append(q)
        return _Exec({"messages": [{"id": m["id"]} for m in self.messages_data]})

    def get(self, userId, id, format):
        return _Exec(next(m for m in self.messages_data if m["id"] == id))


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


def test_wait_for_verification_gmail_mocked(tmp_db, monkeypatch):
    now = time.time()
    msg = {"id": "18c0ffee", "internalDate": str(int(now * 1000)), "payload": {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "Subject", "value": "Verify your account"},
                    {"name": "From", "value": "KLA <kla@myworkday.com>"},
                    {"name": "To", "value": "jane.doe+kla@gmail.com"}],
        "parts": [{"mimeType": "text/plain", "body": {"data": _b64("Verify here")}},
                  {"mimeType": "text/html", "body": {"data": _b64(WORKDAY_HTML)}}]}}
    fake = _FakeGmail([msg])

    def fake_gapi(path, **params):
        if path == "messages":
            return fake.list("me", params["q"], params["maxResults"], params["includeSpamTrash"]).execute()
        return fake.get("me", path.split("/", 1)[1], params["format"]).execute()

    monkeypatch.setattr(mail, "_gapi", fake_gapi)
    monkeypatch.setattr(mail, "_backend", lambda refresh=False: "gmail")
    monkeypatch.setattr(mail, "POLL_SECONDS", 0.05)
    v = mail.wait_for_verification(now - 10, sender_hint="myworkday", to_alias="jane.doe+kla@gmail.com",
                                   timeout=2, want="link")
    assert v and v.link.startswith("https://kla.wd1.myworkdayjobs.com/UR/activate/") and v.message_id == "18c0ffee"
    assert "from:myworkday" in fake.queries[0] and "to:jane.doe+kla@gmail.com" in fake.queries[0]
    assert db.mail_is_seen("18c0ffee")
    t0 = time.monotonic()
    assert mail.wait_for_verification(now - 10, timeout=0.5, want="link") is None
    assert time.monotonic() - t0 < 2


def test_gmail_token_refresh_via_net(tmp_path, monkeypatch):
    import json as _json
    from agent import net
    token = tmp_path / "token.json"
    token.write_text(_json.dumps({"token": "old", "refresh_token": "r1", "client_id": "cid", "client_secret": "sec",
                                  "token_uri": "https://oauth2.googleapis.com/token",
                                  "expiry": "2000-01-01T00:00:00+00:00"}))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(token))
    calls = []

    def fake_request(method, url, headers=None, data=None, timeout=20, attempts=2):
        calls.append((method, url, data))
        return 200, _json.dumps({"access_token": "new", "expires_in": 3600})

    monkeypatch.setattr(net, "request", fake_request)
    assert mail._access_token() == "new"
    assert calls[0][0] == "POST" and "refresh_token=r1" in calls[0][2] and "grant_type=refresh_token" in calls[0][2]
    assert _json.loads(token.read_text())["token"] == "new"
    assert mail._access_token() == "new" and len(calls) == 1  # cached until near expiry


def test_wait_for_verification_want_code_skips_link_only(tmp_db, monkeypatch):
    now = time.time()
    msg = mail._Msg(id="m1", subject="Activate", sender="x@myworkday.com", to="", ts=now, text="",
                    html=WORKDAY_HTML)
    monkeypatch.setattr(mail, "_gmail_list", lambda q: [msg])
    monkeypatch.setattr(mail, "_backend", lambda refresh=False: "gmail")
    monkeypatch.setattr(mail, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(llm, "available", lambda: False)
    assert mail.wait_for_verification(now - 5, timeout=0.4, want="code") is None
    assert not db.mail_is_seen("m1")


def test_unavailable_degrades_fast(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setenv("GMAIL_CLIENT_SECRET_FILE", str(tmp_path / "nope2.json"))
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setattr(mail, "_backend_cache", (0.0, None))
    t0 = time.monotonic()
    assert mail.available() is False
    assert mail.wait_for_verification(time.time(), timeout=30) is None
    assert time.monotonic() - t0 < 2


# ---------- Workday page inspection ----------

def _progress(active: str, steps=("Create Account/Sign In", "My Information", "My Experience", "Review")) -> str:
    items = []
    for i, s in enumerate(steps, 1):
        aid = "progressBarActiveStep" if s == active else "progressBarInactiveStep"
        prefix = "current step" if s == active else "step"
        items.append(f'<li data-automation-id="{aid}"><span>{prefix} {i} of {len(steps)}</span><br>{s}</li>')
    return f'<ol data-automation-id="progressBar">{"".join(items)}</ol>'


FIXTURES = {
    "posting": '<div data-automation-id="jobPostingPage"><a data-automation-id="adventureButton" href="#">Apply</a></div>',
    "start": '<a data-automation-id="adventureButton">Apply</a><div data-automation-id="wd-popup-frame">'
             '<a data-automation-id="autofillWithResume">Autofill</a><a data-automation-id="applyManually">Apply Manually</a></div>',
    "auth_choice": '<div data-automation-id="applyFlowPage">' + _progress("Create Account/Sign In") +
                   '<div data-automation-id="signInContent"><button data-automation-id="SignInWithEmailButton">Sign in with email</button></div></div>',
    "auth_signin": '<div data-automation-id="applyFlowPage">' + _progress("Create Account/Sign In") +
                   '<div data-automation-id="signInContent"><input data-automation-id="email"><input type="password" data-automation-id="password">'
                   '<input data-automation-id="beecatcher"><button data-automation-id="signInSubmitButton">Sign In</button></div></div>',
    "auth_create": '<div data-automation-id="applyFlowPage">' + _progress("Create Account/Sign In") +
                   '<div data-automation-id="signInContent"><input data-automation-id="email"><input type="password" data-automation-id="password">'
                   '<input type="password" data-automation-id="verifyPassword"></div></div>',
    "wizard": '<div data-automation-id="applyFlowPage">' + _progress("My Information") +
              '<label for="fn">First Name*</label><input id="fn"><button data-automation-id="bottom-navigation-next-button">Save and Continue</button></div>',
    "review": '<div data-automation-id="applyFlowPage">' + _progress("Review") +
              '<button data-automation-id="bottom-navigation-next-button">Submit</button></div>',
    "submitted": '<div><h2>Application Submitted</h2><p>Congratulations! We received your application.</p></div>',
    "closed": '<div><h1>The page you are looking for doesn\'t exist.</h1></div>',
}


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        p = b.new_page()
        yield p
        b.close()


@pytest.mark.parametrize("state", list(FIXTURES))
def test_detect_state(page, state):
    page.set_content(f"<html><body>{FIXTURES[state]}</body></html>")
    assert workday.detect_state(page) == state


def test_step_name_and_review(page):
    page.set_content(FIXTURES["wizard"])
    assert workday.step_name(page) == "My Information"
    assert not workday.is_review(page)
    page.set_content(FIXTURES["auth_choice"])
    assert workday.is_auth_step(workday.step_name(page))
    page.set_content(FIXTURES["review"])
    assert workday.step_name(page) == "Review" and workday.is_review(page)
    # Submit label alone also means review, whatever the step is called
    page.set_content('<div data-automation-id="applyFlowPage">' + _progress("Summary", ("Summary",)) +
                     '<button data-automation-id="bottom-navigation-next-button">Submit</button></div>')
    assert workday.is_review(page) and workday.detect_state(page) == "review"


def test_error_texts(page):
    page.set_content('<div data-automation-id="errorBanner"><button>Errors Found</button><ul>'
                     '<li>Error-Phone Number: The field Phone Number is required.</li></ul></div>'
                     '<div data-automation-id="formField-addressLine1"><label>Address Line 1*</label><input data-aa-key="aa-3">'
                     '<div data-automation-id="errorMessage">The field Address Line 1 is required and must have a value.</div></div>'
                     '<div data-automation-id="errorMessage" style="display:none">hidden</div>')
    errs = workday.error_texts(page)
    assert "Error-Phone Number: The field Phone Number is required." in errs
    assert "The field Address Line 1 is required and must have a value." in errs
    assert "hidden" not in errs


def test_skip_js_hides_workday_widgets(page):
    from agent import form
    page.set_content('<div data-automation-id="applyFlowPage">'
                     '<label for="a">City*</label><input id="a">'
                     '<input data-automation-id="beecatcher">'
                     '<div data-automation-id="multiselectInputContainer"><input data-uxi-widget-type="selectinput"></div>'
                     '<div data-automation-id="formField-phoneType"><label>Phone Device Type*</label>'
                     '<button aria-haspopup="listbox">Select One</button></div></div>')
    page.evaluate(workday._SKIP_JS, workday.ROOT)
    labels = [f.label for f in form.extract_fields(page, workday.ROOT)]
    assert labels == ["City"]


class _Ctx:
    def __init__(self, page):
        self.page, self.browser, self.config, self.profile = page, None, {}, {}
        self.submit_mode, self.dry_run, self.notes = "fill_only", True, []

    def screenshot(self, job, tag):
        return None


def _job():
    return workday.Job(id="t", company="KLA", title="t", url="https://kla.wd1.myworkdayjobs.com/UR/job/a",
                       locations=[], category="", date_posted=0)


def test_verify_email_degrades_when_mail_down(page, tmp_db, monkeypatch):
    monkeypatch.setattr(mail, "available", lambda: False)
    page.set_content('<div>Please verify your email. A verification email has been sent.</div>')
    run = workday._Run(_job(), _Ctx(page))
    acct = vault.create(run.tenant, "KLA", "")
    with pytest.raises(workday._Stop) as e:
        run.handle_auth_outcome("verify", acct)
    assert e.value.result.status == workday.ApplyStatus.NEEDS_REVIEW
    assert e.value.result.reason.startswith("email verification required")


def test_account_exists_outcome(page):
    page.set_content("<div></div>")
    run = workday._Run(_job(), _Ctx(page))
    with pytest.raises(workday._Stop) as e:
        run.handle_auth_outcome("exists", None)
    assert e.value.result.reason.startswith("account exists, password unknown")


@pytest.mark.parametrize("text,expected", [
    ("2027-05-01", (2027, 5, 1)), ("08/2026", (2026, 8, 1)), ("10/26/2026", (2026, 10, 26)),
    ("June 2025", (2025, 6, 1)), ("2024-02", (2024, 2, 1)), ("Present", None),
])
def test_parse_date(text, expected):
    assert workday.parse_date(text) == expected


def test_helpers():
    assert workday.site_base_url("https://kla.wd1.myworkdayjobs.com/en-US/UR/job/x/apply") == \
        "https://kla.wd1.myworkdayjobs.com/UR"
    opts = ["Mobile", "Landline", "Home Cellular"]
    assert workday._match_option(opts, "mobile") == 0
    assert workday._match_option(["United States of America", "Canada"], "United States") == 0
    assert workday._match_option(["Yes", "No"], "Maybe") is None
    assert workday.ADAPTER.can_handle(
        workday.Job(id="x", company="KLA", title="t", url="https://kla.wd1.myworkdayjobs.com/UR/job/a",
                    locations=[], category="", date_posted=0))


# ---------- Workday dropdown / prompt selection ----------

def test_pick_option_index_direct_match():
    texts = ["Select One", "Option A", "Option B"]
    assert workday._pick_option_index(texts, "Option B") == 2
    assert workday._pick_option_index(texts, "option a") == 1


def test_unmatched_required_options_are_not_guessed():
    for texts in (["Select One", "Accounting", "Other"], ["Select One", "Morning", "Evening"]):
        assert workday._pick_option_index(texts, "Computer Science", is_required=True) is None
        assert workday._pick_option_index(texts, "Computer Science", is_referral=True, is_required=True) is None
    assert workday._match_option(["Data Science"], "Computer Science") is None


def test_pick_option_index_phone_device_type():
    # Neither a phone number nor an empty answer establishes a device type.
    texts1 = ["Select One", "Landline", "Mobile", "Work"]
    assert workday._pick_option_index(texts1, "+1 555-123-4567", is_phone_device=True) is None
    assert workday._pick_option_index(texts1, "", is_phone_device=True) is None

    # Explicit mobile answers can use the tenant's equivalent personal label.
    texts2 = ["Select One", "Cellular Phone", "Landline"]
    assert workday._pick_option_index(texts2, "Mobile", is_phone_device=True) == 1

    texts3 = ["Select One", "Home Phone", "Work Phone"]
    assert workday._pick_option_index(texts3, "12345", is_phone_device=True) is None


def test_choose_option_referral_and_phone(page):
    html = """
    <div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-source">
        <label>How did you hear about us?*</label>
        <button id="hearBtn" aria-haspopup="listbox" data-aa-key="aa-hear" onclick="openHear()">Select One</button>
      </div>
      <div data-automation-id="formField-phoneDeviceType">
        <label>Phone Device Type*</label>
        <button id="phoneBtn" aria-haspopup="listbox" data-aa-key="aa-phone" onclick="openPhone()">Select One</button>
      </div>
      <div id="lb" role="listbox" style="display:none"></div>
    </div>
    <script>
      function openHear() {
        const lb = document.getElementById('lb');
        lb.style.display = 'block';
        lb.innerHTML = '<div role="option">Select One</div><div role="option">Career Fair</div><div role="option">Job Board</div><div role="option">Other</div>';
        lb.querySelectorAll('[role=option]').forEach(opt => {
          opt.addEventListener('click', () => {
            document.getElementById('hearBtn').innerText = opt.innerText;
            lb.style.display = 'none';
          });
        });
      }
      function openPhone() {
        const lb = document.getElementById('lb');
        lb.style.display = 'block';
        lb.innerHTML = '<div role="option">Select One</div><div role="option">Landline</div><div role="option">Mobile</div>';
        lb.querySelectorAll('[role=option]').forEach(opt => {
          opt.addEventListener('click', () => {
            document.getElementById('phoneBtn').innerText = opt.innerText;
            lb.style.display = 'none';
          });
        });
      }
    </script>
    """
    page.set_content(html, wait_until="load")
    run = workday._Run(_job(), _Ctx(page))
    run.fields["aa-hear"] = workday.Field(key="aa-hear", label="How did you hear about us?*", kind="select", required=True)
    run.fields["aa-phone"] = workday.Field(key="aa-phone", label="Phone Device Type*", kind="select", required=True)

    # An unmatched referral must not be invented.
    hear_btn = page.locator("#hearBtn")
    ok = run.choose_option(hear_btn, "Friend")
    assert ok is False
    assert hear_btn.inner_text() == "Select One"

    # A device needs an explicit answer, even when Mobile is available.
    phone_btn = page.locator("#phoneBtn")
    ok = run.choose_option(phone_btn, "")
    assert ok is False
    assert phone_btn.inner_text() == "Select One"
    assert run.choose_option(phone_btn, "Mobile")
    assert phone_btn.inner_text() == "Mobile"
    assert run.answers["aa-phone"].value == "Mobile"


def test_choose_option_required_fallback(page):
    html = """
    <div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-shift">
        <label>Preferred Shift*</label>
        <button id="shiftBtn" aria-haspopup="listbox" data-aa-key="aa-shift" onclick="openShift()">Select One</button>
      </div>
      <div id="shiftLb" role="listbox" style="display:none"></div>
    </div>
    <script>
      function openShift() {
        const lb = document.getElementById('shiftLb');
        lb.style.display = 'block';
        lb.innerHTML = '<div role="option">Select One</div><div role="option">Morning</div><div role="option">Evening</div>';
        lb.querySelectorAll('[role=option]').forEach(opt => {
          opt.addEventListener('click', () => {
            document.getElementById('shiftBtn').innerText = opt.innerText;
            lb.style.display = 'none';
          });
        });
      }
    </script>
    """
    page.set_content(html, wait_until="load")
    run = workday._Run(_job(), _Ctx(page))
    run.fields["aa-shift"] = workday.Field(key="aa-shift", label="Preferred Shift*", kind="select", required=True)

    btn = page.locator("#shiftBtn")
    # An unmatched answer must leave the required field unselected.
    ok = run.choose_option(btn, "Night")
    assert ok is False
    assert btn.inner_text() == "Select One"
    assert "aa-shift" not in run.answers


def test_searchable_major_does_not_select_first_option_on_enter(page):
    page.set_content('''<div data-automation-id="formField-fieldOfStudy">
      <label>Field of Study*</label><div data-automation-id="multiselectInputContainer">
      <input id="major" data-aa-key="major" onkeydown="if(event.key === 'Enter') document.querySelector('#accounting').click()"></div>
      <div role="listbox"><div role="option" id="accounting" onclick="this.dataset.clicked='yes'">Accounting</div>
      <div role="option" id="cs" onclick="this.dataset.clicked='yes'; this.closest('[data-automation-id=formField-fieldOfStudy]').insertAdjacentHTML('beforeend', '<span data-automation-id=selectedItem>Computer Science</span>')">Computer Science</div></div></div>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field(key="major", label="Field of Study", kind="combobox", required=True)
    assert run.choose_option(page.locator('#major'), 'Computer Science', multi=True, field=field)
    assert page.locator('#accounting').get_attribute('data-clicked') is None
    assert page.locator('#cs').get_attribute('data-clicked') == 'yes'


def test_workday_major_scrolls_lazy_options_to_exact_match(page):
    page.set_content('''<div data-automation-id="formField-fieldOfStudy">
      <label>Field of Study*</label><div data-automation-id="multiselectInputContainer">
        <input id="major" data-aa-key="major" aria-controls="majors" aria-expanded="false">
      </div><div id="majors" role="listbox" style="max-height:60px;overflow-y:auto;display:none">
        <div role="option">Accounting</div><div role="option">Aerospace</div>
        <div role="option">Animation</div><div role="option">Art History</div>
        <div role="option">Biology</div><div role="option">Business</div>
      </div></div><script>
      const major = document.querySelector('#major');
      const majors = document.querySelector('#majors');
      for (let i = 0; i < 30; i++) {
        majors.insertAdjacentHTML('beforeend', `<div role="option">Business ${i}</div>`);
      }
      major.onclick = () => { majors.style.display = 'block'; major.setAttribute('aria-expanded', 'true'); };
      majors.onscroll = () => {
        if (majors.scrollTop > 300 && !document.querySelector('#cs')) {
          majors.insertAdjacentHTML('beforeend', '<div role="option" id="cs">Computer Science</div>');
          document.querySelector('#cs').onclick = () => {
            majors.insertAdjacentHTML('afterend',
              '<span data-automation-id="selectedItem">Computer Science</span>');
            majors.style.display = 'none';
          };
        }
      };
      document.querySelectorAll('#majors [role=option]').forEach(o => o.onclick = () =>
        o.dataset.incorrectlySelected = 'true');
      </script>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('major', 'Education 1 - Field of Study', 'combobox', True,
                          name='formField-fieldOfStudy')
    assert run.choose_option(page.locator('#major'), 'Computer Science', multi=True, field=field)
    assert page.locator('#cs').count() == 1
    assert page.locator('#majors [data-incorrectly-selected]').count() == 0
    assert run.answers['major'].value == 'Computer Science'


def test_workday_education_years_use_profile_and_fill_year_only_fields(page, monkeypatch):
    from agent import answers
    page.set_content('''<div data-automation-id="applyFlowPage">
      <h3 id="Education-1-panel">Education 1</h3>
      <div role="group" aria-labelledby="Education-1-panel">
        <div data-automation-id="formField-startDate"><label>From*</label>
          <div data-automation-id="dateInputWrapper">
            <input data-automation-id="dateSectionYear-input" placeholder="YYYY" maxlength="4" required>
          </div></div>
        <div data-automation-id="formField-endDate"><label>To (Actual or Expected)*</label>
          <div data-automation-id="dateInputWrapper">
            <input data-automation-id="dateSectionYear-input" placeholder="YYYY" maxlength="4" required>
          </div></div>
      </div></div>''')
    monkeypatch.setattr(answers, 'answer_fields', lambda fields, *args, **kwargs: [
        workday.Answer(f.key, None, 'unknown') for f in fields])
    ctx = _Ctx(page)
    ctx.profile = {'education': {'start_year': '2024', 'graduation_year': '2028'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Experience')
    years = page.locator('[data-automation-id="dateSectionYear-input"]')
    assert [years.nth(i).input_value() for i in range(2)] == ['2024', '2028']
    found = [a for a in run.answers.values() if a.value in ('2024', '2028')]
    assert len(found) == 2 and all(a.source == 'profile' for a in found)


def test_workday_keeps_education_with_validation_errors_for_repair(page):
    page.set_content('''<div role="group" aria-labelledby="Education-1-panel" id="education">
      <input value="Stony Brook University"><div data-automation-id="errorMessage">From is required</div>
      <button onclick="this.closest('[role=group]').remove()">Delete</button></div>
      <div role="group" aria-labelledby="Work-Experience-1-panel" id="work">
      <div data-automation-id="errorMessage">Invalid work experience</div>
      <button onclick="this.closest('[role=group]').remove()">Delete</button></div>''')
    run = workday._Run(_job(), _Ctx(page))
    run.drop_bad_work_experience()
    assert page.locator('#education').count() == 1
    assert page.locator('#work').count() == 0


@pytest.mark.parametrize('complete', [False, True])
def test_segmented_date_verifies_every_segment(page, complete):
    page.set_content('''<div data-automation-id="dateInputWrapper">
      <input data-automation-id="dateSectionMonth-input" maxlength="2">
      <input data-automation-id="dateSectionDay-input" maxlength="2">
      <input data-automation-id="dateSectionYear-input" maxlength="4">
      </div>''')
    if complete:
        page.evaluate('''() => {
          const inputs = [...document.querySelectorAll('input')];
          inputs.forEach((el, i) => el.addEventListener('input', () => {
            if (el.value.length === el.maxLength && inputs[i + 1]) inputs[i + 1].focus();
          }));
        }''')
    run = workday._Run(_job(), _Ctx(page))
    assert run.fill_date(page.locator('[data-automation-id="dateSectionMonth-input"]'), '05/12/2028') is complete


def test_failed_widget_fill_blocks_submission(page, monkeypatch):
    from agent import answers
    page.set_content('''<div data-automation-id="applyFlowPage"><div data-automation-id="formField-major">
       <label>Field of Study*</label><button aria-haspopup="listbox">Select One</button></div>
       <div role="listbox"><div role="option">Accounting</div></div></div>''')
    monkeypatch.setattr(answers, 'answer_fields', lambda fields, *args, **kwargs: [
        workday.Answer(f.key, 'Computer Science', 'profile') for f in fields])
    run = workday._Run(_job(), _Ctx(page))
    run.fill_step('My Experience')
    fields = list(run.fields.values())
    saved = list(run.answers.values())
    assert fields and saved[0].source == 'unknown'
    allowed, reason = workday.submit_decision(fields, saved, 'full', False)
    assert not allowed and 'unanswered required' in reason


@pytest.mark.parametrize('value', [
    '2028-13-01', '2028-00-01', '2028-02-30', '2027-02-29',
    '00/2028', '04/31/2028', '0000', '2028-05-01-extra', '05/12/2028 trailing',
])
def test_parse_date_rejects_invalid_calendar_dates_and_extra_text(value):
    assert workday.parse_date(value) is None


def test_parse_date_accepts_leap_day():
    assert workday.parse_date('2028-02-29') == (2028, 2, 29)


def test_major_match_requires_same_choice():
    assert workday._match_option(['Computer Science Education'], 'Computer Science') is None
    assert workday._match_option(['Computer Science'], 'Computer Science Education') is None
    assert workday._match_option(['Computer Science Education', ' COMPUTER  SCIENCE '], 'computer science') == 1


@pytest.mark.parametrize('changes_selection', [False, True])
def test_single_select_requires_displayed_choice(page, changes_selection):
    page.set_content('''<div data-automation-id="formField-major"><label>Major*</label>
      <button id="major" data-aa-key="major" aria-haspopup="listbox">Select One</button>
      <div role="listbox"><div role="option">Computer Science</div></div></div>''')
    if changes_selection:
        page.locator('[role=option]').evaluate('''el => el.onclick = () => {
          document.querySelector('#major').innerText = el.innerText;
          el.parentElement.style.display = 'none';
        }''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('major', 'Major', 'select', True)
    assert run.choose_option(page.locator('#major'), 'Computer Science', field=field) is changes_selection
    assert ('major' in run.answers) is changes_selection


@pytest.mark.parametrize('input_html,value,expected', [
    ('<input>', '05/12/2028', '05/12/2028'),
    ('<input>', '2028-05-01', '05/01/2028'),
    ('<input>', 'May 2028', '05/2028'),
    ('<input type="date">', '05/12/2028', '2028-05-12'),
])
def test_plain_date_preserves_precision(page, input_html, value, expected):
    page.set_content(input_html)
    run = workday._Run(_job(), _Ctx(page))
    assert run.fill_date(page.locator('input'), value)
    assert page.locator('input').input_value() == expected


def test_plain_date_rejects_truncation_and_invalid_input(page):
    page.set_content('<input maxlength="7">')
    run = workday._Run(_job(), _Ctx(page))
    assert not run.fill_date(page.locator('input'), '05/01/2028')
    page.locator('input').fill('')
    assert not run.fill_date(page.locator('input'), '02/30/2028')
    assert page.locator('input').input_value() == ''


def test_profile_major_is_not_fuzzily_coerced_before_workday_selection(page):
    page.set_content('''<div data-automation-id="applyFlowPage"><div data-automation-id="formField-major">
      <label>Field of Study*</label><button id="major" aria-haspopup="listbox" onclick="document.querySelector('[role=listbox]').style.display='block'">Select One</button></div>
      <div role="listbox" style="display:none"><div role="option" onclick="document.querySelector('#major').innerText=this.innerText">Computer Science Education</div></div></div>
      <script>document.addEventListener('keydown', e => { if(e.key === 'Escape') document.querySelector('[role=listbox]').style.display='none' });</script>''')
    ctx = _Ctx(page)
    ctx.profile = {'education': {'major': 'Computer Science'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    assert page.locator('#major').inner_text() == 'Select One'
    assert any(f.required and run.answers[f.key].source == 'unknown' for f in run.fields.values())


@pytest.mark.parametrize('label,profile,option,selected', [
    ('School', {'school': 'Stony Brook University'}, 'Stony Brook University Extension', False),
    ('School', {'school': 'Stony Brook University'}, 'Stony Brook University', True),
    ('Degree', {'degree': "Bachelor's"}, "Bachelor's in Education", False),
    ('Degree', {'degree': "Bachelor's"}, "Bachelor's Degree", True),
])
def test_workday_profile_options_remain_grounded(page, label, profile, option, selected):
    page.set_content('''<div data-automation-id="applyFlowPage"><div data-automation-id="formField-choice">
      <label id="label"></label><button id="choice" aria-haspopup="listbox"
      onclick="document.querySelector('[role=listbox]').style.display='block'">Select One</button></div>
      <div role="listbox" style="display:none"><div role="option" onclick="document.querySelector('#choice').innerText=this.innerText; this.parentElement.style.display='none'"></div></div></div>
      <script>document.addEventListener('keydown', e => { if(e.key === 'Escape') document.querySelector('[role=listbox]').style.display='none' });</script>''')
    page.locator('#label').evaluate('(el, text) => el.innerText = text + "*"', label)
    page.locator('[role=option]').evaluate('(el, text) => el.innerText = text', option)
    ctx = _Ctx(page)
    ctx.profile = {'education': profile}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    assert page.locator('#choice').inner_text() == (option if selected else 'Select One')
    answer = next(iter(run.answers.values()))
    assert (answer.source != 'unknown') is selected
    assert workday.submit_decision(list(run.fields.values()), list(run.answers.values()), 'full', False)[0] is selected


@pytest.mark.parametrize('value,expected', [('Home', 'Home'), ('Landline', 'Landline'), ('', None), ('Work', None), ('1234567', None)])
def test_phone_device_respects_supplied_answer(page, value, expected):
    page.set_content('''<div data-automation-id="formField-phoneDeviceType"><label>Phone Device Type*</label>
      <button id="device" data-aa-key="device">Select One</button><div role="listbox">
      <div role="option">Home</div><div role="option">Landline</div><div role="option">Mobile</div></div></div>
      <script>document.querySelectorAll('[role=option]').forEach(el => el.onclick = () => {
        document.querySelector('#device').innerText=el.innerText; el.parentElement.style.display='none';
      });</script>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('device', 'Phone Device Type', 'select', True)
    answer = workday.Answer('device', value, 'profile')
    assert run.fill_workday_widget(field, answer) is (expected is not None)
    assert page.locator('#device').inner_text() == (expected or 'Select One')


def test_native_workday_select_rejects_fuzzy_school(page):
    page.set_content('''<div data-automation-id="applyFlowPage"><label for="school">School*</label>
      <select id="school" required><option>Select One</option><option>Stony Brook University Extension</option></select></div>''')
    ctx = _Ctx(page)
    ctx.profile = {'education': {'school': 'Stony Brook University'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    assert page.locator('#school').input_value() == 'Select One'
    assert next(iter(run.answers.values())).source == 'unknown'


def test_phone_device_full_step_requires_an_explicit_profile_type(page):
    page.set_content('''<div data-automation-id="applyFlowPage"><div data-automation-id="formField-phoneDeviceType">
      <label>Phone Device Type*</label><button id="device" aria-haspopup="listbox" onclick="document.querySelector('[role=listbox]').style.display='block'">Select One</button></div>
      <div role="listbox" style="display:none"><div role="option">Home</div><div role="option">Mobile</div></div></div>
      <script>document.querySelectorAll('[role=option]').forEach(el => el.onclick = () => {
        document.querySelector('#device').innerText = el.innerText; el.parentElement.style.display='none';
      }); document.addEventListener('keydown', e => { if(e.key === 'Escape') document.querySelector('[role=listbox]').style.display='none' });</script>''')
    ctx = _Ctx(page)
    ctx.profile = {'personal': {'phone_device_type': 'Home'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    assert page.locator('#device').inner_text() == 'Home'
    assert next(iter(run.answers.values())).source == 'profile'
    page.locator('#device').evaluate("el => el.innerText='Select One'")
    ctx.profile = {'personal': {}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    assert page.locator('#device').inner_text() == 'Select One'
    assert next(iter(run.answers.values())).source == 'unknown'


def test_workday_generated_option_validation_does_not_fuzzy_coerce():
    from agent import answers
    field = workday.Field('degree', 'Degree', 'select', True, ["Bachelor's in Education"])
    evidence = [answers._Evidence('e1', 'profile', 'education', "Bachelor's in Education")]
    row = {'source': 'bank', 'value': "Bachelor's", 'evidence_ids': ['e1']}
    result = answers._validate(field, row, evidence, option_matcher=workday._match_option)
    assert result.source == 'unknown'
    assert 'option not in list' in result.note


@pytest.mark.parametrize('value,expected', [('', None), ('Home', 'Home'), ('Work', None)])
def test_native_phone_select_requires_an_explicit_type(page, value, expected):
    page.set_content('<select data-aa-key="phone"><option>Select One</option><option>Home</option><option>Mobile</option></select>')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('phone', 'Phone Device Type', 'select', True)
    assert run.fill_workday_widget(field, workday.Answer('phone', value, 'profile')) is (expected is not None)
    assert page.locator('select').input_value() == (expected or 'Select One')
    if expected:
        assert run.answers['phone'].value == expected
        assert run.answers['phone'].source == 'profile'


def test_native_phone_full_step_does_not_invent_mobile(page):
    page.set_content('''<div data-automation-id="applyFlowPage"><label for="phone">Phone Device Type*</label>
      <select id="phone" required><option>Select One</option><option>Mobile</option></select></div>''')
    run = workday._Run(_job(), _Ctx(page))
    run.fill_step('My Information')
    assert page.locator('#phone').input_value() == 'Select One'
    assert next(iter(run.answers.values())).source == 'unknown'


@pytest.mark.parametrize('requested,options,expected', [
    ('United States (+1)', ['Canada (+1)', 'United States of America (+1)'], 1),
    ('United States of America (+1)', ['United States (+1)'], 0),
    ('United States (+1)', ['American Samoa (+1)', 'Canada (+1)'], None),
    ('United States (+1)', ['United States of America (+44)'], None),
    ('New York', ['United States of America (+1)'], None),
    ('Stony Brook University', ['Stony Brook University Extension'], None),
    ("Bachelor's", ["Bachelor's in Education"], None),
])
def test_workday_exact_phone_country_alias_and_no_fuzzy_matching(requested, options, expected):
    assert workday._match_option(options, requested) == expected


@pytest.mark.parametrize('options,expected', [
    (['Select One', 'Mobile - Personal', 'Mobile - Work', 'Home'], 1),
    (['Select One', 'Home', 'Pers Mobile', 'Work'], 2),
    (['Select One', 'Home Mobile', 'Work Mobile'], 1),
    (['Select One', 'Home', 'Work'], None),
    (['Select One', 'Mobile - Work'], None),
])
def test_phone_device_personal_mobile_only(options, expected):
    assert workday._pick_option_index(options, 'Mobile', is_phone_device=True, is_required=True) == expected
    assert workday._pick_option_index(options, '', is_phone_device=True, is_required=True) is None


@pytest.mark.parametrize('options,expected', [
    (['Select One', 'Career Site', 'Job Board'], 2),
    (['Select One', 'Job Sites', 'Social Referral'], 1),
    (['Select One', 'Other', 'Job Boards'], 2),
    (['Select One', 'Job Board - Indeed', 'Other', 'Job Board - Other'], 3),
    (['Select One', 'Indeed.com', 'Other'], 2),
    (['Select One', 'School Career Website', 'Career Fair'], None),
    (['Select One', 'Career Site', 'Employee Referral'], None),
    (['Select One', 'LinkedIn', 'Indeed', 'Glassdoor', 'Social Media'], None),
    (['Select One', 'Job Board - LinkedIn', 'Other'], 2),
])
def test_simplify_maps_only_to_real_job_board_category(options, expected):
    assert workday._pick_option_index(options, 'Simplify', is_referral=True) == expected
    assert workday._pick_option_index(options, 'Simplify', is_referral=False) is None


def test_scoped_workday_portal_does_not_mix_phone_and_referral_options(page):
    page.set_content('''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-phoneCode"><label>Country Phone Code*</label>
        <button id="phone" data-aa-key="phone" aria-haspopup="listbox" aria-controls="phone-list">United States of America (+1)</button>
      </div>
      <div id="phone-list" role="listbox"><div role="option" id="canada">Canada (+1)</div>
        <div role="option" id="us">United States of America (+1)</div></div>
      <div data-automation-id="formField-source"><label>How Did You Hear About Us?*</label>
        <button id="source" data-aa-key="source" aria-haspopup="listbox" aria-controls="source-list"
          onclick="document.querySelector('#source-list').style.display='block'">Select One</button>
      </div>
      <div id="source-list" role="listbox" style="display:none">
        <div role="option">Career Site</div><div role="option" id="board">Job Board</div></div>
    </div>
    <script>document.querySelectorAll('#source-list [role=option]').forEach(el => {
      el.onclick = () => { document.querySelector('#source').innerText = el.innerText;
        document.querySelector('#source-list').style.display='none'; };
    }); document.querySelector('#canada').onclick = e => e.target.dataset.clicked = 'yes';</script>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('source', 'How Did You Hear About Us?', 'select', True,
                          name='formField-source')
    assert run.choose_option(page.locator('#source'), 'Simplify', field=field)
    assert page.locator('#source').inner_text() == 'Job Board'
    assert page.locator('#phone').inner_text() == 'United States of America (+1)'
    assert page.locator('#canada').get_attribute('data-clicked') is None
    assert run.answers['source'].value == 'Job Board'


def test_workday_unrelated_persistent_menu_never_supplies_options(page):
    page.set_content('''<div data-automation-id="formField-state"><label>State*</label>
      <button id="state" data-aa-key="state" onclick="void(0)">Select One</button></div>
      <div role="listbox"><div role="option" onclick="this.dataset.clicked='yes'">United States of America (+1)</div></div>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('state', 'State', 'select', True)
    assert not run.choose_option(page.locator('#state'), 'New York', field=field)
    assert page.locator('#state').inner_text() == 'Select One'
    assert page.locator('[role=option]').get_attribute('data-clicked') is None


def test_workday_ambiguous_multiple_popups_fail_closed(page):
    page.set_content('''<div data-automation-id="formField-degree"><label>Degree*</label>
      <button id="degree" data-aa-key="degree" onclick="document.querySelectorAll('[role=listbox]')
        .forEach(el => el.style.display='block')">Select One</button></div>
      <div role="listbox" style="display:none"><div role="option" id="safe">Bachelor's Degree</div></div>
      <div role="listbox" style="display:none"><div role="option" id="bad">Bachelor's in Education</div></div>
      <script>document.querySelectorAll('[role=option]').forEach(el => el.onclick = () => el.dataset.clicked='yes')</script>''')
    run = workday._Run(_job(), _Ctx(page))
    assert not run.choose_option(page.locator('#degree'), "Bachelor's", field=workday.Field('degree', 'Degree', 'select', True))
    assert page.locator('#safe').get_attribute('data-clicked') is None
    assert page.locator('#bad').get_attribute('data-clicked') is None


def test_workday_referral_and_personal_phone_in_full_step(page):
    page.set_content('''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-source"><label>How Did You Hear About Us?*</label>
        <button id="source" aria-haspopup="listbox" onclick="document.querySelector('#sources').style.display='block'">Select One</button></div>
      <div id="sources" role="listbox" style="display:none"><div role="option">Job Board</div>
        <div role="option">Career Site</div></div>
      <div data-automation-id="formField-phoneDeviceType"><label>Phone Device Type*</label>
        <button id="device" aria-haspopup="listbox" onclick="document.querySelector('#devices').style.display='block'">Select One</button></div>
      <div id="devices" role="listbox" style="display:none"><div role="option">Home</div>
        <div role="option">Mobile - Personal</div><div role="option">Mobile - Work</div></div>
    </div><script>document.querySelectorAll('[role=option]').forEach(opt => {
      opt.onclick = () => { let list = opt.closest('[role=listbox]');
        document.querySelector(list.id === 'sources' ? '#source' : '#device').innerText = opt.innerText;
        list.style.display='none'; };
    }); document.addEventListener('keydown', e => { if(e.key === 'Escape')
      document.querySelectorAll('[role=listbox]').forEach(el => el.style.display='none'); });</script>''')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'how_did_you_hear': 'Simplify'}, 'personal': {'phone_device_type': 'Mobile'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    assert page.locator('#source').inner_text() == 'Job Board'
    assert page.locator('#device').inner_text() == 'Mobile - Personal'
    assert all(a.source != 'unknown' for a in run.answers.values())


def test_workday_portaled_referral_ignores_remounted_country_prompts(page):
    page.set_content('''<style>
        #source {position:absolute;left:20px;top:20px;width:220px;height:30px}
        #country-menu {position:absolute;left:20px;top:290px;width:220px}
        #source-menu {position:absolute;left:20px;top:55px;width:220px}
      </style><div data-automation-id="formField-source">
        <label>How Did You Hear About Us?*</label>
        <button id="source" data-aa-key="source" onclick="openSource()">Select One</button>
      </div>
      <div id="country-menu"><div data-automation-id="promptOption">Canada (+1)</div>
        <div data-automation-id="promptOption" id="us">United States of America (+1)</div></div>
      <script>
        function openSource() {
          // Simulate React remounting an unrelated country menu when a referral menu opens.
          document.getElementById('country-menu').innerHTML =
            '<div data-automation-id="promptOption">Canada (+1)</div>' +
            '<div data-automation-id="promptOption" id="us">United States of America (+1)</div>';
          document.getElementById('source-menu')?.remove();
          const menu = document.createElement('div');
          menu.id = 'source-menu';
          menu.innerHTML = '<div data-automation-id="promptOption">Career Site</div>' +
            '<div data-automation-id="promptOption" id="board">Job Board</div>';
          document.body.appendChild(menu);
          menu.querySelectorAll('[data-automation-id="promptOption"]').forEach(n => n.onclick = () => {
            document.getElementById('source').innerText = n.innerText;
            menu.remove();
          });
        }
      </script>''')
    page.locator('#us').evaluate("e => e.addEventListener('click', () => e.dataset.clicked='yes')")
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('source', 'How Did You Hear About Us?', 'select', True, name='formField-source')
    assert run.choose_option(page.locator('#source'), 'Simplify', field=field)
    assert page.locator('#source').inner_text() == 'Job Board'
    assert run.answers['source'].value == 'Job Board'
    assert page.locator('#us').get_attribute('data-clicked') is None


@pytest.mark.parametrize('question', [
    'Have you previously worked for CoStar Group or its acquired companies?',
    'Have you ever been employed by Itron or worked as a contractor for Itron?',
])
def test_workday_former_employment_radio_uses_confirmed_no(page, monkeypatch, question):
    from agent import answers
    monkeypatch.setattr(answers, 'resume_text', lambda *args: 'Experience: Software Engineering Fellowship')
    company = 'CoStar Group' if 'CoStar' in question else 'Itron'
    page.set_content(f'''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-previouslyEmployed">
        <label>{question}*</label><div class="options">
          <label><input type="radio" name="former" value="yes" required>Yes</label>
          <label><input type="radio" name="former" value="no" required>No</label>
        </div>
      </div></div>''')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'previously_employed_here': 'No'}}
    job = _job()
    job.company = company
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    assert page.locator('input[value="no"]').is_checked()
    assert not page.locator('input[value="yes"]').is_checked()
    field = next(f for f in run.fields.values() if f.kind == 'radio')
    assert field.label == question
    assert run.answers[field.key].value == 'No'
    assert run.answers[field.key].source == 'profile'


def test_workday_employment_conflict_requires_review(page, monkeypatch):
    from agent import answers
    monkeypatch.setattr(answers, 'resume_text', lambda *args: 'Past employment: CoStar Group')
    page.set_content('''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-former"><label>Have you previously worked for CoStar Group?*</label>
      <div><label><input name="former" type="radio" required>Yes</label>
      <label><input name="former" type="radio" required>No</label></div></div></div>''')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'previously_employed_here': 'No'}}
    job = _job()
    job.company = 'CoStar Group'
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    assert page.locator('input:checked').count() == 0
    field = next(f for f in run.fields.values() if f.kind == 'radio')
    assert run.answers[field.key].source == 'unknown'


def test_workday_prompt_buttons_fill_grounded_yes_no_and_hold_broad_relationship(page, monkeypatch):
    from agent import answers
    page.set_content('''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-workEligible"><div data-automation-id="formLabel">Are you legally eligible to work in the country to which you are applying?*</div>
        <button data-automation-id="promptButton" id="work" onclick="openMenu(this)">Select One</button></div>
      <div data-automation-id="formField-relocate"><div data-automation-id="formLabel">Are you willing to relocate if required?*</div>
        <button data-automation-id="promptButton" id="relocate" onclick="openMenu(this)">Select One</button></div>
      <div data-automation-id="formField-relationship"><div data-automation-id="formLabel">Do you have a relative or person with whom you have a close personal relationship who works at, or is interviewing at, the Company?*</div>
        <button data-automation-id="promptButton" id="relationship" onclick="openMenu(this)">Select One</button></div>
      <div id="choices" role="listbox" style="display:none"><div role="option">Yes</div><div role="option">No</div></div>
    </div><script>
      let current;
      function openMenu(btn) { current = btn; document.querySelector('#choices').style.display = 'block'; }
      document.querySelectorAll('[role=option]').forEach(opt => opt.onclick = () => {
        current.innerText = opt.innerText; document.querySelector('#choices').style.display = 'none';
      });
      document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelector('#choices').style.display = 'none'; });
    </script>''')
    def profile_answers(fields, *args, **kwargs):
        return [workday.Answer(f.key, 'No' if 'relationship' in f.label else 'Yes', 'profile') for f in fields]
    monkeypatch.setattr(answers, 'answer_fields', profile_answers)
    ctx = _Ctx(page)
    ctx.profile = {'work_authorization': {'authorized_us': 'Yes'},
                   'preferences': {'willing_to_relocate': 'Yes', 'relatives_at_company': 'No'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('Application Questions 1 of 2')
    assert page.locator('#work').inner_text() == 'Yes'
    assert page.locator('#relocate').inner_text() == 'Yes'
    assert page.locator('#relationship').inner_text() == 'Select One'
    relationship = next(f for f in run.fields.values() if 'relationship' in f.label)
    assert relationship.required
    assert run.answers[relationship.key].source == 'unknown'
    assert not workday.submit_decision(list(run.fields.values()), list(run.answers.values()), 'full', False)[0]


def test_workday_itron_prompt_button_resolves_former_employment(page, monkeypatch):
    from agent import answers
    monkeypatch.setattr(answers, 'resume_text', lambda: 'Software Engineer at Open Source Foundation')
    page.set_content('''<div data-automation-id="applyFlowPage"><div data-automation-id="formField-former">
      <div data-automation-id="formLabel">Have you ever been employed by Itron or worked as a contractor for Itron?*</div>
      <button id="former" data-automation-id="promptButton" onclick="document.querySelector('#choices').style.display='block'">Select One</button>
      <div id="choices" role="listbox" style="display:none"><div role="option" onclick="choose(this)">Yes</div>
        <div role="option" onclick="choose(this)">No</div></div></div></div><script>
      function choose(opt) { document.querySelector('#former').innerText = opt.innerText;
        document.querySelector('#choices').style.display='none'; }
      document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelector('#choices').style.display='none'; });
    </script>''')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'previously_employed_here': 'No'}}
    job = _job()
    job.company = 'Itron'
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    assert page.locator('#former').inner_text() == 'No'
    field = next(iter(run.fields.values()))
    assert field.label.startswith('Have you ever been employed by Itron')
    assert run.answers[field.key].source == 'profile'


@pytest.mark.parametrize('company,question', [
    ('Itron', 'Have you ever been employed by Itron or worked as a contractor for Itron?'),
    ('CoStar Group', 'Have you previously worked for CoStar Group or its acquired companies?'),
])
def test_workday_hidden_native_yes_no_radios_are_extracted_and_verified(page, monkeypatch, company, question):
    from agent import answers
    monkeypatch.setattr(answers, 'resume_text', lambda: 'Experience: Software Engineering Fellowship')
    page.set_content(f'''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-previouslyEmployed">
        <div data-automation-id="formLabel">{question}*</div>
        <div class="options">
          <input type="radio" id="former-yes" name="former" value="yes" style="display:none">
          <label for="former-yes">Yes</label>
          <input type="radio" id="former-no" name="former" value="no" style="display:none">
          <label for="former-no">No</label>
        </div>
      </div></div>''')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'previously_employed_here': 'No'}}
    job = _job()
    job.company = company
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    assert page.locator('#former-no').is_checked()
    assert not page.locator('#former-yes').is_checked()
    assert len([f for f in run.fields.values() if f.kind == 'radio']) == 1
    field = next(f for f in run.fields.values() if f.kind == 'radio')
    assert field.required
    assert run.answers[field.key].value == 'No'
    assert run.answers[field.key].source == 'profile'


def test_workday_itron_referral_other_after_named_boards_and_no_fabricated_site(page):
    page.set_content('''<div data-automation-id="formField-source">
      <div data-automation-id="formLabel">How Did You Hear About Us?*</div>
      <button data-automation-id="promptButton" data-aa-key="source" id="source"
        aria-controls="sources" onclick="document.querySelector('#sources').style.display='block'">Select One</button></div>
      <div id="sources" role="listbox" style="display:none;overflow-y:auto;height:55px">
        <div role="option">51job.com</div><div role="option">APEC</div>
        <div role="option">Bumeran</div><div role="option">Cadremploi</div>
        <div role="option">Diversity Job Board</div><div role="option">Hellowork</div>
        <div role="option">Indeed.com</div><div role="option">Infojobs</div>
        <div role="option">Irishjobs</div><div role="option">JobsForHer</div>
        <div role="option">Jobstreet</div><div role="option">Naukri</div>
      </div><script>
        (() => {
        const menu = document.querySelector('#sources');
        menu.onscroll = () => {
          if (menu.scrollTop > 35 && !document.querySelector('#other')) {
            menu.insertAdjacentHTML('beforeend', '<div role="option" id="other">Other</div>');
            document.querySelector('#other').onclick = () => {
              document.querySelector('#source').innerText = 'Other'; menu.style.display='none';
            };
          }
        };
        document.addEventListener('keydown', e => { if (e.key === 'Escape') menu.style.display='none'; });
        })();
      </script>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('source', 'How Did You Hear About Us?', 'select', True)
    assert run.choose_option(page.locator('#source'), 'Simplify', field=field)
    assert page.locator('#source').inner_text() == 'Other'
    assert run.answers['source'].value == 'Other'


def test_workday_itron_lazy_referral_search_finds_exact_simplify_after_naukri(page):
    page.set_content('''<div data-automation-id="formField-source">
      <div data-automation-id="formLabel">How Did You Hear About Us?*</div>
      <button data-automation-id="promptButton" data-aa-key="source" id="source" aria-controls="sources"
        onclick="document.querySelector('#sources').style.display='block'">Select One</button></div>
      <div id="sources" role="listbox" style="display:none;overflow-y:auto;height:55px">
        <div role="option" id="board">Job Board</div>
        <div role="option">51job.com</div><div role="option">APEC</div><div role="option">Bumeran</div>
        <div role="option">Cadremploi</div><div role="option">Diversity Job Board</div>
        <div role="option">Indeed.com</div><div role="option">Naukri</div>
      </div><script>
        (() => {
        const menu = document.querySelector('#sources');
        menu.onscroll = () => {
          if (menu.scrollTop > 45 && !document.querySelector('#simplify')) {
            menu.insertAdjacentHTML('beforeend', '<div role="option" id="simplify">Simplify</div>');
            document.querySelector('#simplify').onclick = () => {
              document.querySelector('#source').innerText = 'Simplify'; menu.style.display='none';
            };
          }
        };
        document.querySelector('#board').onclick = () => { throw new Error('picked category too soon'); };
        document.addEventListener('keydown', e => { if (e.key === 'Escape') menu.style.display='none'; });
        })();
      </script>''')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'how_did_you_hear': 'Simplify'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    assert page.locator('#source').inner_text() == 'Simplify'
    assert run.answers['source'].source == 'profile'
    assert page.locator('#board').get_attribute('data-clicked') is None


def test_workday_hp_referral_opens_job_board_category_and_selects_simplify(page):
    page.set_content('''<div data-automation-id="formField-source">
      <div data-automation-id="formLabel">How Did You Hear About Us?*</div>
      <button data-automation-id="promptButton" data-aa-key="source" id="source"
        aria-controls="sources" onclick="document.querySelector('#sources').style.display='block'">Select One</button></div>
      <div id="sources" role="listbox" style="display:none">
        <div role="option" id="advertising">Advertising</div>
        <div role="option" id="career">Career Site</div>
        <div role="option" id="board">Job Board</div>
        <div role="option" id="recruiter">Recruiter Outreach</div>
        <div role="option" id="events">Recruiting Events</div>
      </div><script>
        (() => {
          const menu = document.querySelector('#sources');
          document.querySelector('#board').onclick = () => {
            menu.innerHTML = '<div role="option" id="other">Other</div>' +
                             '<div role="option" id="simplify">Simplify</div>';
            document.querySelector('#other').onclick = () => { throw new Error('premature generic selection'); };
            document.querySelector('#simplify').onclick = () => {
              document.querySelector('#source').innerText = 'Simplify'; menu.style.display='none';
            };
          };
          document.addEventListener('keydown', e => {
            if (e.key === 'Escape') menu.style.display='none';
          });
        })();
      </script>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('source', 'How Did You Hear About Us?', 'select', True)
    assert run.choose_option(page.locator('#source'), 'Simplify', field=field)
    assert page.locator('#source').inner_text() == 'Simplify'
    assert run.answers['source'].value == 'Simplify'


@pytest.fixture
def workday_choice(page, monkeypatch):
    """Exercise extraction, profile answering and verified filling with real browser controls."""
    def offline(*args, **kwargs):
        raise llm.LLMUnavailable('offline regression')

    monkeypatch.setattr(llm, 'generate_json', offline)

    def render(question, options, kind='prompt', current='Select One'):
        page.set_content('''<div data-automation-id="applyFlowPage">
          <div data-automation-id="formField-test" aria-required="true">
            <label id="question" for="choice" data-automation-id="formLabel"></label>
            <div id="control"></div></div>
          <div id="choices" role="listbox" style="display:none"></div></div>''')
        page.evaluate('''({question, options, kind, current}) => {
          document.querySelector('#question').textContent = question;
          const container = document.querySelector('#control');
          if (kind === 'radio') {
            for (const text of options) {
              const label = document.createElement('label');
              const radio = document.createElement('input');
              radio.type = 'radio'; radio.name = 'choice'; radio.value = text;
              radio.required = true; radio.checked = text === current;
              label.append(radio, text); container.append(label);
            }
            return;
          }
          const choice = document.createElement(kind === 'native' ? 'select' : 'button');
          choice.id = 'choice'; choice.setAttribute('aria-required', 'true');
          container.append(choice);
          if (kind === 'native') {
            for (const text of ['Select One', ...options])
              choice.add(new Option(text, text, false, text === current));
          } else {
            choice.dataset.automationId = 'promptButton';
            choice.textContent = current;
            const list = document.querySelector('#choices');
            choice.onclick = () => list.style.display = 'block';
            for (const text of options) {
              const option = document.createElement('div');
              option.setAttribute('role', 'option'); option.textContent = text;
              option.onclick = () => { choice.textContent = text; list.style.display = 'none'; };
              list.append(option);
            }
            document.addEventListener('keydown', e => {
              if (e.key === 'Escape') list.style.display = 'none';
            });
          }
        }''', {'question': question, 'options': options, 'kind': kind, 'current': current})
        return page

    return render


@pytest.mark.parametrize('kind', ['native', 'prompt'])
@pytest.mark.parametrize('options,expected', [
    (['Indeed.com', 'Other'], 'Other'),
    (['Job Board - Indeed', 'Other', 'Job Board - Other'], 'Job Board - Other'),
])
def test_workday_referral_uses_saved_source_when_initial_options_only_offer_other(workday_choice, kind, options, expected):
    page = workday_choice('How Did You Hear About Us?', options, kind)
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'how_did_you_hear': 'Simplify'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    field = next(iter(run.fields.values()))
    assert run.answers[field.key].value == expected
    assert run.answers[field.key].source == 'profile'
    assert field.current_value == expected


@pytest.mark.parametrize('source,options', [
    (None, ['Other', 'Job Board']),
    ('Simplify', ['Career Center Advisor', 'Career Fair', 'Employee Referral', 'School Career Website']),
])
def test_workday_required_referral_needs_a_truthful_source(workday_choice, source, options):
    page = workday_choice('How Did You Hear About Us?', options)
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'how_did_you_hear': source}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    run.fill_step('My Information', repair=['The field How Did You Hear About Us? is required.'])
    assert page.locator('#choice').inner_text() == 'Select One'
    result = run.review()
    assert result.status == workday.ApplyStatus.NEEDS_REVIEW
    assert result.unanswered_required == ['How Did You Hear About Us?']


@pytest.mark.parametrize('kind', ['native', 'prompt', 'radio'])
@pytest.mark.parametrize('known', [False, True, None])
def test_workday_short_employer_yes_no_is_grounded_across_controls(workday_choice, kind, known):
    page = workday_choice('Have you previously been employed by HP?', ['Yes', 'No'], kind, current='No')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'previously_employed_here': known}}
    job = _job()
    job.company = 'Hewlett Packard (HP)'
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    run.fill_step('My Information', repair=[])
    field = next(f for f in run.fields.values() if f.kind in ('radio', 'select'))
    assert field.required
    answer = run.answers[field.key]
    if known is None:
        assert answer.source == 'unknown'
        assert run.review().unanswered_required == [field.label]
    else:
        assert answer.source == 'profile'
        assert answer.value == ('Yes' if known else 'No')
        if kind == 'radio':
            assert page.locator('input:checked').input_value() == answer.value
        else:
            assert field.current_value == answer.value


def test_workday_employer_conflict_reads_the_profile_resume(workday_choice, monkeypatch):
    from agent import answers
    page = workday_choice('Have you previously been employed by HP?', ['Yes', 'No'], current='No')
    paths = []

    def resume(path):
        paths.append(path)
        return 'Experience: Software Engineering Intern at HP'

    monkeypatch.setattr(answers, 'resume_text', resume)
    ctx = _Ctx(page)
    ctx.profile = {'resume_path': '/candidate/resume.pdf', 'preferences': {'previously_employed_here': 'No'}}
    job = _job()
    job.company = 'Hewlett Packard (HP)'
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    assert paths == ['/candidate/resume.pdf']
    assert next(iter(run.answers.values())).source == 'unknown'
    assert run.review().unanswered_required == ['Have you previously been employed by HP?']


def test_workday_broader_relationship_prefill_stays_unknown_on_repair(workday_choice):
    question = 'Do any relatives, domestic partners or roommates work at the Company?'
    page = workday_choice(question, ['Yes', 'No'], current='No')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'relatives_at_company': 'No'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('Application Questions 1 of 2')
    run.fill_step('Application Questions 1 of 2', repair=[])
    assert next(iter(run.answers.values())).source == 'unknown'
    assert run.review().unanswered_required == [question]


@pytest.mark.parametrize('kind', ['native', 'prompt'])
def test_workday_explicit_mobile_alias_is_verified_across_controls(workday_choice, kind):
    page = workday_choice('Phone Device Type', ['Work Mobile', 'Home Mobile', 'Pers Mobile'], kind)
    ctx = _Ctx(page)
    ctx.profile = {'personal': {'phone_device_type': 'Mobile'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    field = next(iter(run.fields.values()))
    assert run.answers[field.key].value == 'Pers Mobile'
    assert run.answers[field.key].source == 'profile'


@pytest.mark.parametrize('leaf,expected', [('Simplify', 'Simplify'), ('Other', 'Other'), ('Indeed.com', None)])
def test_workday_referral_category_display_is_not_a_verified_leaf(workday_choice, leaf, expected):
    page = workday_choice('How Did You Hear About Us?', ['Job Board'])
    page.locator('[role=option]').evaluate('''(option, leaf) => {
      option.onclick = () => {
        document.querySelector('#choice').textContent = 'Job Board';
        const list = document.querySelector('#choices');
        list.replaceChildren();
        const child = document.createElement('div');
        child.setAttribute('role', 'option'); child.textContent = leaf;
        child.onclick = () => { document.querySelector('#choice').textContent = leaf; list.style.display = 'none'; };
        list.append(child);
      };
    }''', leaf)
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'how_did_you_hear': 'Simplify'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    field = next(iter(run.fields.values()))
    assert run.answers[field.key].value == expected
    assert (run.answers[field.key].source == 'profile') is (expected is not None)
    assert run.review().unanswered_required == ([] if expected else ['How Did You Hear About Us?'])


@pytest.mark.parametrize('kind', ['native', 'prompt', 'radio'])
def test_workday_factual_yes_no_prefill_is_not_profile_evidence(workday_choice, kind):
    question = 'Have you ever held a security clearance?'
    page = workday_choice(question, ['Yes', 'No'], kind, current='No')
    run = workday._Run(_job(), _Ctx(page))
    run.fill_step('Application Questions')
    run.fill_step('Application Questions', repair=[])
    assert next(iter(run.answers.values())).source == 'unknown'
    assert run.review().unanswered_required == [question]


def test_workday_boolean_false_radio_answer_is_verified(workday_choice, monkeypatch):
    from agent import answers
    page = workday_choice('Are you authorized to work in the US?', ['Yes', 'No'], kind='radio')
    monkeypatch.setattr(answers, 'answer_fields', lambda fields, *a, **k: [
        workday.Answer(f.key, False, 'profile') for f in fields])
    run = workday._Run(_job(), _Ctx(page))
    run.fill_step('Application Questions')
    assert page.locator('input:checked').input_value() == 'No'
    assert next(iter(run.answers.values())).value == 'No'


def test_workday_radio_recovers_when_a_stale_prompt_covers_its_label(workday_choice):
    page = workday_choice('Have you ever been employed by Itron?', ['Yes', 'No'], kind='radio')
    page.evaluate('''() => {
      const popup = document.createElement('div');
      popup.setAttribute('data-automation-id', 'promptLeafNode');
      popup.style.cssText = 'position:fixed;inset:0;z-index:100;background:white';
      document.body.append(popup);
    }''')
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'previously_employed_here': 'No'}}
    job = _job()
    job.company = 'Itron'
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    assert page.locator('input:checked').input_value() == 'No'
    assert next(iter(run.answers.values())).source == 'profile'


@pytest.mark.parametrize('input_html,value', [
    ('<input placeholder="MM/DD/YYYY">', 'May 2028'),
    ('<input type="date">', 'May 2028'),
    ('<input placeholder="MM/YYYY">', '2028'),
])
def test_workday_date_requires_the_precision_requested_by_the_control(page, input_html, value):
    page.set_content(input_html)
    run = workday._Run(_job(), _Ctx(page))
    assert not run.fill_date(page.locator('input'), value)
    assert page.locator('input').input_value() == ''


def test_workday_desired_start_uses_availability_without_overwriting_employment(page, monkeypatch):
    from agent import answers
    page.set_content('''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-desiredStart"><label for="desired">What is your desired start date?*</label>
        <input id="desired" type="date" required></div>
      <div role="group" aria-labelledby="Work-Experience-1-panel">
        <h3 id="Work-Experience-1-panel">Work Experience 1</h3>
        <div data-automation-id="formField-startDate"><label for="from">From*</label>
          <input id="from" placeholder="MM/YYYY" required></div>
        <label><input id="current" type="checkbox">I currently work here</label>
      </div></div>''')
    monkeypatch.setattr(answers, 'answer_fields', lambda fields, *a, **k: [
        workday.Answer(f.key, '09/2025' if f.label.endswith('From') else None,
                      'profile' if f.label.endswith('From') else 'unknown') for f in fields])
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'available_start': '2027-05-17'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Experience')
    assert page.locator('#desired').input_value() == '2027-05-17'
    assert page.locator('#from').input_value() == '09/2025'
    assert not page.locator('#current').is_checked()
    assert not any(a.value == '2024' for a in run.answers.values())


@pytest.mark.parametrize('kind', ['hidden_native', 'aria'])
@pytest.mark.parametrize('confirmed', [False, True])
def test_workday_single_terms_checkbox_requires_explicit_consent_and_real_state(page, monkeypatch, kind, confirmed):
    question = 'Yes, I have read and consent to the terms and conditions'
    control = ('<input id="terms" type="checkbox" style="display:none" required>' if kind == 'hidden_native' else
               '<button id="terms" type="button" role="checkbox" aria-checked="false" aria-required="true" '
               'onclick="this.setAttribute(\'aria-checked\',this.getAttribute(\'aria-checked\')===\'false\')"></button>')
    page.set_content(f'''<div data-automation-id="applyFlowPage">
      <div data-automation-id="formField-terms" aria-required="true">
        <label data-automation-id="formLabel" for="terms">{question}*</label>{control}
      </div></div>''')
    monkeypatch.setattr(llm, 'generate_json', lambda *a, **k: pytest.fail('consent must not be inferred'))
    ctx = _Ctx(page)
    if confirmed:
        ctx.profile = {'company_answers': {_job().company: {question: True}}}
    run = workday._Run(_job(), ctx)
    run.fill_step('Voluntary Disclosures')
    field = next(iter(run.fields.values()))
    assert field.required and field.kind == 'checkbox' and field.options == []
    actual = page.locator('#terms').evaluate("el => el.matches('input') ? el.checked : el.getAttribute('aria-checked') === 'true'")
    assert actual is confirmed
    assert (run.answers[field.key].source == 'bank') is confirmed
    if confirmed:
        page.locator('label[for="terms"]').click()
        assert run.answers[field.key].value is True
    assert run.review().unanswered_required == [question]


def test_workday_rejected_checkbox_click_is_not_forged_or_counted_as_an_answer(page):
    question = 'I agree to the terms and conditions'
    page.set_content(f'''<div data-automation-id="applyFlowPage"><div data-automation-id="formField-terms">
      <label for="terms" data-automation-id="formLabel">{question}*</label>
      <input id="terms" type="checkbox" required onclick="event.preventDefault()">
      </div></div>''')
    ctx = _Ctx(page)
    ctx.profile = {'company_answers': {_job().company: {question: True}}}
    run = workday._Run(_job(), ctx)
    run.fill_step('Voluntary Disclosures')
    assert not page.locator('#terms').is_checked()
    assert next(iter(run.answers.values())).source == 'unknown'
    assert run.review().status == workday.ApplyStatus.NEEDS_REVIEW


def test_workday_checkbox_group_clears_other_selected_answers(page, monkeypatch):
    from agent import answers
    page.set_content('''<div data-automation-id="applyFlowPage"><div data-automation-id="formField-ethnicityMulti">
      <div data-automation-id="formLabel">Please select your ethnicity*</div>
      <label><input type="checkbox" checked>Asian</label>
      <label><input type="checkbox">I do not wish to answer</label>
      </div></div>''')
    monkeypatch.setattr(answers, 'answer_fields', lambda fields, *a, **k: [
        workday.Answer(f.key, ['I do not wish to answer'], 'profile') for f in fields])
    ctx = _Ctx(page)
    ctx.profile = {'eeo': {'race_ethnicity': 'I do not wish to answer'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('Voluntary Disclosures')
    assert page.locator('input:checked').count() == 1
    assert page.locator('input:checked').locator('..').inner_text() == 'I do not wish to answer'
    assert next(iter(run.answers.values())).value == 'I do not wish to answer'


@pytest.mark.parametrize('source,start,education,clean', [
    ('Other', '05/17/2027', 'Stony Brook University', True),
    ('LinkedIn', '05/17/2027', 'Stony Brook University', False),
    ('Other', '01/01/2024', 'Stony Brook University', False),
    ('Other', '05/17/2027', 'No Response', False),
])
def test_workday_review_verifies_rendered_summary_before_readiness_or_submit(page, monkeypatch, source, start, education, clean):
    from agent import captcha
    page.set_content(f'''<div data-automation-id="applyFlowPage">{_progress('Review')}
      <div data-automation-id="formField-source"><label data-automation-id="formLabel">How Did You Hear About Us?</label>
        <div>{source}</div></div>
      <div data-automation-id="formField-desiredStart"><label data-automation-id="formLabel">What is your desired start date?</label>
        <div>{start}</div></div>
      <section><h3>Education</h3><p>{education}</p></section>
      <button id="submit" data-automation-id="bottom-navigation-next-button" onclick="this.dataset.clicked='true'">Submit</button>
      </div>''')
    monkeypatch.setattr(captcha, 'solve_if_present', lambda *a: pytest.fail('review must block before CAPTCHA or submit'))
    ctx = _Ctx(page)
    ctx.submit_mode, ctx.dry_run = 'full', clean
    ctx.profile = {'preferences': {'how_did_you_hear': 'Simplify', 'available_start': '2027-05-17'},
                   'education': {'school': 'Stony Brook University'}}
    run = workday._Run(_job(), ctx)
    for field, value in [(workday.Field('source', 'How Did You Hear About Us?', 'select', True, name='formField-source'), 'Simplify'),
                         (workday.Field('start', 'What is your desired start date?', 'date', True), '2027-05-17')]:
        run.fields[field.key] = field
        run.answers[field.key] = workday.Answer(field.key, value, 'profile')
    result = run.review()
    assert result.status == (workday.ApplyStatus.DRY_RUN_OK if clean else workday.ApplyStatus.NEEDS_REVIEW)
    assert (not result.unanswered_required) is clean
    assert page.locator('#submit').get_attribute('data-clicked') is None


def test_workday_stale_saved_selection_is_rechecked_at_review(workday_choice):
    page = workday_choice('How Did You Hear About Us?', ['Simplify', 'LinkedIn'], kind='native')
    ctx = _Ctx(page)
    ctx.submit_mode = 'full'
    ctx.profile = {'preferences': {'how_did_you_hear': 'Simplify'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('My Information')
    page.locator('#choice').select_option(label='LinkedIn')
    assert next(iter(run.answers.values())).value == 'Simplify'
    result = run.review()
    assert result.status == workday.ApplyStatus.NEEDS_REVIEW
    assert result.unanswered_required == ['How Did You Hear About Us?']


def test_workday_prefilled_text_is_not_promoted_to_profile_evidence(page, monkeypatch):
    from agent import answers
    page.set_content('''<div data-automation-id="applyFlowPage"><label for="salary">Desired salary*</label>
      <input id="salary" value="100000" required></div>''')
    monkeypatch.setattr(answers, 'answer_fields', lambda fields, *a, **k: [
        workday.Answer(f.key, None, 'unknown') for f in fields])
    run = workday._Run(_job(), _Ctx(page))
    run.fill_step('Application Questions')
    assert next(iter(run.answers.values())).source == 'unknown'
    assert run.review().unanswered_required == ['Desired salary']


def test_workday_candidate_identifier_is_not_mistaken_for_a_date(workday_choice):
    page = workday_choice('Have you previously been employed by HP?', ['Yes', 'No'])
    page.locator('[data-automation-id="formField-test"]').evaluate(
        "el => el.setAttribute('data-automation-id', 'formField-candidateIsPreviousWorker')")
    ctx = _Ctx(page)
    ctx.profile = {'preferences': {'previously_employed_here': 'No'}}
    job = _job()
    job.company = 'HP'
    run = workday._Run(job, ctx)
    run.fill_step('My Information')
    field = next(iter(run.fields.values()))
    assert not run._is_date_field(field)
    assert page.locator('#choice').inner_text() == 'No'
    assert run.answers[field.key].value == 'No'


def test_workday_declining_self_identification_does_not_become_no(workday_choice):
    page = workday_choice('Are you Hispanic or Latino?', ['Yes', 'No'])
    ctx = _Ctx(page)
    ctx.profile = {'eeo': {'hispanic_latino': 'Decline to self-identify'}}
    run = workday._Run(_job(), ctx)
    run.fill_step('Voluntary Disclosures')
    assert page.locator('#choice').inner_text() == 'Select One'
    assert next(iter(run.answers.values())).source == 'unknown'
    assert run.review().unanswered_required == ['Are you Hispanic or Latino?']


def test_workday_search_text_without_a_selected_item_is_not_verified(page):
    page.set_content('''<div data-automation-id="formField-fieldOfStudy">
      <label>Field of Study*</label><div data-automation-id="multiselectInputContainer">
        <input data-aa-key="major" id="major"></div>
      <div role="listbox"><div role="option">Computer Science</div></div></div>''')
    run = workday._Run(_job(), _Ctx(page))
    field = workday.Field('major', 'Field of Study', 'combobox', True)
    assert not run.choose_option(page.locator('#major'), 'Computer Science', multi=True, field=field)
    assert page.locator('#major').input_value() == 'Computer Science'
    assert 'major' not in run.answers


def test_workday_review_rechecks_after_captcha_before_recording_submit_intent(page, monkeypatch):
    from agent import captcha
    page.set_content(f'''<div data-automation-id="applyFlowPage">{_progress('Review')}
      <div data-automation-id="formField-source"><label data-automation-id="formLabel">How Did You Hear About Us?</label>
        <div id="source">Simplify</div></div>
      <button data-automation-id="bottom-navigation-next-button" onclick="this.dataset.clicked='true'">Submit</button>
      </div>''')
    ctx = _Ctx(page)
    ctx.submit_mode, ctx.dry_run = 'full', False
    ctx.profile = {'preferences': {'how_did_you_hear': 'Simplify'}}
    ctx.mark_submit_attempt = lambda *a: pytest.fail('must block before submit intent')
    run = workday._Run(_job(), ctx)
    field = workday.Field('source', 'How Did You Hear About Us?', 'select', True, name='formField-source')
    run.fields[field.key] = field
    run.answers[field.key] = workday.Answer(field.key, 'Simplify', 'profile')

    def solve(*args):
        page.locator('#source').evaluate("el => el.innerText = 'LinkedIn'")
        return captcha.CaptchaOutcome('none')

    monkeypatch.setattr(captcha, 'solve_if_present', solve)
    result = run.review()
    assert result.status == workday.ApplyStatus.NEEDS_REVIEW
    assert 'review verification failed' in result.reason
    assert page.locator(workday.NEXT).get_attribute('data-clicked') is None
