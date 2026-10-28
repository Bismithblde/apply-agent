"""Offline tests for the form engine, answer matcher, captcha handling, and submit gating.
Local HTML fixtures only (page.set_content); agent.llm is always mocked."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from agent import answers, captcha, db, form, llm
from agent.ats.base import ApplyContext
from agent.models import Answer, ApplyStatus, Field, Job

FIX = Path(__file__).parent / "fixtures"

PROFILE = {
    "resume_path": "/nonexistent/resume.pdf",
    "personal": {"first_name": "Ryan", "last_name": "Chen", "full_name": "Ryan Chen", "preferred_name": "Ryan",
                 "email": "ryan@example.com", "phone": "718-219-8854", "phone_country": "United States (+1)",
                 "city": "Stony Brook", "state": "New York", "state_abbr": "NY", "country": "United States",
                 "location": "Stony Brook, NY", "linkedin": "https://www.linkedin.com/in/ryan",
                 "github": "https://github.com/ryan", "website": "https://ryan.dev"},
    "education": {"school": "Stony Brook University", "degree": "Bachelor's", "degree_full": "Bachelor of Science",
                  "major": "Computer Science", "gpa": "3.74", "graduation_month": "May", "graduation_year": "2028",
                  "graduation_date": "May 2028", "start_month": "August", "start_year": "2024", "current_year": "Junior"},
    "work_authorization": {"authorized_us": "Yes", "us_citizen": "Yes", "needs_sponsorship_now": "No",
                           "needs_sponsorship_future": "No", "citizenship_country": "United States"},
    "preferences": {"willing_to_relocate": "Yes", "available_start": "May 2027", "available_end": "August 2027",
                    "over_18": "Yes", "how_did_you_hear": "Simplify", "previously_employed_here": "No"},
    "eeo": {"gender": "Decline to self-identify", "race_ethnicity": "Decline to self-identify",
            "veteran_status": "I don't wish to answer", "disability_status": "I do not want to answer",
            "hispanic_latino": "Decline to self-identify"},
    "answer_bank": {"why_company": "I like shipping real products.", "about_me": "CS student at Stony Brook."},
}
JOB = Job(id="test:1", company="Acme", title="SWE Intern", url="https://example.com/jobs/1", locations=["NY"],
          category="Software", date_posted=0)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def _load(page, name: str):
    page.set_content((FIX / name).read_text())
    return page


def _by_label(fields: list[Field]) -> dict[str, Field]:
    return {f.label: f for f in fields}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def boom(*a, **k):
        raise llm.LLMUnavailable("offline test")
    monkeypatch.setattr(answers.llm, "generate_json", boom)
    monkeypatch.setattr(answers, "resume_text", lambda path=None: "RESUME: Stony Brook CS student, C++ KV store.")


# ---------- extraction ----------

def test_extract_labels_kinds_required(page):
    fields = form.extract_fields(_load(page, "form_basic.html"))
    f = _by_label(fields)
    assert set(f) == {
        "First Name", "Last Name", "Email", "Phone", "Resume/CV", "Degree",
        "Are you legally authorized to work in the United States?", "Languages (check all that apply)",
        "School", "Requesting visa sponsorship?", "Education: Field of Study", "Why do you want to join us?",
        "I agree to the privacy policy",
    }
    assert f["First Name"].required and f["First Name"].kind == "text"      # trailing * in <label for>
    assert f["Last Name"].required                                          # required attribute
    assert f["Email"].required and f["Email"].kind == "email"              # aria-label + aria-required
    assert f["Phone"].kind == "tel" and not f["Phone"].required            # aria-labelledby
    assert f["Why do you want to join us?"].kind == "textarea"             # nearest preceding text
    assert f["Requesting visa sponsorship?"].required                       # label class *required*
    assert all(x.key.startswith("aa-") for x in fields)
    assert len({x.key for x in fields}) == len(fields)


def test_extract_options_groups_combobox_file(page):
    f = _by_label(form.extract_fields(_load(page, "form_basic.html")))
    assert f["Degree"].kind == "select"
    assert f["Degree"].options == ["High School", "Bachelor's Degree", "Master's Degree"]
    auth = f["Are you legally authorized to work in the United States?"]
    assert auth.kind == "radio" and auth.required
    assert auth.options == ["Yes, I am authorized", "No, I am not authorized"]
    langs = f["Languages (check all that apply)"]
    assert langs.kind == "checkbox" and langs.options == ["English", "Spanish", "Mandarin"]
    school = f["School"]
    assert school.kind == "combobox" and school.required
    assert "Stony Brook University" in school.options                        # opened to read options
    yn = f["Requesting visa sponsorship?"]
    assert yn.kind == "radio" and yn.options == ["Yes", "No"]
    resume = f["Resume/CV"]
    assert resume.kind == "file" and resume.required                         # hidden input behind "Attach"
    consent = f["I agree to the privacy policy"]
    assert consent.kind == "checkbox" and consent.options == [] and consent.required


PINPOINT_LIKE = """<form><fieldset><legend>3. Questions</legend>
  <div><label class="external-form__label" for="q0_true"><span class="external-form__label--required">Are you legally
    authorized to work in the United States of America?</span></label>
    <div class="frow"><div><input type="radio" id="q0_true" name="q0" value="true"><label for="q0_true">Yes</label></div>
    <div><input type="radio" id="q0_false" name="q0" value="false"><label for="q0_false">No</label></div></div></div>
  <div><label>Will you require sponsorship?</label>
    <div class="frow"><div><input type="radio" id="q1_true" name="q1"><label for="q1_true">Yes</label></div>
    <div><input type="radio" id="q1_false" name="q1"><label for="q1_false">No</label></div></div></div>
  <div><label>Which time zone?</label>
    <div class="css-control" style="position:relative"><div class="react-select__placeholder"
      style="position:absolute;left:0;top:0;width:200px;height:30px">Select...</div>
    <input role="combobox" aria-label="Which time zone?" aria-expanded="false" aria-controls="tz-list"
      style="width:4px;height:26px" onfocus="" onkeydown="if(event.key==='ArrowDown'){document.getElementById('tz-list').hidden=false}">
    <div id="tz-list" role="listbox" hidden>
      <div role="option" onclick="this.closest('.css-control').querySelector('.react-select__placeholder').textContent=this.textContent;this.parentElement.hidden=true">Eastern</div>
      <div role="option">Pacific</div></div></div></div>
