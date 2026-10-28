from __future__ import annotations

import json

import pytest

from agent import answers
from agent.models import Field, Job


JOB = Job(id="test:rag", company="Example", title="Intern", url="https://example.test/job",
          locations=[], category="Software", date_posted=0)


@pytest.fixture
def isolated_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(answers, "resume_text", lambda path=None: "")
    monkeypatch.setattr("agent.config.ROOT", tmp_path)


def _answer_map(fields, profile, **kwargs):
    return {answer.key: answer for answer in answers.answer_fields(fields, JOB, profile, **kwargs)}


def test_empty_profile_leaves_personal_facts_unknown(isolated_evidence, monkeypatch):
    def unexpected_llm(*args, **kwargs):
        raise AssertionError("an empty evidence set must not trigger generation")

    monkeypatch.setattr(answers.llm, "generate_json", unexpected_llm)
    fields = [
        Field("country", "Country", "select", True, ["United States", "Canada"]),
        Field("phone", "Phone country code", "select", True, ["+1", "+44"]),
        Field("degree", "Degree", "select", True, ["Bachelor's Degree", "Master's Degree"]),
        Field("student", "Are you currently a student?", "radio", True, ["Yes", "No"]),
        Field("years", "Years of professional experience", "select", True, ["None", "1-3 years"]),
        Field("heard", "How did you hear about us?", "select", True, ["LinkedIn", "Other"]),
        Field("referral", "Referral source", "text", True),
        Field("resume", "Resume/CV", "file", True),
        Field("sponsor", "Do you currently or in the future need sponsorship?", "radio", True,
              ["Yes", "No"]),
    ]

    got = _answer_map(fields, {})

    assert all(got[field.key].source == "unknown" and got[field.key].value is None for field in fields)


def test_shortcuts_require_matching_profile_facts(isolated_evidence, monkeypatch):
    def unavailable(*args, **kwargs):
        raise answers.llm.LLMUnavailable("offline")

    monkeypatch.setattr(answers.llm, "generate_json", unavailable)
    profile = {
        "personal": {"country": "United States"},
        "education": {"degree_full": "Bachelor of Science", "degree": "Master of Science"},
        "experience": {"years_professional": "3"},
        "preferences": {"how_did_you_hear": "Simplify"},
    }
    fields = [
        Field("phone", "Phone country code", "select", True, ["+44"]),
        Field("degree", "Degree", "select", True, ["Bachelor's Degree"]),
        Field("years", "Years of professional experience", "select", True, ["None"]),
        Field("heard", "How did you hear about us?", "select", True, ["Other"]),
        Field("referral", "Referral source", "text", True),
    ]

    got = _answer_map(fields, profile)

    assert all(got[field.key].source == "unknown" and got[field.key].value is None
               for field in fields if field.key != "heard")
    assert got["heard"].value == "Other" and got["heard"].source == "profile"


def test_conflicting_degree_profile_stays_unknown(isolated_evidence, monkeypatch):
    def unexpected_llm(*args, **kwargs):
        raise AssertionError("conflicting profile values must not be selected by generation")

    monkeypatch.setattr(answers.llm, "generate_json", unexpected_llm)
    field = Field("degree", "Degree", "select", True, ["Bachelor's Degree", "Master's Degree"])
    profile = {"education": {"degree_full": "Bachelor of Science", "degree": "Master of Science"}}

    got = _answer_map([field], profile)["degree"]

    assert got.source == "unknown" and got.value is None
    assert got.note == "conflicting profile values"


def test_supported_country_degree_and_year_synonyms_remain_available(isolated_evidence):
    fields = [
        Field("country", "Country", "select", True, ["USA", "Canada"]),
        Field("degree", "Degree", "select", True, ["Bachelor's Degree", "Master's Degree"]),
        Field("years", "Years of professional experience", "select", True, ["0-2 years", "3-5 years"]),
        Field("heard", "How did you hear about us?", "select", True, ["Simplify", "Other"]),
    ]
    profile = {
        "personal": {"country": "United States"},
        "education": {"degree_full": "Bachelor of Science"},
        "experience": {"years_professional": "3"},
        "preferences": {"how_did_you_hear": "Simplify"},
    }

    got = _answer_map(fields, profile)

    assert [got[field.key].value for field in fields] == ["USA", "Bachelor's Degree", "3-5 years", "Simplify"]
    assert all(got[field.key].source == "profile" for field in fields)