</fieldset></form>"""


def test_page_wide_fieldset_legend_does_not_label_questions(page):
    page.set_content(PINPOINT_LIKE)
    f = _by_label(form.extract_fields(page, probe_options=False))
    assert "Are you legally authorized to work in the United States of America?" in f
    assert f["Are you legally authorized to work in the United States of America?"].required
    assert "Will you require sponsorship?" in f
    assert "3. Questions" not in f


def test_combobox_covered_by_placeholder_still_opens(page):
    page.set_content(PINPOINT_LIKE)
    tz = _by_label(form.extract_fields(page))["Which time zone?"]
    assert tz.options == ["Eastern", "Pacific"]


def test_extract_keys_stable_across_passes(page):
    _load(page, "form_basic.html")
    first = {f.label: f.key for f in form.extract_fields(page)}
    second = {f.label: f.key for f in form.extract_fields(page, probe_options=False)}
    assert first == second


def test_extract_root_scoping(page):
    _load(page, "form_basic.html")
    assert form.extract_fields(page, root="#does-not-exist") == []
    assert [f.label for f in form.extract_fields(page, root=".education-entry")] == ["Education: Field of Study"]


def test_numbered_resume_examples_keep_the_full_prompt_and_fill_distinct_evidence(page, monkeypatch):
    prompt = ("We look for evidence of exceptional ability. Please provide 3 examples using quantitative metrics. "
              + "Capture the problem, solution, and result with technical detail. " * 7 + "First example:")
    page.set_content(f'''<form>
      <label for="first">{prompt} *</label><textarea id="first" required></textarea>
      <label for="second">Second example: *</label><textarea id="second" required></textarea>
      <label for="third">Third example: *</label><textarea id="third" required></textarea>
      <fieldset><legend>I understand that this position requires me to work on-site. *</legend>
        <div><input id="onsite" type="checkbox" required><label for="onsite">Yes</label></div>
      </fieldset></form>''')
    resume = ("Projects\nCache | C++, TCP/IP\n"
              "• Sustained 159K operations/second across 512 concurrent clients.\n"
              "• Sharded storage across 64 independent locks.\n"
              "Search | Python, Pinecone\n• Indexed 200,000 products with latency under 100 ms.\n"
              "Matcher | Redis\n• Cut schedule comparisons by 90% for 20,000 profiles.")
    monkeypatch.setattr(answers, "resume_text", lambda path=None: resume)
    fields, responses, failed = form.fill_form(page, JOB, PROFILE)
    assert fields[0].label == prompt and len(fields[0].label) > 400
    assert fields[-1].label == "I understand that this position requires me to work on-site."
    assert fields[-1].kind == "checkbox" and fields[-1].required
    assert failed == [] and page.is_checked("#onsite")
    values = [page.input_value(f"#{name}") for name in ("first", "second", "third")]
    assert len(set(values)) == 3 and all(value and value in resume.replace("\n• ", " ") for value in values)
    assert all(a.source == "bank" and "resume#" in a.note for a in responses[:3])


def test_filling_checkbox_choices_clears_previous_selections(page):
    page.set_content('''<form><fieldset><legend>Are you authorized to work?</legend>
      <label><input type="checkbox" name="auth" value="yes" checked>Yes</label>
      <label><input type="checkbox" name="auth" value="no">No</label>
    </fieldset></form>''')
    fields = form.extract_fields(page)
    assert form.fill_fields(page, fields, [Answer(fields[0].key, ["No"], "profile")]) == []
    assert not page.is_checked('[value="yes"]') and page.is_checked('[value="no"]')


def test_calendar_and_compound_address_fields_use_only_supported_values(page):
    page.set_content('''<form>
      <label for="start">When are you available to start an internship?</label><select id="start">
        <option>October 2026</option><option>January 2027</option><option>None of the above</option></select>
      <label for="grad">What is your graduation date? (anticipated or completed)</label><select id="grad">
        <option>Spring 2026</option><option>Summer 2027</option><option>None of the above/Beyond 2027</option></select>
      <label for="address">City, State &amp; Zip</label><input id="address">
    </form>''')
    profile = {**PROFILE, "personal": {**PROFILE["personal"], "postal_code": "11356"}}
    _, _, failed = form.fill_form(page, JOB, profile)
    assert not failed
    assert page.locator("#start").input_value() == "None of the above"
    assert page.locator("#grad").input_value() == "None of the above/Beyond 2027"
    assert page.locator("#address").input_value() == "Stony Brook, NY 11356"
    assert form.best_option("2028", ["2026", "2027"]) is None
    assert form.best_option("May 2027", ["January 2027", "May 2026"]) is None


def test_source_details_use_adjacent_question_context_after_a_conditional_render(page):
    page.set_content('''<form><label for="source">How did you hear about us?</label>
      <select id="source" onchange="if (!document.getElementById('details'))
        this.insertAdjacentHTML('afterend', '&lt;label for=details&gt;Please specify&lt;/label&gt;&lt;input id=details&gt;')">
        <option value="">Select...</option><option>LinkedIn</option><option>Other (please specify)</option>
      </select></form>''')
    fields, responses, failed = form.fill_form(page, JOB, PROFILE)
    assert failed == [] and len(fields) == 2
    assert page.input_value("#details") == "Simplify"
    assert responses[-1].note == "source specified after Other"


SCHOOL_COMBO = '''<form onsubmit="event.preventDefault(); window.sent = true">
  <label for="school">School *</label><div class="select-control">
    <div class="select__single-value">Aston University</div>
    <input id="school" role="combobox" aria-controls="schools" aria-expanded="false" required
      onfocus="document.getElementById('schools').hidden = false; this.setAttribute('aria-expanded','true')"
      onclick="document.getElementById('schools').hidden = false; this.setAttribute('aria-expanded','true')"
      onkeydown="if(event.key === 'Escape'){document.getElementById('schools').hidden=true; this.setAttribute('aria-expanded','false')}"
      oninput="setTimeout(() => document.getElementById('correct').hidden = !this.value.includes('Stony'), 50)">
    <div id="schools" role="listbox" hidden>
      <div role="option" onclick="window.wrongClicks=(window.wrongClicks || 0)+1">Aston University</div>
      <div id="correct" role="option" hidden onclick="
        document.querySelector('.select__single-value').textContent = 'Stony Brook University';
        document.getElementById('schools').hidden = true;
        document.getElementById('school').setAttribute('aria-expanded','false');
        document.getElementById('school').value = ''">Stony Brook University</div>
    </div></div><button type="submit">Submit application</button></form>'''


def test_async_school_search_never_accepts_similar_initial_options(page):
    page.set_content(SCHOOL_COMBO)
    fields, responses, failed = form.fill_form(page, JOB, PROFILE)
    assert failed == [] and responses[0].value == "Stony Brook University"
    assert page.locator('.select__single-value').inner_text() == "Stony Brook University"
    assert page.evaluate("window.wrongClicks || 0") == 0
    assert form._verify(page, fields[0], "Stony Brook University")


def test_miscommitted_school_blocks_readiness_despite_search_text_and_menu_match(page, monkeypatch):
    page.set_content(SCHOOL_COMBO.replace("textContent = 'Stony Brook University'", "textContent = 'Aston University'")
                     .replace("document.getElementById('school').value = ''", "document.getElementById('school').value = 'Stony Brook University'"))
    fields, responses, failed = form.fill_form(page, JOB, PROFILE, passes=1)
    assert responses[0].value == "Stony Brook University" and failed == [fields[0].key]
    assert not form._verify(page, fields[0], responses[0].value)
    monkeypatch.setattr(captcha, "solve_if_present", lambda *a: pytest.fail("solver called for mismatched selection"))
    result = form.finish_application(JOB, _ctx(page, submit_mode="full", dry_run=True), page,
                                     fields, responses, failed, submit_texts=["Submit application"])
    assert result.status == ApplyStatus.NEEDS_REVIEW and "School" in result.reason
    assert not page.evaluate("window.sent || false")


def test_native_select_rechecks_actual_selection_after_change(page):
    page.set_content('''<label for="school">School</label><select id="school" required onchange="this.value='aston'">
      <option value="">Select...</option><option value="aston">Aston University</option>
      <option value="stony">Stony Brook University</option></select>''')
    fields, responses, failed = form.fill_form(page, JOB, PROFILE, passes=1)
    assert responses[0].value == "Stony Brook University" and failed == [fields[0].key]
    assert page.locator("#school option:checked").inner_text() == "Aston University"


def test_later_field_reset_of_selected_school_blocks_final_readiness(page):
    page.set_content('''<form onsubmit="event.preventDefault(); window.sent = true">
      <label for="school">School</label><select id="school" required>
        <option value="">Select...</option><option value="aston">Aston University</option>
        <option value="stony">Stony Brook University</option></select>
      <label for="first">First Name</label><input id="first" oninput="document.getElementById('school').value='aston'">
      <button type="submit">Submit application</button></form>''')
    fields, responses, failed = form.fill_form(page, JOB, PROFILE, passes=1)
    assert failed == [fields[0].key]
    assert responses[0].value == "Stony Brook University"
    # The final gate independently checks DOM state even when a caller loses the fill-failure list.
    result = form.finish_application(JOB, _ctx(page, submit_mode="full", dry_run=True), page,
                                     fields, responses, [], submit_texts=["Submit application"])
    assert result.status == ApplyStatus.NEEDS_REVIEW and "School" in result.reason
    assert not page.evaluate("window.sent || false")


def test_combobox_options_and_verification_ignore_another_controls_menu(page):
    page.set_content('''<form><label for="school">School</label><div class="select-control">
      <div class="select__single-value">Stony Brook University - SUNY</div>
      <input id="school" role="combobox" aria-controls="schools" aria-expanded="false">
      <div id="schools" role="listbox"></div></div>
      <input role="combobox" aria-label="Other school" aria-controls="other-schools" aria-expanded="true">
      <div id="other-schools" role="listbox"><div role="option" onclick="window.wrongClick=true">
        Stony Brook University</div></div></form>''')
    field = form.extract_fields(page, probe_options=False)[0]
    loc = page.locator("#school")
    assert form._probe_combo(page, field.key) == []
    assert not form._click_option(page, loc, "Stony Brook University")
    assert not page.evaluate("window.wrongClick || false")
    assert form._verify(page, field, "Stony Brook University")


@pytest.mark.parametrize(("country", "matches"), [("us", True), ("ca", False), ("", False)])
def test_country_verification_checks_flag_identity_with_shared_calling_code(page, country, matches):
    page.set_content(f'''<label for="country">Country</label><div class="select-control">
      <div class="select__single-value"><div class="iti__flag iti__{country}"></div><span>+1</span></div>
      <input id="country" role="combobox" aria-expanded="false"></div>''')
    field = form.extract_fields(page, probe_options=False)[0]
    assert form._verify(page, field, "United States +1") is matches


def test_closed_autocomplete_search_text_is_not_a_committed_selection(page):
    page.set_content('''<label for="school">School</label><div class="select-control">
      <div class="select__placeholder">Select...</div><input id="school" role="combobox"
        aria-expanded="false" value="Stony Brook University"></div>''')
    field = form.extract_fields(page, probe_options=False)[0]
    assert not form._verify(page, field, "Stony Brook University")


@pytest.mark.parametrize("reset", ["month", "year"])
def test_date_month_and_year_are_rechecked_after_other_fields(page, reset):
    page.set_content(f'''<form><div class="education-entry">
      <label for="month">End date month</label><select id="month" required>
        <option>January</option><option>May</option></select>
      <label for="year">End date year</label><input id="year" type="number" required value="2027"></div>
      <label for="first">First Name</label><input id="first"
        oninput="document.getElementById('{reset}').value='{'January' if reset == 'month' else '2027'}'"></form>''')
    fields, responses, failed = form.fill_form(page, JOB, PROFILE, passes=1)
    by_name = {field.name: field for field in fields}
    assert failed == [by_name[reset].key]
    assert {a.value for a in responses} == {"May", "2028", "Ryan"}


def test_optional_wrong_school_also_blocks_final_submit(page, monkeypatch):
    page.set_content('''<form onsubmit="event.preventDefault(); window.sent=true">
      <label for="school">School</label><select id="school"><option>Aston University</option>
        <option>Stony Brook University</option></select><button>Submit application</button></form>''')
    fields = form.extract_fields(page, probe_options=False)
    responses = [Answer(fields[0].key, "Stony Brook University", "profile")]
    monkeypatch.setattr(captcha, "solve_if_present", lambda *a: pytest.fail("solver called for wrong school"))
    result = form.finish_application(JOB, _ctx(page, submit_mode="full"), page,
                                     fields, responses, [], submit_texts=["Submit application"])
    assert result.status == ApplyStatus.NEEDS_REVIEW and "School" in result.reason
    assert not page.evaluate("window.sent || false")


def test_native_select_uses_current_dom_options_after_rerender(page):
    page.set_content('<label for="school">School</label><select id="school"><option>Aston University</option></select>')
    fields = form.extract_fields(page, probe_options=False)
    page.locator("#school").evaluate("e => e.innerHTML='<option value=stony>Stony Brook University</option>'")
    assert form.fill_fields(page, fields, [Answer(fields[0].key, "Stony Brook University", "profile")]) == []
    assert page.locator("#school option:checked").inner_text() == "Stony Brook University"


def test_city_autocomplete_commits_full_profile_location_and_rejects_similar_cities(page):
    page.set_content('''<form><label for="city">Location (City)</label><div class="select-control">
      <div class="select__placeholder">Select...</div>
      <input id="city" role="combobox" aria-controls="cities" aria-expanded="false" required
        onclick="document.getElementById('cities').hidden=false; this.setAttribute('aria-expanded','true')"
        oninput="document.getElementById('new-york').hidden=!this.value.includes('New York')"
        onkeydown="if(event.key==='Escape'){document.getElementById('cities').hidden=true;this.setAttribute('aria-expanded','false')}">
      <div id="cities" role="listbox" hidden>
        <div role="option" onclick="window.wrongClick=true">West New York, New Jersey, United States</div>
        <div id="new-york" role="option" hidden onclick="
          const el=document.getElementById('city'); el.value='';el.setAttribute('aria-expanded','false');
          document.getElementById('cities').hidden=true;
          const display=document.querySelector('.select__placeholder');
          display.className='select__single-value';display.textContent=this.textContent;
        ">New York, New York, United States</div>
      </div></div></form>''')
    profile = {"personal": {"city": "New York", "state": "New York", "state_abbr": "NY",
                             "country": "United States", "location": "New York, NY"}}
    fields, responses, failed = form.fill_form(page, JOB, profile)
    assert failed == [] and responses[0].value == "New York, New York, United States"
    assert page.locator('.select__single-value').inner_text() == "New York, New York, United States"
    assert form._verify(page, fields[0], responses[0].value)
    assert not page.evaluate("window.wrongClick || false")


@pytest.mark.parametrize(("wanted", "offered"), [
    ("Stony Brook University", "Aston University"), ("May 2027", "January 2027"),
    ("May 2027", "May 2028"), ("May 2027", "May 2027 - August 2028"),
    ("2028", "Spring 2028"), ("May 2027", "2027"), ("May University", "May Institute"),
])
def test_option_identity_rejects_different_school_or_calendar_components(wanted, offered):
    assert not form.option_matches(wanted, offered)


# ---------- filling ----------

def test_fill_fields_round_trip(page, tmp_path):
    _load(page, "form_basic.html")
    fields = form.extract_fields(page)
    f = _by_label(fields)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    essay = "I want to build reliable infrastructure. " * 8
    vals = {
        "First Name": "Ryan", "Last Name": "Chen", "Email": "ryan@example.com", "Phone": "718-219-8854",
        "Resume/CV": str(resume), "Degree": "Bachelor's", "Are you legally authorized to work in the United States?": "Yes",
        "Languages (check all that apply)": ["English", "Mandarin"], "School": "Stony Brook University",
        "Requesting visa sponsorship?": "No", "Education: Field of Study": "Computer Science",
        "Why do you want to join us?": essay, "I agree to the privacy policy": True,
    }
    ans = [Answer(f[label].key, v, "profile") for label, v in vals.items()]
    assert form.fill_fields(page, fields, ans) == []
    assert page.input_value("#first_name") == "Ryan"
    assert page.input_value("#phone") == "718-219-8854"
    assert page.eval_on_selector("#degree", "e => e.options[e.selectedIndex].text") == "Bachelor's Degree"
    assert page.is_checked("input[name=auth][value=y]")
    assert page.is_checked("input[name=lang][value=en]") and page.is_checked("input[name=lang][value=zh]")
    assert not page.is_checked("input[name=lang][value=es]")
    assert page.input_value("#school") == "Stony Brook University"
    assert page.get_attribute(".yesno button:nth-child(2)", "aria-pressed") == "true"
    assert page.input_value("#why") == essay
    assert page.is_checked("#consent")
    assert page.eval_on_selector("#resume", "e => e.files[0].name") == "resume.pdf"


def test_clearcompany_style_required_sms_radio_round_trip(page):
    label = ("May we use this number to send you text messages from Americo Financial Life and "
             "Annuity that pertain to your job application?")
    page.set_content(f'''<form><div class="application-question">
      <div class="question-title">{label} *</div>
      <div class="options">
        <label><input type="radio" name="sms-consent" value="yes" required> Yes</label>
        <label><input type="radio" name="sms-consent" value="no"> No</label>
      </div>
    </div></form>''')
    fields = form.extract_fields(page, probe_options=False)
    assert len(fields) == 1
    field = fields[0]
    assert field.label == label and field.kind == "radio" and field.required
    assert field.options == ["Yes", "No"]
    answer = answers.answer_fields([field], JOB, {"personal": {"phone": "555-111-2222"}})[0]
    assert answer.value == "No" and answer.source == "profile"
    assert form.fill_fields(page, [field], [answer]) == []
    assert page.is_checked('input[name="sms-consent"][value="no"]')


def test_fill_reports_failures(page):
    _load(page, "form_basic.html")
    f = _by_label(form.extract_fields(page))
    bad = [Answer(f["Degree"].key, "Doctorate in Underwater Basketweaving", "generated"),
           Answer(f["Resume/CV"].key, "/no/such/file.pdf", "profile"),
           Answer("aa-999", "ghost", "profile")]
    fields = [f["Degree"], f["Resume/CV"], Field("aa-999", "Ghost", "text", False)]
    assert sorted(form.fill_fields(page, fields, bad)) == sorted([f["Degree"].key, f["Resume/CV"].key, "aa-999"])


def test_fill_skips_unknown(page):
    _load(page, "form_basic.html")
    f = _by_label(form.extract_fields(page))
    assert form.fill_fields(page, [f["First Name"]], [Answer(f["First Name"].key, None, "unknown")]) == []
    assert page.input_value("#first_name") == ""


def test_remounted_greenhouse_select_keeps_key_and_fills(page):
    page.set_content('''<form>
      <label for="first">First Name</label>
      <input id="first" oninput="document.getElementById('visa').outerHTML =
        '<select id=&quot;visa&quot; required><option value=&quot;&quot;>Select...</option><option>No</option></select>'">
      <label for="visa">Will you at any point require sponsorship?</label>
      <select id="visa" required><option value="">Select...</option><option>No</option></select>
    </form>''')
    fields = _by_label(form.extract_fields(page, probe_options=False))
    old_key = fields["Will you at any point require sponsorship?"].key
    vals = {"First Name": "Ryan", "Will you at any point require sponsorship?": "No"}
    assert form.fill_fields(page, list(fields.values()),
                            [Answer(fields[label].key, value, "profile") for label, value in vals.items()]) == []
    assert page.locator("#visa").input_value() == "No"
    assert _by_label(form.extract_fields(page, probe_options=False))[
        "Will you at any point require sponsorship?"].key == old_key


def test_select_remount_after_change_can_be_verified(page):
    page.set_content('''<form><label for="visa">Sponsorship</label>
      <select id="visa" onchange="this.outerHTML =
        '<select id=&quot;visa&quot;><option>No</option></select>'">
        <option value="">Select...</option><option>No</option>
      </select></form>''')
    field = _by_label(form.extract_fields(page, probe_options=False))["Sponsorship"]
    assert form.fill_fields(page, [field], [Answer(field.key, "No", "profile")]) == []
    assert page.locator("#visa").input_value() == "No"


def test_fill_fields_disabled_readonly_and_prefilled(page):
    page.set_content("""
        <form>
            <div><label for="f_pre">First Name</label><input id="f_pre" type="text" value="Ryan"></div>
            <div><label for="f_dis">Email</label><input id="f_dis" type="email" disabled></div>
            <div><label for="f_ro">Username</label><input id="f_ro" type="text" readonly value=""></div>
        </form>
    """)
    page.eval_on_selector("#f_pre", "e => e.setAttribute('data-aa-key', 'k_pre')")
    page.eval_on_selector("#f_dis", "e => e.setAttribute('data-aa-key', 'k_dis')")
    page.eval_on_selector("#f_ro", "e => e.setAttribute('data-aa-key', 'k_ro')")
    fields = [
        Field("k_pre", "First Name", "text", False, current_value="Ryan"),
        Field("k_dis", "Email", "email", False),
        Field("k_ro", "Username", "text", False),
    ]
    answers = [
        Answer("k_pre", "Ryan", "profile"),
        Answer("k_dis", "ryan@example.com", "profile"),
        Answer("k_ro", "ryan", "profile"),
    ]
    t0 = time.time()
    failed = form.fill_fields(page, fields, answers)
    elapsed = time.time() - t0
    assert failed == []
    assert elapsed < 2.0
    assert page.input_value("#f_pre") == "Ryan"


def test_best_option_fuzzy():
    assert form.best_option("Yes", ["No, I am not", "Yes, I am authorized"]) == "Yes, I am authorized"
    assert form.best_option("No", ["None of the above", "No, I will not require sponsorship"]) == \
        "No, I will not require sponsorship"
    assert form.best_option("United States", ["Canada +1", "United States +1"]) == "United States +1"
    assert form.best_option("Bachelor of Science", ["High School", "Bachelor's Degree", "Master's"]) in (None, "Bachelor's Degree")
    assert form.best_option("Klingon", ["English", "Spanish"]) is None


# ---------- answers ----------

def test_commuting_residence_uses_home_and_job_city_state():
    question = "Do you reside in the location for this job posting or within commutable distance?"
    field = Field("commute", question, "radio", True, ["Yes", "No"])
    job = Job(id="vanshb03:74980d2d490cb41d", company="Ginkgo Bioworks",
              title="Software Engineering Intern", url="https://example.test/job",
              locations=["Emeryville, CA"], category="Software", date_posted=0)

    def answer(locations, profile=PROFILE, field=field):
        job.locations = locations
        return answers.answer_fields([field], job, profile)[0]

    result = answer(["Emeryville, CA"])
    assert result.value == "No" and result.source == "profile"

    same_city = {**PROFILE, "personal": {**PROFILE["personal"], "city": "Emeryville",
                                       "state": "California", "location": "Emeryville, CA"}}
    assert answer(["Emeryville, California"], same_city).value == "Yes"

    for locations in (["San Francisco, NY"], ["Emeryville, CA", "Remote"], [], ["New York, NY"]):
        assert answer(locations).source == "unknown"
    assert answer(["Emeryville, CA"], {"personal": {}}).source == "unknown"
    assert answer(["Emeryville, CA"], field=Field("commute", question, "select", True,
                                                ["Undecided"])).source == "unknown"


def _ans(fields, **kw):
    return {a.key: a for a in answers.answer_fields(fields, JOB, PROFILE, **kw)}


def test_deterministic_answers_from_profile():
    fields = [
        Field("k1", "First Name", "text", True), Field("k2", "Last Name", "text", True),
        Field("k3", "Email", "email", True), Field("k4", "Phone", "tel", True),
        Field("k5", "Are you legally authorized to work in the US?", "radio", True,
              ["Yes, I am authorized", "No, I am not authorized"]),
        Field("k6", "Will you now or in the future require sponsorship?", "select", True,
              ["Yes, I will require sponsorship", "No, I will not require sponsorship"]),
        Field("k7", "Expected graduation year", "select", True, ["2026", "2027", "2028", "2029"]),
        Field("k8", "Gender", "combobox", False, ["Male", "Female", "Decline To Self Identify"]),
        Field("k9", "Veteran Status", "select", False, ["I am a veteran", "I am not a veteran", "I don't wish to answer"]),
        Field("k10", "How did you hear about us?", "select", True, ["LinkedIn", "Referral", "Other"]),
        Field("k11", "Resume/CV", "file", True),
        Field("k12", "Preferred Name | What would you like us to call you?", "text", False),
        Field("k13", "University", "text", True), Field("k14", "GPA", "number", False),
        Field("k15", "LinkedIn Profile", "url", False),
        Field("k16", "What is your graduation date?", "text", True, placeholder="Pick date..."),
        Field("k17", "Graduation date", "date", False),
        Field("k18", "Expected graduation", "combobox", True, ["Spring 2027", "Fall 2027", "Spring 2028"]),
        Field("k19", "I agree to the privacy policy", "checkbox", True),
        Field("k20", "Are you at least 18 years of age?", "radio", True, ["Yes", "No"]),
    ]
    a = _ans(fields)
    expect = {
        "k1": "Ryan", "k2": "Chen", "k3": "ryan@example.com", "k4": "718-219-8854",
        "k5": "Yes, I am authorized", "k6": "No, I will not require sponsorship", "k7": "2028",
        "k8": "Decline To Self Identify", "k9": "I don't wish to answer",
        "k11": "/nonexistent/resume.pdf", "k12": "Ryan", "k13": "Stony Brook University", "k14": "3.74",
        "k15": "https://www.linkedin.com/in/ryan", "k16": "05/01/2028", "k17": "2028-05-01",
        "k18": "Spring 2028", "k19": True, "k20": "Yes",
    }
    for k, v in expect.items():
        assert a[k].value == v, (k, a[k])
        assert a[k].source == "profile", (k, a[k])
    assert a["k10"].source == "profile" and a["k10"].value == "Other"


def test_phone_device_type_and_heard_profile():
    fields = [
        Field("p1", "Phone device type", "select", True, ["Mobile", "Landline"]),
        Field("p2", "Device type", "select", True, ["Cell", "Work"]),
        Field("p3", "Phone type", "combobox", True, ["Cellular", "Home"]),
        Field("p4", "Phone number", "tel", True),
        Field("h1", "How did you hear about us?", "select", True, ["Billboard", "Newspaper"]),
        Field("h2", "How did you hear about us?", "select", True, ["Simplify", "Other"]),
        Field("h3", "How did you hear about us?", "select", True, ["Career Page", "LinkedIn", "Indeed", "Job Board"]),
    ]
    ans = _ans(fields)
    assert ans["p1"].value == "Mobile" and ans["p1"].source == "profile"
    assert ans["p2"].value == "Cell" and ans["p2"].source == "profile"
    assert ans["p3"].value == "Cellular" and ans["p3"].source == "profile"
    assert ans["p4"].value == "718-219-8854" and ans["p4"].source == "profile"
    assert ans["h1"].value is None and ans["h1"].source == "unknown"
    assert ans["h2"].value == "Simplify" and ans["h2"].source == "profile"
    assert ans["h3"].value == "Job Board" and ans["h3"].source == "profile"


def test_relatives_and_government_rules_avoid_false_positives():
    prof = {**PROFILE, "preferences": {**PROFILE["preferences"], "government_employment": "No",
                                       "relatives_at_company": "No"}}
    yes = ["Prior Government Employment?", "Do you have any relatives currently employed by Acme?",
           "Are any of your family members working at Acme?"]
    no = ["Describe a time you learned a relatively new technology", "Emergency contact name (family member)",
          "How does your experience compare relative to this role?"]
    fields = [Field(f"k{i}", t, "text", True) for i, t in enumerate(yes + no)]
    got = answers.answer_fields(fields, JOB, prof)
    assert [a.value for a in got[:len(yes)]] == ["No"] * len(yes)
    assert all(a.source != "profile" for a in got[len(yes):])


def test_dynamic_combobox_partial_options_fallback():
    # Greenhouse and other ATS pre-populate the first ~100 options in DOM (e.g. starting with 'A').
    # A combobox must fall back to candidate text if not found in pre-rendered options,
    # whereas a select/radio must return None.
    f_combo = Field("s_combo", "School", "combobox", True, ["Amherst College", "Auburn University"])
    f_select = Field("s_select", "School", "select", True, ["Amherst College", "Auburn University"])
    ans = _ans([f_combo, f_select])
    assert ans["s_combo"].value == "Stony Brook University"
    assert ans["s_combo"].source == "profile"
    assert ans["s_select"].value is None
    assert ans["s_select"].source == "unknown"


def test_disability_major_life_activities_not_matched_as_education_major():
    label = "Do you have a physical or mental impairment that substantially limits one or more of your major life activities?"
    f = Field("dis", label, "select", True, ["Yes, I have a disability", "No", "I don't wish to answer"])
    ans = _ans([f])
    assert ans["dis"].value == "I don't wish to answer"
    assert ans["dis"].source == "profile"


def test_optional_eeo_free_text_left_blank():
    a = _ans([Field("p", "Pronouns", "text", False)])
    assert a["p"].value is None and a["p"].source == "unknown"


def test_url_in_label_does_not_match_rules():
    a = _ans([Field("q", "Take this quiz: https://acme.github.io/quiz/", "text", True)])
    assert a["q"].source == "unknown" and a["q"].value is None


def test_llm_unavailable_degrades_to_unknown_and_bank():
    fields = [Field("e", "Who referred you to this role?", "text", True),
              Field("w", "Why do you want to join Acme?", "textarea", True),
              Field("o", "Anything else?", "textarea", False),
              Field("n", "First Name", "text", True)]
    a = _ans(fields)
    assert a["e"].source == "unknown" and a["e"].value is None
    assert a["w"].source == "bank" and a["w"].value == PROFILE["answer_bank"]["why_company"]
    assert a["o"].source == "unknown"
    assert a["n"].value == "Ryan"


def test_llm_answers_validated(monkeypatch):
    seen = {}

    def fake(prompt, schema, **kw):
        seen["prompt"], seen["system"] = prompt, kw.get("system", "")
        payload = json.loads(prompt.split("\n", 1)[1])
        evidence_ids = {field["key"]: [item["id"] for item in field.get("evidence", [])]
                        for field in payload["fields"]}
        rows = [
            {"key": "why", "value": "Acme's payments work matches my C++ systems background.", "source": "generated"},
            {"key": "pick", "value": "Hybrid", "source": "generated"},
            {"key": "bad", "value": "Maybe", "source": "generated"},
            {"key": "deadline", "value": "I do not have any offer deadlines.", "source": "generated"},
            {"key": "langs", "value": "English || Spanish", "source": "generated"},
            {"key": "unk", "value": "", "source": "unknown", "note": "not in profile"},
        ]
        for row in rows:
            row["evidence_ids"] = evidence_ids.get(row["key"], [])
        return {"answers": rows}
    monkeypatch.setattr(answers.llm, "generate_json", fake)
    fields = [Field("why", "Why Acme?", "textarea", True),
              Field("pick", "Preferred work arrangement", "select", True, ["Remote", "Hybrid", "Onsite"]),
              Field("bad", "Do you like cats?", "radio", True, ["Yes", "No"]),
              Field("deadline", "Do you have an active offer deadline?", "textarea", True),
              Field("langs", "Which tools do you use?", "checkbox", False, ["English", "Spanish", "French"]),
              Field("unk", "Additional information", "textarea", True),
              Field("fn", "First Name", "text", True)]
    profile = {
        **PROFILE,
        "personal": {**PROFILE["personal"], "languages": "English"},
        "preferences": {**PROFILE["preferences"], "open_to_hybrid": "Yes"},
    }
    a = {row.key: row for row in answers.answer_fields(fields, JOB, profile, job_text="Acme builds payments.")}
    assert a["why"].source == "generated" and "C++" in a["why"].value
    assert a["pick"].source == "unknown"  # Openness to hybrid work does not establish a preference.
    assert a["bad"].source == "unknown"                     # not a verbatim option
    assert a["deadline"].source == "unknown"                # factual question, not in profile
    assert a["langs"].source == "unknown" and a["langs"].value is None  # Spanish has no supporting evidence
    assert a["unk"].source == "unknown"
    assert a["fn"].source == "profile"
    payload = json.loads(seen["prompt"].split("\n", 1)[1])
    prompt_fields = {row["key"]: row for row in payload["fields"]}
    assert "fn" not in prompt_fields                                  # deterministic fields are not sent
    assert "pick" not in prompt_fields
    assert any(item["source"] == "resume" for item in prompt_fields["unk"]["evidence"])
    assert "RESUME: Stony Brook" in seen["prompt"] and "Acme builds payments." in seen["prompt"]
    assert "never invent facts" in seen["system"].lower()


# ---------- captcha ----------

def test_captcha_detect_and_unsupported_without_key(page, monkeypatch):
    _load(page, "captcha_v2.html")
    frame, w, kind = captcha.detect(page)
    assert kind == "recaptcha_v2" and w["sitekey"].startswith("6LcTEST")
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    out = captcha.solve_if_present(page, {"captcha": {}})
    assert out.status == "unsupported" and captcha.blocks_submit(out)


def test_captcha_none(page):
    _load(page, "form_basic.html")
    assert captcha.solve_if_present(page, {}).status == "none"


def test_captcha_budget(page, monkeypatch):
    _load(page, "captcha_v2.html")
    monkeypatch.setenv("CAPSOLVER_API_KEY", "test-key")
    monkeypatch.setattr(captcha.db, "captcha_spend_since", lambda ts: 2.9999)
    out = captcha.solve_if_present(page, {"captcha": {"max_spend_per_night_usd": 3.0}})
    assert out.status == "budget"


def test_captcha_solve_injects_token(page, monkeypatch):
    _load(page, "captcha_v2.html")
    spend = []
    monkeypatch.setenv("CAPSOLVER_API_KEY", "test-key")
    monkeypatch.setattr(captcha.db, "captcha_spend_since", lambda ts: 0.0)
    monkeypatch.setattr(captcha.db, "add_captcha_spend", lambda *a, **k: spend.append(a))
    monkeypatch.setattr(captcha, "_solve", lambda key, task, timeout: ("TOKEN123", "ok"))
    out = captcha.solve_if_present(page, {"captcha": {}}, job_id="test:1")
    assert out.status == "solved" and out.cost > 0
    assert page.input_value("#g-recaptcha-response") == "TOKEN123"
    assert page.evaluate("window.__captchaToken") == "TOKEN123"
    assert spend and spend[0][0] == "recaptcha_v2" and spend[0][2] is True


def test_hcaptcha_invisible_is_passive():
    out = captcha.CaptchaOutcome("unsupported", "hcaptcha_invisible")
    assert not captcha.blocks_submit(out)
    assert captcha.blocks_submit(captcha.CaptchaOutcome("unsupported", "hcaptcha"))


def test_reused_field_key_refreshes_answer_when_label_changes(page):
    page.set_content('''<form>
      <label for="person">First Name</label><input id="person" type="text"
        oninput="if (document.querySelector('label').textContent === 'First Name' && this.value === 'Ryan')
          document.querySelector('label').textContent = 'Last Name'">
    </form>''')
    fields, responses, failed = form.fill_form(page, JOB, PROFILE, passes=2)
    assert not failed
    assert len(fields) == 1 and fields[0].label == "Last Name"
    assert len(responses) == 1 and responses[0].value == PROFILE["personal"]["last_name"]
    assert page.locator("#person").input_value() == PROFILE["personal"]["last_name"]


def test_visible_hcaptcha_is_unsupported_without_provider_request(page, monkeypatch):
    page.set_content('''<form><div class="h-captcha" data-sitekey="hTEST" data-callback="onSolved"></div>
      <textarea name="h-captcha-response"></textarea><textarea name="g-recaptcha-response"></textarea></form>
      <script>window.captchaResult = ''; window.onSolved = t => window.captchaResult = t;</script>''')
    hit = captcha.detect(page)
    assert hit is not None and hit[2] == "hcaptcha" and hit[1]["sitekey"] == "hTEST"
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    monkeypatch.setattr(captcha.db, "captcha_spend_since", lambda ts: pytest.fail("unexpected spend check"))
    monkeypatch.setattr(captcha.db, "add_captcha_spend", lambda *a: pytest.fail("unexpected charge"))
    monkeypatch.setattr(captcha, "_solve", lambda *a: pytest.fail("unsupported solver called"))
    result = captcha.solve_if_present(page, {"captcha": {"max_spend_per_night_usd": 0.1}})
    assert result.status == "unsupported" and result.kind == "hcaptcha"
    assert "does not support hCaptcha" in result.detail
    assert captcha.blocks_submit(result)
    assert page.locator('[name="h-captcha-response"]').input_value() == ""
    assert page.locator('[name="g-recaptcha-response"]').input_value() == ""
    assert page.evaluate('window.captchaResult') == ""


def test_hcaptcha_unsupported_provider_fails_without_charging(page, monkeypatch):
    page.set_content('<form><div class="h-captcha" data-sitekey="hTEST"></div></form>')
    monkeypatch.setenv("CAPSOLVER_API_KEY", "test-key")
    monkeypatch.setattr(captcha.db, "captcha_spend_since", lambda ts: pytest.fail("unexpected spend check"))
    monkeypatch.setattr(captcha.db, "add_captcha_spend", lambda *a: pytest.fail("unexpected charge"))
    monkeypatch.setattr(captcha.requests, "post", lambda *a, **k: pytest.fail("unsupported provider request"))
    out = captcha.solve_if_present(page, {"captcha": {}})
    assert out.status == "unsupported" and captcha.blocks_submit(out)
    assert "manual verification required" in out.detail


def test_hcaptcha_visible_challenge_overrides_invisible_widget_without_a_service_call(page, monkeypatch):
    page.route("https://*.hcaptcha.com/**", lambda route: route.fulfill(body="<html></html>"))
    page.set_content('''<div class="h-captcha" data-sitekey="hTEST" data-size="invisible" hidden></div>
      <iframe width="1" height="1" src="https://newassets.hcaptcha.com/captcha#frame=checkbox-invisible&amp;sitekey=hTEST"></iframe>
      <iframe id="challenge" width="300" height="300" src="https://newassets.hcaptcha.com/captcha#frame=challenge&amp;sitekey=hTEST"></iframe>''')
    monkeypatch.setenv("CAPSOLVER_API_KEY", "test-key")
    monkeypatch.setattr(captcha.requests, "post", lambda *a, **k: pytest.fail("hCaptcha provider request"))
    monkeypatch.setattr(captcha.db, "captcha_spend_since", lambda *a: pytest.fail("hCaptcha budget reservation"))
    visible = captcha.solve_if_present(page, {})
    assert visible.status == "unsupported" and visible.kind == "hcaptcha" and visible.cost == 0
    assert captcha.blocks_submit(visible)
    page.locator("#challenge").evaluate("e => e.hidden = true")
    passive = captcha.solve_if_present(page, {})
    assert passive.status == "unsupported" and passive.kind == "hcaptcha_invisible" and passive.cost == 0
    assert not captcha.blocks_submit(passive)


# ---------- submit gating ----------

def _ctx(page, **kw) -> ApplyContext:
    ctx = ApplyContext(page=page, browser=None, config={"captcha": {}}, profile=PROFILE, **kw)
    ctx.screenshot = lambda job, tag: None
    return ctx


@pytest.mark.parametrize(("mode", "dry_run", "status", "reason"), [
    ("full", True, ApplyStatus.DRY_RUN_OK, "dry run"),
    ("fill_only", False, ApplyStatus.NEEDS_REVIEW, "fill_only mode"),
])
def test_dry_run_never_submits(page, tmp_path, monkeypatch, mode, dry_run, status, reason):
    _load(page, "form_basic.html")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    prof = dict(PROFILE, resume_path=str(resume))
    fields, ans, failed = form.fill_form(page, JOB, prof)
    monkeypatch.setattr(captcha, "solve_if_present", lambda *a: pytest.fail("solver called in fill-only/dry-run"))
    res = form.finish_application(JOB, _ctx(page, submit_mode=mode, dry_run=dry_run), page, fields, ans, failed,
                                  submit_texts=["Submit application"])
    assert not page.evaluate("window.__submitted")
    assert res.status == status, res.reason
    assert res.reason == reason and failed == []
    assert res.fields_total == len(fields) == 13
    assert "Languages (check all that apply)" not in res.unanswered_required


def test_unknown_required_blocks_submit(page):
    _load(page, "form_basic.html")
    fields = form.extract_fields(page)
    ans = [Answer(f.key, None, "unknown") for f in fields]
    res = form.finish_application(JOB, _ctx(page, submit_mode="full"), page, fields, ans, [],
                                  submit_texts=["Submit application"])
    assert res.status == ApplyStatus.NEEDS_REVIEW and res.reason.startswith("unanswered required")
    assert not page.evaluate("window.__submitted")


def test_submit_and_confirm_on_local_fixture(page, monkeypatch):
    # This fixture submits only to its local HTML form; skip durable run-intent persistence.
    monkeypatch.setattr(db, "record_submit_intent", lambda *args, **kwargs: None)
    _load(page, "captcha_v2.html")
    page.evaluate("""() => { document.querySelector('.g-recaptcha').remove();
      document.querySelector('form').addEventListener('submit', e => { e.preventDefault();
        document.body.insertAdjacentHTML('beforeend', '<h2>Thank you for applying!</h2>'); }); }""")
    fields = form.extract_fields(page)
    ans = [Answer(fields[0].key, "ryan@example.com", "profile")]
    form.fill_fields(page, fields, ans)
    res = form.finish_application(JOB, _ctx(page, submit_mode="full", run_id=1), page, fields, ans, [], submit_texts=["Submit"])
    assert res.status == ApplyStatus.SUBMITTED


def test_education_section_workday_variants():
    months = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]
    years = ["2022", "2023", "2024", "2025", "2026", "2027", "2028"]
    fields = [
        Field("s1", "School or University", "text", True),
        Field("s2", "Education - School or University", "text", True),
        Field("s3", "Education 1 - School or University", "text", True),
        Field("s4", "School", "text", True),
        Field("s5", "University", "text", True),
        Field("d1", "Education - From", "text", True),
        Field("d2", "Education 1 - From", "text", True),
        Field("d3", "Education - From", "select", True, options=years),
        Field("d4", "Education - From", "select", True, options=months),
        Field("d5", "Education: Start Date", "text", True),
        Field("t1", "Education - To (Actual or Expected)", "text", True, placeholder="MM/YYYY"),
        Field("t2", "Education 1 - To (Actual or Expected)", "text", True, placeholder="MM/YYYY"),
        Field("t3", "Education - To (Actual or Expected)", "select", True, options=years),
        Field("t4", "Education - To (Actual or Expected)", "select", True, options=months),
        Field("t5", "Education: End Date", "text", True, placeholder="MM/YYYY"),
        Field("t6", "Expected Graduation", "select", True, options=years),
    ]
    ans = _ans(fields)
    assert ans["s1"].value == "Stony Brook University"
    assert ans["s2"].value == "Stony Brook University"
    assert ans["s3"].value == "Stony Brook University"
    assert ans["s4"].value == "Stony Brook University"
    assert ans["s5"].value == "Stony Brook University"

    assert ans["d1"].value == "08/2024"
    assert ans["d2"].value == "08/2024"
    assert ans["d3"].value == "2024"
    assert ans["d4"].value == "August"
    assert ans["d5"].value == "08/2024"

    assert ans["t1"].value == "05/2028"
    assert ans["t2"].value == "05/2028"
    assert ans["t3"].value == "2028"
    assert ans["t4"].value == "May"
    assert ans["t5"].value == "05/2028"
    assert ans["t6"].value == "2028"


def test_education_distinct_month_year_and_rules_continue():
    numeric_months = [f"{i:02d}" for i in range(1, 13)]
    named_months = ["January", "February", "March", "April", "May", "June", "July", "August",
                    "September", "October", "November", "December"]
    years = ["2022", "2023", "2024", "2025", "2026", "2027", "2028"]

    fields = [
        # Distinct start month/year
        Field("sy", "Education: Start date year", "select", True, options=years),
        Field("sm_num", "Education: Start date month", "select", True, options=numeric_months),
        Field("sm_name", "Education: Start date month", "select", True, options=named_months),
        # Distinct end month/year
        Field("ey", "Education: End date year", "select", True, options=years),
        Field("em_num", "Education: End date month", "select", True, options=numeric_months),
        Field("em_name", "Education: End date month", "select", True, options=named_months),
        # RULES continuation test: sponsorship rule returns None due to 'citizen' and 'and',
        # but continuing allows citizen rule to resolve it to 'Yes'
        Field("cit", "Are you a U.S. citizen and do you require sponsorship?", "radio", True, ["Yes", "No"]),
    ]
    ans = _ans(fields)
    assert ans["sy"].value == "2024"
    assert ans["sm_num"].value == "08"
    assert ans["sm_name"].value == "August"

    assert ans["ey"].value == "2028"
    assert ans["em_num"].value == "05"
    assert ans["em_name"].value == "May"

    assert ans["cit"].value == "Yes"


def test_greenhouse_employment_dates_keep_their_section(page, monkeypatch):
    page.set_content("""
      <form>
        <div class="employment--container"><div class="employment-form">
          <label for="company">Company name</label><input id="company">
          <label for="startmonth">Start date month</label><input id="startmonth">
          <label for="startyear">Start date year</label><input id="startyear">
          <label for="endmonth">End date month</label><input id="endmonth">
          <label for="endyear">End date year</label><input id="endyear" required>
          <label for="current">Current role</label><input id="current" type="checkbox"
            onchange="document.querySelector('#endmonth').disabled = this.checked;
                      document.querySelector('#endyear').disabled = this.checked">
        </div></div>
        <div class="education-entry"><label for="grad">End date year</label><input id="grad"></div>
        <label for="available">Available to start</label><input id="available">
      </form>
    """)
    monkeypatch.setattr(answers, "resume_text", lambda path=None: (
        "Machine Learning ResearcherAugust 2026 – Present\nCRIZM Stony Brook University\n"))
    profile = {**PROFILE, "personal": {**PROFILE["personal"],
                                  "current_company": "CRIZM (Stony Brook University)",
                                  "current_title": "Machine Learning Researcher"}}
    fields = form.extract_fields(page, probe_options=False)
    got = {f.label: a for f, a in zip(fields, answers.answer_fields(fields, JOB, profile))}
    assert got["Employment: Company name"].value == "CRIZM (Stony Brook University)"
    assert got["Employment: Start date month"].value == "August"
    assert got["Employment: Start date year"].value == "2026"
    assert got["Employment: End date month"].source == "unknown"
    assert got["Employment: End date year"].value is None
    assert got["Employment: Current role"].value is True
    assert got["Education: End date year"].value == "2028"
    assert got["Available to start"].value == "May 2027"
    assert form.fill_fields(page, fields, list(got.values())) == []
    assert page.input_value("#startmonth") == "August"
    assert page.input_value("#startyear") == "2026"
    assert page.is_checked("#current")
    assert page.is_disabled("#endmonth") and page.is_disabled("#endyear")
    assert page.input_value("#endmonth") == page.input_value("#endyear") == ""


def test_unscoped_dates_are_not_internship_availability_and_past_role_dates_are_supported():
    fields = [Field("s", "Employment: Start date year", "text", True),
              Field("e", "Employment: End date year", "text", True),
              Field("role", "Employment: Current role", "checkbox", False),
              Field("bare", "End date year", "text", True)]
    profile = {**PROFILE, "experience": {"current": {
        "start_date": "February 2024", "end_date": "July 2024"}}}
    got = {a.key: a for a in answers.answer_fields(fields, JOB, profile)}
    assert (got["s"].value, got["e"].value) == ("2024", "2024")
    assert got["role"].value is False
    assert got["bare"].value is None and got["bare"].source == "unknown"


def test_avail_consent_and_company_rules():
    prof = dict(PROFILE)
    prof["personal"] = dict(PROFILE["personal"])
    prof["personal"]["current_company"] = "Acme Corp"
    prof["personal"]["current_title"] = "Software Engineer"

    fields = [
        # Avail month/year extraction
        Field("as_y", "Earliest start date year", "text", True),
        Field("as_m", "Earliest start date month", "text", True),
        # Affirm and Consent patterns
        Field("arb", "Arbitration Agreement", "radio", True, ["Confirm", "Decline"]),
        Field("conf", "I confirm the arbitration policy", "checkbox", True),
        # Company and title patterns
        Field("comp", "Company", "text", True),
        Field("emp", "Employer", "text", True),
        Field("cname", "Company Name", "text", True),
        Field("emp_comp", "Employment: Company", "text", True),
        Field("emp_emp", "Employment: Employer", "text", True),
        Field("title", "Title", "text", True),
        Field("jtitle", "Job Title", "text", True),
        Field("emp_title", "Employment: Job Title", "text", True),
        Field("ctitle", "Current Job Title", "text", True),
        Field("mtitle", "Most Recent Title", "text", True),
        # Full-time and referral rules
        Field("ft", "Are you available to begin full-time employment?", "radio", True, ["Yes", "No"]),
        Field("ref_pos", "Were you referred to this position?", "radio", True, ["Yes", "No"]),
        Field("ref_lead", "Were you referred by a senior leader or decision maker?", "radio", True, ["Yes", "No"]),
    ]
    ans = {a.key: a for a in answers.answer_fields(fields, JOB, prof)}
    assert ans["as_y"].value == "2027"
    assert ans["as_m"].value == "May"
    assert ans["arb"].value == "Confirm"
    assert ans["conf"].value is True
    assert ans["comp"].value == "Acme Corp"
    assert ans["emp"].value == "Acme Corp"
    assert ans["cname"].value == "Acme Corp"
    assert ans["emp_comp"].value == "Acme Corp"
    assert ans["emp_emp"].value == "Acme Corp"
    assert ans["title"].value == "Software Engineer"
    assert ans["jtitle"].value == "Software Engineer"
    assert ans["emp_title"].value == "Software Engineer"
    assert ans["ctitle"].value == "Software Engineer"
    assert ans["mtitle"].value == "Software Engineer"
    assert all(ans[key].value is None and ans[key].source == "unknown" for key in ("ft", "ref_pos", "ref_lead"))


def test_submit_email_security_code_verified(page, monkeypatch):
    from agent import mail
    monkeypatch.setattr(db, "record_submit_intent", lambda *args, **kwargs: None)
    page.set_content("""
      <form id="app">
        <label>Email <input type="email" name="email" value="ryan@example.com" /></label>
        <button type="submit" id="sub">Submit</button>
      </form>
      <script>
        document.getElementById('app').addEventListener('submit', e => {
          e.preventDefault();
          document.body.innerHTML = `
            <div>
              <p>Please enter the verification code sent to your email</p>
              <input type="text" name="security_code" id="sec_code" />
              <button type="button" id="verify_btn">Verify</button>
            </div>
          `;
          document.getElementById('verify_btn').addEventListener('click', () => {
            const val = document.getElementById('sec_code').value;
            if (val === '654321') {
              document.body.innerHTML = '<h2>Thank you for applying! Your application has been submitted.</h2>';
            }
          });
        });
      </script>
    """)
    monkeypatch.setattr(mail, "wait_for_verification", lambda after_ts, timeout=30, want="any": mail.Verification(
        code="654321", link=None, message_id="msg1", subject="Code", sender="hr@example.com"
    ))
    fields = form.extract_fields(page)
    ans = [Answer(fields[0].key, "ryan@example.com", "profile")]
    form.fill_fields(page, fields, ans)
    res = form.finish_application(JOB, _ctx(page, submit_mode="full", run_id=1), page, fields, ans, [], submit_texts=["Submit"])
    assert res.status == ApplyStatus.SUBMITTED
    assert res.reason == "submitted"


def test_submit_email_security_code_timeout_needs_review(page, monkeypatch):
    from agent import mail
    monkeypatch.setattr(db, "record_submit_intent", lambda *args, **kwargs: None)
    page.set_content("""
      <form id="app">
        <label>Email <input type="email" name="email" value="ryan@example.com" /></label>
        <button type="submit" id="sub">Submit</button>
      </form>
      <script>
        document.getElementById('app').addEventListener('submit', e => {
          e.preventDefault();
          document.body.innerHTML = `
            <div>
              <p>Please enter the verification code sent to your email</p>
              <input type="text" name="security_code" id="sec_code" />
              <button type="button" id="verify_btn">Verify</button>
            </div>
          `;
        });
      </script>
    """)
    monkeypatch.setattr(mail, "wait_for_verification", lambda after_ts, timeout=30, want="any": None)
    fields = form.extract_fields(page)
    ans = [Answer(fields[0].key, "ryan@example.com", "profile")]
    form.fill_fields(page, fields, ans)
    res = form.finish_application(JOB, _ctx(page, submit_mode="full", run_id=1), page, fields, ans, [], submit_texts=["Submit"])
    assert res.status == ApplyStatus.NEEDS_REVIEW
    assert "email security code requested after submit" in res.reason


def test_submit_email_security_code_multi_box_otp(page, monkeypatch):
    from agent import mail
    monkeypatch.setattr(db, "record_submit_intent", lambda *args, **kwargs: None)
    page.set_content("""
      <form id="app">
        <label>Email <input type="email" name="email" value="ryan@example.com" /></label>
        <button type="submit" id="sub">Submit</button>
      </form>
      <script>
        document.getElementById('app').addEventListener('submit', e => {
          e.preventDefault();
          document.body.innerHTML = `
            <div id="otp-section">
              <h3>Security code</h3>
              <p>Please enter the 8-digit verification code sent to your email</p>
              <div class="otp-inputs">
                <input type="text" maxlength="1" class="otp-digit" />
                <input type="text" maxlength="1" class="otp-digit" />
                <input type="text" maxlength="1" class="otp-digit" />
                <input type="text" maxlength="1" class="otp-digit" />
                <input type="text" maxlength="1" class="otp-digit" />
                <input type="text" maxlength="1" class="otp-digit" />
                <input type="text" maxlength="1" class="otp-digit" />
                <input type="text" maxlength="1" class="otp-digit" />
              </div>
              <button type="button" id="submit_app">Submit application</button>
            </div>
          `;
          document.getElementById('submit_app').addEventListener('click', () => {
            const val = Array.from(document.querySelectorAll('.otp-digit')).map(i => i.value).join('');
            if (val === '87654321') {
              document.body.innerHTML = '<h2>Thank you for applying! Your application has been submitted.</h2>';
            }
          });
        });
      </script>
    """)
    monkeypatch.setattr(mail, "wait_for_verification", lambda after_ts, timeout=30, want="any": mail.Verification(
        code="87654321", link=None, message_id="msg1", subject="Code", sender="hr@example.com"
    ))
    fields = form.extract_fields(page)
    ans = [Answer(fields[0].key, "ryan@example.com", "profile")]
    form.fill_fields(page, fields, ans)
    res = form.finish_application(JOB, _ctx(page, submit_mode="full", run_id=1), page, fields, ans, [], submit_texts=["Submit"])
    assert res.status == ApplyStatus.SUBMITTED
    assert res.reason == "submitted"