def test_sponsorship_uses_the_requested_time_horizon(isolated_evidence, monkeypatch):
    def unavailable(*args, **kwargs):
        raise answers.llm.LLMUnavailable("offline")

    monkeypatch.setattr(answers.llm, "generate_json", unavailable)
    profile = {"work_authorization": {"needs_sponsorship_now": "No", "needs_sponsorship_future": "Yes"}}
    fields = [
        Field("now", "Will you need sponsorship now?", "radio", True, ["Yes", "No"]),
        Field("future", "Will you need sponsorship in the future?", "radio", True, ["Yes", "No"]),
        Field("either", "Do you need sponsorship now or in the future?", "radio", True, ["Yes", "No"]),
        Field("both", "Do you need sponsorship now and in the future?", "radio", True, ["Yes", "No"]),
        Field("unspecified", "Do you require sponsorship?", "radio", True, ["Yes", "No"]),
    ]

    got = _answer_map(fields, profile)

    assert [got[field.key].value for field in fields] == ["No", "Yes", "Yes", "No", None]
    assert got["unspecified"].source == "unknown"


def test_selected_greenhouse_sponsorship_wording(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    fields = [
        Field("neuralink", "Will you, at any point, require employer sponsorship to work in the United States?",
              "combobox", True, ["Yes", "No"]),
        Field("ginkgo", "Do you now or will you in the future need employer sponsorship for a visa to work in the country where the job is open?",
              "combobox", True, ["Yes, I will need sponsorship support now", "Yes, but not until the future", "No"]),
    ]
    no = {"work_authorization": {"needs_sponsorship_now": "No", "needs_sponsorship_future": "No"},
          "personal": {"current_company": "CRIZM (Stony Brook University)"}}
    assert [a.value for a in _answer_map(fields, no).values()] == ["No", "No"]
    now = {"work_authorization": {"needs_sponsorship_now": "Yes", "needs_sponsorship_future": "No"}}
    got = _answer_map(fields, now)
    assert got["neuralink"].value == "Yes"
    assert got["ginkgo"].value == "Yes, I will need sponsorship support now"
    future = {"work_authorization": {"needs_sponsorship_now": "No", "needs_sponsorship_future": "Yes"}}
    assert _answer_map(fields, future)["ginkgo"].value == "Yes, but not until the future"


def test_employer_name_field_matches_only_employer_questions(isolated_evidence):
    profile = {"personal": {"current_company": "CRIZM (Stony Brook University)"}}
    fields = [Field(f"company-{i}", label, "text", True) for i, label in enumerate([
        "Employer", "Company", "Company Name", "Employment: Company", "Current Employer Name",
        "Most Recent Employer",
    ])]
    got = _answer_map(fields, profile)
    assert all(a.value == "CRIZM (Stony Brook University)" for a in got.values())


def test_resume_grounded_internship_and_greenhouse_season(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    monkeypatch.setattr(answers, "resume_text", lambda path=None:
                        "Software Engineer InternFebruary 2024 – July 2024\nCompany Example")
    fields = [Field("past", "Do you have prior internship or co-op experience?", "combobox", True, ["Yes", "No"]),
              Field("season", "What intern season are you interested in?", "combobox", True,
                    ["Summer 2027", "Fall 2027"])]
    profile = {"resume_path": "data/resume.pdf", "preferences": {"internship_term": "Summer 2027"}}
    got = _answer_map(fields, profile)
    assert got["past"].value == "Yes" and got["past"].source == "profile"
    assert got["season"].value == "Summer 2027" and got["season"].source == "profile"
    got_without_resume = _answer_map([fields[0]], {"experience": {"years_professional": "0"}})
    assert got_without_resume["past"].source == "unknown"


def test_guidepoint_former_employee_from_same_company_preference(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    field = Field("gps", "Are you a current or former GPS employee?", "combobox", True, ["Yes", "No"])
    job = Job(id="test:guidepoint", company="GuidePoint Security", title="Intern",
              url="https://example.test/guidepoint", locations=[], category="Software", date_posted=0)
    profile = {"preferences": {"previously_employed_here": "No"}}
    assert answers.answer_fields([field], job, profile)[0].value == "No"
    assert _answer_map([field], profile)["gps"].source == "unknown"


def test_sms_opt_in_defaults_no_and_respects_explicit_preference(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    label = ("If selected for hire, would you like to receive information regarding your first day via text "
             "message/SMS? You may receive up to three messages via text by opting in. Message and data "
             "rates may apply depending on your mobile plan. You can opt out at any time.")
    field = Field("sms", label, "combobox", True, ["Yes", "No"])
    profile = {"personal": {"phone": "555-111-2222"}}
    got = _answer_map([field], profile)["sms"]
    assert got.source == "profile" and got.value == "No" and got.note == "default SMS opt-out"
    profile["preferences"] = {"sms_opt_in": "Yes"}
    got = _answer_map([field], profile)["sms"]
    assert got.source == "profile" and got.value == "Yes"
    profile["preferences"]["sms_opt_in"] = "No"
    assert _answer_map([field], profile)["sms"].value == "No"


def test_clearcompany_sms_permission_only_with_supported_yes_no_options(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    label = ("May we use this number to send you text messages from Americo Financial Life and "
             "Annuity that pertain to your job application?")
    fields = [
        Field("americo", label, "radio", True, ["Yes", "No"]),
        Field("other", "May we contact you by text message about your application?", "radio", True,
              ["Yes, please", "No, thanks"]),
        Field("unsupported", label, "radio", True, ["Agree", "Disagree"]),
        Field("unprobed", label, "radio", True, []),
        Field("experience", "Do you have experience developing SMS text message services?", "radio", True,
              ["Yes", "No"]),
        Field("privacy", "I consent to the privacy policy", "radio", True, ["I agree", "I decline"]),
    ]
    got = _answer_map(fields, {"personal": {"phone": "555-111-2222"}})
    assert got["americo"].value == "No" and got["americo"].note == "default SMS opt-out"
    assert got["other"].value == "No, thanks"
    assert got["unsupported"].source == "unknown" and got["unsupported"].value is None
    assert got["unprobed"].source == "unknown" and got["unprobed"].value is None
    assert got["experience"].source == "unknown" and got["experience"].value is None
    assert got["privacy"].value == "I agree"
    got_yes = _answer_map(fields[:2], {"preferences": {"sms_opt_in": "Yes"}})
    assert got_yes["americo"].value == "Yes"
    assert got_yes["other"].value == "Yes, please"


def test_mach_discrete_gpa_and_source_are_grounded(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    job = Job(id="test:mach", company="Mach Industries", title="Intern", url="https://example.test/mach",
              locations=[], category="Software", date_posted=0)
    options = ["Not applicable/Do not recall", "4.0 out of 4.0", "3.9 out of 4.0", "3.8 out of 4.0",
               "3.7 out of 4.0", "3.6 out of 4.0"]
    fields = [
        Field("gpa", "GPA (Undergraduate)", "combobox", True, options),
        Field("heard", "How did you hear about this job?", "combobox", True,
              ["Careers site", "Indeed", "LinkedIn", "Other (indicate below)"]),
        Field("details", "Please specify", "text", False, name="question_7715252009"),
        Field("unrelated", "How did you hear about this job?", "combobox", True,
              ["NISC Website", "LinkedIn", "Other"]),
    ]
    profile = {"education": {"gpa": "3.74"}, "preferences": {"how_did_you_hear": "Simplify"}}
    got = {a.key: a for a in answers.answer_fields(fields, job, profile)}
    assert (got["gpa"].value, got["gpa"].source) == ("3.7 out of 4.0", "profile")
    assert (got["heard"].value, got["heard"].source) == ("Other (indicate below)", "profile")
    assert (got["details"].value, got["details"].source) == ("Simplify", "profile")
    assert got["unrelated"].source == "profile" and got["unrelated"].value == "Other"
    assert _answer_map([fields[0]], {"education": {"gpa": "3.75"}})["gpa"].value == "3.8 out of 4.0"


def test_nisc_source_is_grounded_but_membership_and_office_choices_need_confirmation(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers, "resume_text", lambda path=None:
                        "Software Engineer Intern at Necessary Behavior; CRIZM Research at Stony Brook")
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    job = Job(id="vanshb03:19090f192d50fb34", company="National Information Solutions Cooperative",
              title="Platform Development Intern", url="https://example.test/nisc",
              locations=["Lake Saint Louis, MO", "Mandan, ND"], category="Software", date_posted=0)
    fields = [
        Field("member", "Are you currently employed at an NISC Member site?", "combobox", True, ["Yes", "No"]),
        Field("source", "How did you hear about this job?", "combobox", True,
              ["NISC Website", "LinkedIn", "Other"]),
        Field("locations", "What locations are you interested in working from? (Select all that apply)",
              "checkbox", True, ["Atlanta, GA", "Cedar Rapids, IA", "Lake St. Louis, MO", "Mandan, ND",
                                 "I want to work remotely"]),
    ]
    profile = {"resume_path": "data/resume.pdf", "preferences": {
        "how_did_you_hear": "Simplify", "willing_to_relocate": "Yes", "open_to_hybrid": "Yes",
        "open_to_onsite": "Yes"}}
    got = {a.key: a for a in answers.answer_fields(fields, job, profile)}
    assert got["source"].value == "Other" and got["source"].source == "profile"
    assert all(got[key].value is None and got[key].source == "unknown" for key in ("member", "locations"))

    unprobed_source = Field("source", "How did you hear about this job?", "combobox", True)
    inferred = answers.answer_fields([unprobed_source], job, profile)[0]
    assert inferred.value == "Simplify"  # An unobserved menu cannot establish an Other option.

    profile["company_answers"] = {job.company: {fields[0].label: "No",
                                               fields[2].label: ["Lake St. Louis, MO", "Mandan, ND"]}}
    confirmed = {a.key: a for a in answers.answer_fields(fields, job, profile)}
    assert confirmed["member"].value == "No" and confirmed["member"].source == "bank"
    assert confirmed["locations"].value == ["Lake St. Louis, MO", "Mandan, ND"]
    del profile["company_answers"]

    profile["preferences"]["willing_to_relocate"] = "No"
    assert answers.answer_fields(fields, job, profile)[2].source == "unknown"

    monkeypatch.setattr(answers, "resume_text", lambda path=None: "")
    assert answers.answer_fields(fields, job, profile)[0].source == "unknown"


def test_guidepoint_hours_require_explicit_confirmation(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    field = Field("hours", "Are you able to work 25-29hrs/wk Monday-Friday supporting the Eastern Time Zone "
                  "for 12 weeks? (Flexible hours)*", "combobox", True, ["Yes", "No"])
    profile = {"preferences": {"available_start": "May 2027", "available_end": "August 2027"},
               "personal": {"state": "New York"}}
    job = Job(id="test:guidepoint", company="GuidePoint Security", title="Intern",
              url="https://example.test/guidepoint", locations=[], category="Software", date_posted=0)
    got = answers.answer_fields([field], job, profile)[0]
    assert got.value is None and got.source == "unknown"
    profile["company_answers"] = {job.company: {field.label: "Yes"}}
    confirmed = answers.answer_fields([field], job, profile)[0]
    assert confirmed.value == "Yes" and confirmed.source == "bank"
    assert _answer_map([field], profile)["hours"].source == "unknown"


def test_guidepoint_degree_question_uses_confirmed_enrollment_and_exact_major(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    question = "Are you currently enrolled, or recently graduated with a degree in any of the following?"
    field = Field("degree_options", question, "combobox", True,
                  ["Artificial Intelligence", "Computer Science", "Cybersecurity", "None of the above"])
    job = Job(id="test:guidepoint", company="GuidePoint Security", title="Cloud Infrastructure Intern",
              url="https://example.test/guidepoint", locations=[], category="Software", date_posted=0)
    profile = {"education": {"school": "Stony Brook University", "major": "Computer Science",
                             "start_year": "2024", "graduation_year": "2028"}}

    got = answers.answer_fields([field], job, profile)[0]
    assert got.value == "Computer Science" and got.source == "profile"

    for changed in (dict(profile["education"], graduation_year="2024"),
                    dict(profile["education"], major="Mathematics"),
                    dict(profile["education"], school="")):
        unknown = answers.answer_fields([field], job, {"education": changed})[0]
        assert unknown.value is None and unknown.source == "unknown"

    unsupported = Field("degree_options", question, "combobox", True,
                        ["Cybersecurity", "None of the above"])
    assert answers.answer_fields([unsupported], job, profile)[0].source == "unknown"
    other = Job(id="test:other", company="Another Company", title="Intern",
                url="https://example.test/other", locations=[], category="Software", date_posted=0)
    assert answers.answer_fields([field], other, profile)[0].value == "Computer Science"


def test_historical_compound_and_other_subjects_bypass_profile_shortcuts(isolated_evidence, monkeypatch):
    def unavailable(*args, **kwargs):
        raise answers.llm.LLMUnavailable("offline")

    monkeypatch.setattr(answers.llm, "generate_json", unavailable)
    profile = {
        "education": {"degree": "Bachelor's"},
        "work_authorization": {"needs_sponsorship_now": "No", "needs_sponsorship_future": "Yes"},
        "preferences": {"open_to_remote": "Yes", "open_to_hybrid": "Yes", "open_to_onsite": "No",
                        "willing_to_relocate": "Yes"},
    }
    fields = [
        Field("direct_remote", "Are you open to remote work?", "radio", True, ["Yes", "No"]),
        Field("remote_history", "Have you worked remotely before?", "radio", True, ["Yes", "No"]),
        Field("compound_mode", "Are you open to hybrid or on-site work?", "radio", True, ["Yes", "No"]),
        Field("relocation_history", "Have you relocated in the past?", "radio", True, ["Yes", "No"]),
        Field("sponsorship_history", "Have you ever been sponsored by an employer?", "radio", True, ["Yes", "No"]),
        Field("sponsorship_type", "What type of visa sponsorship would you require?", "text", True),
        Field("job_degree", "Does this position require a degree?", "radio", True, ["Yes", "No"]),
    ]

    got = _answer_map(fields, profile)

    assert got["direct_remote"].value == "Yes" and got["direct_remote"].source == "profile"
    assert all(got[field.key].source == "unknown" and got[field.key].value is None for field in fields[1:])


def test_student_fact_can_be_answered_from_retrieved_profile_evidence(isolated_evidence, monkeypatch):
    seen = {}

    def answer_from_evidence(prompt, schema, **kwargs):
        payload = json.loads(prompt.split("\n", 1)[1])
        field = payload["fields"][0]
        seen["evidence"] = field["evidence"]
        return {"answers": [{"key": field["key"], "value": "Yes", "source": "bank",
                             "evidence_ids": [field["evidence"][0]["id"]]}]}

    monkeypatch.setattr(answers.llm, "generate_json", answer_from_evidence)
    field = Field("student", "Are you currently a student?", "radio", True, ["Yes", "No"])

    got = _answer_map([field], {"education": {"currently_enrolled": "Yes"}})["student"]

    assert got.value == "Yes" and got.source == "bank"
    assert any("currently_enrolled: Yes" in item["text"] for item in seen["evidence"])


@pytest.mark.parametrize("evidence_ids", [[], ["FOREIGN"], None])
def test_answers_without_field_scoped_citations_are_unknown(isolated_evidence, monkeypatch, evidence_ids):
    evidence = answers._Evidence("E1", "profile", "personal.country", "country: United States")
    monkeypatch.setattr(answers, "_evidence_corpus", lambda profile: [evidence])
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: {
        "answers": [{"key": "country", "value": "United States", "source": "bank",
                     "evidence_ids": evidence_ids}]
    })
    field = Field("country", "Country", "select", True, ["United States", "Canada"])

    got = _answer_map([field], {})["country"]

    assert got.source == "unknown" and got.value is None


def test_validation_uses_only_the_clipped_evidence_text(isolated_evidence, monkeypatch):
    full_text = "country: " + ("unrelated filler " * 40) + "United States"
    evidence = answers._Evidence("E1", "profile", "personal.country", full_text)
    monkeypatch.setattr(answers, "_evidence_corpus", lambda profile: [evidence])
    seen = {}

    def answer_from_prompt(prompt, schema, **kwargs):
        payload = json.loads(prompt.split("\n", 1)[1])
        clipped = payload["fields"][0]["evidence"][0]
        seen["text"] = clipped["text"]
        return {"answers": [{"key": "country", "value": "United States", "source": "bank",
                             "evidence_ids": [clipped["id"]]}]}

    monkeypatch.setattr(answers.llm, "generate_json", answer_from_prompt)
    field = Field("country", "Country", "select", True, ["United States", "Canada"])

    got = _answer_map([field], {})["country"]

    assert "United States" not in seen["text"]
    assert got.source == "unknown" and got.value is None


def test_prompt_budget_accounts_for_gemini_json_escaping():
    prompt = '"' * 24_000

    assert len(prompt.encode("utf-8")) < answers._MAX_ANSWER_PROMPT_BYTES
    assert not answers._answer_prompt_fits(prompt)


def _mock_cited_answer(monkeypatch, passages, value):
    evidence = [answers._Evidence(f"E{i}", "knowledge", f"synthetic#{i}", text)
                for i, text in enumerate(passages, 1)]
    monkeypatch.setattr(answers, "_evidence_corpus", lambda profile: evidence)

    def generate(prompt, schema, **kwargs):
        field = json.loads(prompt.split("\n", 1)[1])["fields"][0]
        return {"answers": [{"key": field["key"], "value": value, "source": "bank",
                             "evidence_ids": [item["id"] for item in field["evidence"]]}]}

    monkeypatch.setattr(answers.llm, "generate_json", generate)


@pytest.mark.parametrize(("passages", "supported"), [
    (["Nightglass protocol was tested but was not used."], False),
    (["Nightglass protocol was tested. It wasn't used."], False),
    (["I used Nightglass protocol.", "I never used Nightglass protocol."], False),
    (["I used Nightglass protocol and I did not use it."], False),
    (["I used Nightglass protocol. The protocol was not used."], False),
    (["I used Nightglass protocol. No protocols were used."], False),
    (["I used Nightglass protocol.", "No protocols were used."], False),
    (["I used Quartz protocol.", "Nightglass protocol was tested."], False),
    (["I used Nightglass protocol."], True),
    (["I used Nightglass protocol.", "I never used Quartz protocol."], True),
    (["I used Nightglass protocol but did not use Quartz protocol."], True),
])
def test_option_requires_linked_and_consistent_positive_evidence(isolated_evidence, monkeypatch, passages, supported):
    _mock_cited_answer(monkeypatch, passages, "Nightglass protocol")
    field = Field("protocol", "Which protocols have you used?", "select", True,
                  ["Nightglass protocol", "Quartz protocol"])

    got = _answer_map([field], {})["protocol"]

    assert (got.value, got.source) == (("Nightglass protocol", "bank") if supported else (None, "unknown"))


@pytest.mark.parametrize(("passage", "supported"), [
    ("Nightglass protocol was tested but was not used.", False),
    ("I used Nightglass protocol.", True),
])
def test_factual_free_text_respects_evidence_polarity(isolated_evidence, monkeypatch, passage, supported):
    _mock_cited_answer(monkeypatch, [passage], "Nightglass protocol")
    field = Field("protocol", "Which protocol have you used?", "text", True)

    got = _answer_map([field], {})["protocol"]

    assert (got.value, got.source) == (("Nightglass protocol", "bank") if supported else (None, "unknown"))


@pytest.mark.parametrize(("value", "passages", "supported"), [
    ("Yes", ["I used Nightglass protocol."], True),
    ("No", ["I did not use Nightglass protocol."], True),
    ("No", ["Nightglass protocol was tested but was not used."], True),
    ("No", ["No, I did not use Nightglass protocol."], True),
    ("Yes", ["Yes, I did not use Nightglass protocol."], False),
    ("Yes", ["I used Nightglass protocol.", "I never used Nightglass protocol."], False),
    ("No", ["No, I did not use Nightglass protocol and I used Nightglass protocol."], False),
    ("No", ["I used Nightglass protocol."], False),
])
def test_yes_no_options_require_consistent_polarity(isolated_evidence, monkeypatch, value, passages, supported):
    _mock_cited_answer(monkeypatch, passages, value)
    field = Field("protocol", "Have you used Nightglass protocol?", "radio", True, ["Yes", "No"])

    got = _answer_map([field], {})["protocol"]

    assert (got.value, got.source) == ((value, "bank") if supported else (None, "unknown"))


def test_explicit_negative_option_can_be_supported(isolated_evidence, monkeypatch):
    _mock_cited_answer(monkeypatch, ["I am not a veteran."], "I am not a veteran")
    field = Field("veteran", "What is your veteran status?", "select", True,
                  ["I am not a veteran", "I am a veteran"])

    got = _answer_map([field], {})["veteran"]

    assert got.value == "I am not a veteran" and got.source == "bank"


def test_explicit_none_option_can_be_supported(isolated_evidence, monkeypatch):
    _mock_cited_answer(monkeypatch, ["experience.protocols_used: None"], "None")
    field = Field("protocol", "Which protocols have you used?", "select", True,
                  ["Nightglass protocol", "None"])

    got = _answer_map([field], {})["protocol"]

    assert got.value == "None" and got.source == "bank"


def test_compeer_user_confirmed_answers_survive_a_new_run(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **kw: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    job = Job(id="test:compeer", company="Compeer Financial", title="Infrastructure Engineering Intern",
              url="https://example.test/compeer", locations=[], category="Software", date_posted=0)
    yes_internal = "Yes - Internal team members must apply through Compeer's Internal Careers page."
    labels = [
        "This internship requires full-time work during the summer. Are you able to work 40 hours/week during the summer?",
        "Our internship program requires students to be actively enrolled in college during the time of their internship. Will you be an active college student at the time of this internship?",
        "Based on the locations available from the job posting, please list your preferred office location(s). **Compeer does not provide housing reimbursement.:",
        "Do you have any agricultural experience? If yes, please explain.",
        "Are you a current team member at Compeer Financial?",
        "Are you a previous team member of Compeer Financial?",
        "Have you ever been employed at another Farm Credit institution?",
        "Are you open to relocating for this position, or are you currently within driving distance of the position location?",
    ]
    fields = [Field(str(i), label, "text" if i in {2, 3} else "select", True,
                    [] if i in {2, 3} else [yes_internal, "No"])
              for i, label in enumerate(labels)]
    got = {a.key: a for a in answers.answer_fields(fields, job, {})}
    assert [got[str(i)].source for i in range(len(fields))] == ["bank"] * len(fields)
    assert [got[str(i)].value for i in (0, 1, 4, 7)] == [yes_internal] * 4
    assert got["2"].value.startswith("Lakeville, MN")
    assert got["3"].value == "No"
    assert got["5"].value == "No" and got["6"].value == "No"

    other = Job(id="test:other", company="Other Financial", title=job.title, url=job.url,
                locations=[], category="Software", date_posted=0)
    outside = answers.answer_fields(fields, other, {})
    assert all(a.source == "unknown" for a in outside)


def test_profile_company_answers_are_exactly_scoped(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **kw: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    job = Job(id="test:custom", company="Example", title="Intern", url="https://example.test/job",
              locations=[], category="Software", date_posted=0)
    field = Field("k", "Can you work 40 hours a week?", "select", True, ["Yes", "No"])
    profile = {"company_answers": {"Example": {"Can you work 40 hours a week?": "Yes"}}}
    answer = answers.answer_fields([field], job, profile)[0]
    assert (answer.value, answer.source) == ("Yes", "bank")
    assert not any("40 hours" in item.text for item in answers._evidence_corpus(profile))
    other = Job(id="test:other", company="Other Company", title="Intern", url=job.url,
                locations=[], category="Software", date_posted=0)
    assert answers.answer_fields([field], other, profile)[0].source == "unknown"


def test_preferences_do_not_establish_promises_or_completed_education(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: pytest.fail("unconfirmed facts sent to LLM"))
    profile = {"personal": {"state": "New York"},
               "education": {"degree": "Bachelor's", "graduation_year": "2028"},
               "preferences": {"willing_to_relocate": "Yes", "open_to_onsite": "Yes",
                               "available_start": "May 2027", "available_end": "August 2027"}}
    fields = [
        Field("travel", "What percentage of the time are you willing to travel?", "combobox", True, ["0%", "1-10%"]),
        Field("location", "Which onsite location would you like to apply to?", "combobox", True,
              ["Austin", "South San Francisco", "No preference"]),
        Field("resident", "Will you be physically located within the United States during the entire duration of the internship?",
              "combobox", True, ["Yes", "No"]),
        Field("future", "Where will you primarily be located during the internship?", "combobox", True, ["New York", "Texas"]),
        Field("cost", "If hired, are you able to relocate on your own?", "combobox", True, ["Yes", "No"]),
        Field("completed", "What is your most recently completed form of education?", "combobox", True,
              ["High School", "Bachelor's Degree"]),
        Field("arrangement", "Preferred work arrangement", "select", True, ["Hybrid", "Remote", "Onsite"]),
    ]
    got = _answer_map(fields, profile)
    assert all(a.value is None and a.source == "unknown" for a in got.values())
    profile["education"]["highest_completed"] = "High School"
    profile["preferences"]["travel_percentage"] = "0%"
    profile["preferences"]["preferred_work_arrangement"] = "Hybrid"
    confirmed = _answer_map(fields, profile)
    assert confirmed["completed"].value == "High School" and confirmed["travel"].value == "0%"
    assert confirmed["arrangement"].value == "Hybrid"
    assert "sms_opt_in" not in profile["preferences"]


@pytest.mark.parametrize(("value", "supported"), [
    ("Built a C++ store sustaining 159K operations/second across 512 clients.", True),
    ("Built a C++ store sustaining 160K operations/second across 512 clients.", False),
    ("Built embedded firmware sustaining 159K operations/second across 512 clients.", False),
    ("Built a C++ store sustaining 159K operations/second across 512 clients. I will relocate at my own expense.", False),
])
def test_experience_narratives_require_copied_claims_and_metrics(isolated_evidence, monkeypatch, value, supported):
    _mock_cited_answer(monkeypatch, ["Project: Built a C++ store sustaining 159K operations/second across 512 clients."], value)
    field = Field("example", "Describe a project experience using quantitative metrics.", "textarea", True)
    got = _answer_map([field], {})["example"]
    assert (got.value, got.source) == ((value, "bank") if supported else (None, "unknown"))


def test_numbered_examples_keep_series_context_without_reusing_or_inventing_evidence(isolated_evidence, monkeypatch):
    resume = ("Projects\nCache | C++\n• Sustained 159K operations/second across 512 clients.\n"
              "Search | Python\n• Indexed 200,000 products and reduced latency by 98%.")
    monkeypatch.setattr(answers, "resume_text", lambda path=None: resume)
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: pytest.fail("numbered examples need no generation"))
    series = [Field("first", "Provide examples of exceptional ability using quantitative metrics. First example:", "textarea", True),
              Field("second", "Second example:", "textarea", True),
              Field("third", "Third example:", "textarea", True)]
    profile = {"resume_path": "resume.pdf"}
    got = _answer_map(series[1:], profile, context_fields=series)
    assert got["second"].value == "Indexed 200,000 products and reduced latency by 98%."
    assert got["third"].value is None and got["third"].source == "unknown"
    evidence = answers._evidence_corpus(profile)
    assert any("Search | Python" in item.text and "200,000" in item.text for item in evidence)


def test_missing_experience_does_not_establish_negative_experience_or_numeric_pay(isolated_evidence, monkeypatch):
    _mock_cited_answer(monkeypatch, ["I built Python preprocessing pipelines and used AWS."], "No formal experience")
    field = Field("pipeline", "Which statement best describes your experience building or managing CI/CD pipelines?",
                  "combobox", True, ["No formal experience", "Created production-ready CI/CD pipelines"])
    assert _answer_map([field], {})["pipeline"].value is None
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(answers.llm.LLMUnavailable("offline")))
    pay = Field("pay", "Target Compensation", "combobox", True, ["less than 25k", "26-30k"])
    profile = {"answer_bank": {"salary_expectation": "Open to the standard intern compensation for this role."}}
    assert _answer_map([pay], profile)["pay"].value is None


def test_pdf_font_changes_preserve_word_and_metric_boundaries(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    fonts = DictionaryObject({NameObject(name): DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject(face)})
                              for name, face in (("/F1", "/Helvetica"), ("/F2", "/Helvetica-Bold"))})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 700 Td (Sustained) Tj /F2 12 Tf (159K operations/second) Tj /F1 12 Tf (across 512 clients.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "resume.pdf"
    writer.write(path)
    assert answers.resume_text(str(path)) == "Sustained 159K operations/second across 512 clients."


def test_school_coercion_preserves_requested_identity_in_partial_dropdown_results(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(answers.llm.LLMUnavailable("offline")))
    field = Field("school", "Education: School", "combobox", True, ["Aston University", "Boston University"])
    profile = {"education": {"school": "Stony Brook University"}}
    got = _answer_map([field], profile)["school"]
    assert got.value == "Stony Brook University" and got.source == "profile"
    field.options = ["Stony Brook University"]
    assert _answer_map([field], profile)["school"].value == "Stony Brook University"


def test_education_dates_do_not_supply_missing_months_or_years(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(
        answers.llm.LLMUnavailable("offline")))
    fields = [
        Field("start_month", "Education: Start date month", "combobox", True, ["August", "September"]),
        Field("start_year", "Education: Start date year", "text", True),
        Field("start_date", "Education: Start date", "text", True, placeholder="MM/YYYY"),
        Field("end_month", "Education: End date month", "combobox", True, ["May", "December"]),
        Field("end_year", "Education: End date year", "text", True),
        Field("end_date", "Education: End date", "text", True, placeholder="MM/YYYY"),
    ]
    assert all(a.source == "unknown" and a.value is None for a in _answer_map(fields, {}).values())
    profile = {"education": {"start_year": "2026", "graduation_year": "2031"}}
    got = _answer_map(fields, profile)
    assert got["start_year"].value == "2026" and got["end_year"].value == "2031"
    assert all(got[key].source == "unknown" and got[key].value is None
               for key in ("start_month", "start_date", "end_month", "end_date"))


def test_education_month_controls_preserve_profile_month_across_display_formats(isolated_evidence):
    profile = {"education": {"start_date": "September 2026", "graduation_date": "December 2031"},
               "preferences": {"available_start": "June 2029"}}
    fields = [
        Field("start", "Education: Start date month", "select", True, ["8", "9"]),
        Field("end", "Education: End date month", "select", True, ["May", "December"]),
        Field("short", "Education: End date month", "select", True, ["May", "Dec"]),
        Field("year", "Education: End date year", "select", True, ["2028", "2031"]),
        Field("available", "Available start month", "select", True, ["05", "06"]),
    ]
    got = _answer_map(fields, profile)
    assert [got[f.key].value for f in fields] == ["9", "December", "Dec", "2031", "06"]
    assert all(a.source == "profile" for a in got.values())


def test_conflicting_education_date_components_stay_unknown(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: pytest.fail("conflicting dates sent to LLM"))
    profile = {"education": {"graduation_date": "May 2028", "graduation_month": "December",
                             "graduation_year": "2028"}}
    fields = [Field("month", "Education: End date month", "text", True),
              Field("year", "Education: End date year", "text", True)]
    got = _answer_map(fields, profile)
    assert all(a.source == "unknown" and a.value is None and a.note == "conflicting profile values"
               for a in got.values())


@pytest.mark.parametrize("option", ["Billboard", "Career Fair", "School Career Website"])
def test_required_referral_does_not_select_an_unrelated_source(isolated_evidence, option):
    field = Field("source", "How did you hear about us?", "combobox", True, [option])
    got = _answer_map([field], {"preferences": {"how_did_you_hear": "Simplify"}})["source"]
    assert got.value is None and got.source == "unknown"


@pytest.mark.parametrize(("question", "options"), [
    ("Which statement best describes your experience building or managing CI/CD pipelines?",
     ["No formal experience", "General user pushing code and triggering pipelines",
      "Maintained or debugged existing pipelines", "Created production-ready CI/CD pipelines"]),
    ("Which statement best describes your experience with Cloud Infrastructure and IaC?",
     ["None - I have no experience with IaC",
      "Basic - I have a conceptual cloud knowledge but limited hands-on IaC experience",
      "Intermediate - I have worked with Terraform, Ansible, and GitHub Actions before",
      "Advanced - I have written reusable IaC modules and automation pipelines used in production"]),
])
def test_confirmed_experience_not_represented_by_observed_choices_stays_unknown(
        isolated_evidence, monkeypatch, question, options):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: pytest.fail("unrepresentable confirmation sent to LLM"))
    field = Field("experience", question, "combobox", True, options)
    profile = {"company_answers": {JOB.company: {question: "I have coursework or personal project experience"}}}
    got = _answer_map([field], profile)["experience"]
    assert got.source == "unknown" and got.value is None and got.note == "company option unavailable"
    profile["company_answers"][JOB.company][question] = options[1]
    confirmed = _answer_map([field], profile)["experience"]
    assert confirmed.source == "bank" and confirmed.value == options[1]


def test_city_autocomplete_rejects_conflicting_profile_locations(isolated_evidence, monkeypatch):
    monkeypatch.setattr(answers.llm, "generate_json", lambda *a, **k: pytest.fail("conflicting locations sent to LLM"))
    field = Field("city", "Location (City)", "combobox", True)
    profile = {"personal": {"location": "Boston, MA", "city": "New York", "state": "New York",
                             "state_abbr": "NY", "country": "United States"}}
    got = _answer_map([field], profile)["city"]
    assert got.source == "unknown" and got.value is None and got.note == "conflicting profile values"
