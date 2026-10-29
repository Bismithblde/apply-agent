from __future__ import annotations

import io
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent import db, interview
from agent.interview import (
    InterviewQuit,
    ProfileUpdate,
    QuestionCluster,
    UnansweredQuestion,
    classify_question,
    cluster_questions,
    format_prompt,
    prompt_user,
    read_unanswered_questions,
    run_interview,
    update_answers_bank,
    update_profile_yaml,
)


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_apply.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(db.SCHEMA)
    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def sample_profile_yaml(tmp_path: Path) -> Path:
    profile_path = tmp_path / "profile.yaml"
    content = """# Applicant Profile
# Everything the agent is allowed to state
personal:
  first_name: Ryan
  last_name: Chen
  email: test@example.com

preferences:
  willing_to_relocate: "Yes"
  open_to_remote: "Yes"

# Pre-existing company answers
company_answers:
  Neuralink:
    "Which onsite location would you like to apply to?": "Austin"
"""
    profile_path.write_text(content, encoding="utf-8")
    return profile_path


# ==============================================================================
# 1. Question Classification Tests
# ==============================================================================


def test_classify_company_name_in_question():
    category, section, subsection, key = classify_question(
        "Have you previously worked for CoStar Group or its acquired companies?",
        company="CoStar Group",
    )
    assert category == "company"
    assert section == "company_answers"
    assert subsection == "CoStar Group"
    assert "CoStar Group" in key


def test_classify_company_name_cleaned_match():
    category, section, subsection, key = classify_question(
        "Do you have any direct or indirect relationship with any Alstom employee?",
        company="Alstom Inc.",
    )
    assert category == "company"
    assert section == "company_answers"
    assert subsection == "Alstom Inc."


def test_classify_company_specific_phrasing():
    category, section, subsection, key = classify_question(
        "Do you have a relative or family member working for the Company?",
        company="Skydio",
    )
    assert category == "company"
    assert section == "company_answers"
    assert subsection == "Skydio"


def test_classify_company_consent_terms():
    category, section, subsection, key = classify_question(
        "I consent to the terms and conditions and privacy policy.",
        company="GuidePoint Security",
    )
    assert category == "company"
    assert section == "company_answers"
    assert subsection == "GuidePoint Security"


def test_classify_general_salary_question():
    category, section, subsection, key = classify_question(
        "What are your salary expectations for this internship?",
        company="Acme Corp",
    )
    assert category == "general"
    assert section == "custom_answers"
    assert subsection is None
    assert key == "What are your salary expectations for this internship?"


def test_classify_general_skills_question():
    category, section, subsection, key = classify_question(
        "How many years of experience do you have with Python and Docker?",
        company="Tech Innovators",
    )
    assert category == "general"
    assert section == "custom_answers"
    assert subsection is None


def test_classify_general_preferences_mapping():
    category, section, subsection, key = classify_question(
        "Are you willing to relocate to another city?",
        company="Global Logistics",
    )
    assert category == "general"
    assert section == "preferences"
    assert key == "willing_to_relocate"


def test_is_transient_question_detects_signature_date_and_name():
    from agent.interview import is_transient_question
    assert is_transient_question("Signature Date") is True
    assert is_transient_question("Date Signed") is True
    assert is_transient_question("Signed on") is True
    assert is_transient_question("Date") is True
    assert is_transient_question("Today's Date") is True
    assert is_transient_question("Full Name") is True
    assert is_transient_question("Legal Name") is True
    assert is_transient_question("Applicant Name") is True
    assert is_transient_question("Name") is True
    assert is_transient_question("Confirm Email") is True
    assert is_transient_question("Please complete the CAPTCHA") is True
    # Non-transient questions
    assert is_transient_question("What is your expected graduation date?") is False
    assert is_transient_question("What is your desired salary?") is False
    assert is_transient_question("Are you willing to work on-site in Austin?") is False


# ==============================================================================
# 2. Question Clustering and Deduplication Tests
# ==============================================================================


def test_cluster_deduplicates_general_questions_across_companies():
    q1 = UnansweredQuestion(
        job_id="job_1",
        company="Copart",
        title="Software Engineer Intern",
        question="What are your salary expectations?",
    )
    q2 = UnansweredQuestion(
        job_id="job_2",
        company="Astrolab",
        title="Backend Engineer Intern",
        question="What are your salary expectations?",
    )
    q3 = UnansweredQuestion(
        job_id="job_3",
        company="Skydio",
        title="Robotics Intern",
        question="What are your salary expectations?",
    )

    clusters = cluster_questions([q1, q2, q3])
    assert len(clusters) == 1
    c = clusters[0]
    assert c.category == "general"
    assert c.target_section == "custom_answers"
    assert set(c.job_ids) == {"job_1", "job_2", "job_3"}
    assert set(c.companies) == {"Copart", "Astrolab", "Skydio"}


def test_cluster_separates_company_specific_questions():
    q1 = UnansweredQuestion(
        job_id="job_alstom_1",
        company="Alstom",
        title="ATC Intern",
        question="Have you previously worked at Alstom?",
    )
    q2 = UnansweredQuestion(
        job_id="job_skydio_1",
        company="Skydio",
        title="Autonomy Intern",
        question="Have you previously worked at Skydio?",
    )

    clusters = cluster_questions([q1, q2])
    assert len(clusters) == 2
    assert {c.company for c in clusters} == {"Alstom", "Skydio"}
    assert all(c.category == "company" for c in clusters)


def test_cluster_merges_same_company_questions():
    q1 = UnansweredQuestion(
        job_id="job_alstom_1",
        company="Alstom",
        title="ATC Intern",
        question="Have you previously worked at Alstom?",
    )
    q2 = UnansweredQuestion(
        job_id="job_alstom_2",
        company="Alstom",
        title="Embedded Intern",
        question="Have you previously worked at Alstom?",
    )

    clusters = cluster_questions([q1, q2])
    assert len(clusters) == 1
    assert set(clusters[0].job_ids) == {"job_alstom_1", "job_alstom_2"}


def test_cluster_binary_options_inference():
    q = UnansweredQuestion(
        job_id="job_1",
        company="TestCo",
        title="Engineer",
        question="Are you authorized to work in the US?",
        options=[],
    )
    clusters = cluster_questions([q])
    assert len(clusters) == 1
    assert clusters[0].options == ["Yes", "No"]


# ==============================================================================
# 3. Reading Questions from SQLite Tests
# ==============================================================================


def test_read_unanswered_questions_from_db(test_db: Path):
    now = 1700000000.0
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_1", "test", "Alstom", "Software Intern", "https://alstom.com/job1", "alstom.com/job1", "alstom|1", "other", now, "needs_review"),
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_1", "needs_review", "missing required fields", json.dumps(["Prior Employment?"]), now),
        )

        # Job 2 with rich metadata
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_2", "test", "Skydio", "Autonomy Intern", "https://skydio.com/job2", "skydio.com/job2", "skydio|2", "other", now, "needs_review"),
        )
        rich_unanswered = [
            {"key": "aa-5", "label": "Willing to work on-site in San Mateo?", "kind": "radio", "options": ["Yes", "No"]}
        ]
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_2", "needs_review", "missing required fields", json.dumps(rich_unanswered), now),
        )

    questions = read_unanswered_questions(db_path=test_db)
    assert len(questions) == 2
    q1 = next(q for q in questions if q.job_id == "job_1")
    assert q1.question == "Prior Employment?"
    assert q1.company == "Alstom"

    q2 = next(q for q in questions if q.job_id == "job_2")
    assert q2.question == "Willing to work on-site in San Mateo?"
    assert q2.options == ["Yes", "No"]


def test_read_unanswered_questions_filters_transient_derived(test_db: Path):
    now = 1700000000.0
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_transient", "test", "Acme", "Intern", "https://acme.com/1", "acme.com/1", "acme|1", "other", now, "needs_review"),
        )
        unanswered = [
            {"key": "f1", "label": "Signature Date", "kind": "text"},
            {"key": "f2", "label": "Full Name", "kind": "text"},
            {"key": "f3", "label": "What is your primary programming language?", "kind": "text"},
        ]
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_transient", "needs_review", "missing required fields", json.dumps(unanswered), now),
        )

    questions = read_unanswered_questions(job_ids=["job_transient"], db_path=test_db)
    assert len(questions) == 1
    assert questions[0].question == "What is your primary programming language?"


# ==============================================================================
# 4. Interactive Formatting & Prompt User Tests
# ==============================================================================


def test_format_prompt_renders_choices_and_context():
    cluster = QuestionCluster(
        cluster_id="cl_1",
        question="Do you have security clearance?",
        category="general",
        company=None,
        target_section="custom_answers",
        target_key="Do you have security clearance?",
        options=["Yes", "No", "Confidential"],
        job_titles=["Skydio: Autonomy Intern"],
    )
    prompt_str = format_prompt(cluster, index=1, total=3)
    assert "Question [1/3]" in prompt_str
    assert "Do you have security clearance?" in prompt_str
    assert "[1] Yes" in prompt_str
    assert "[2] No" in prompt_str
    assert "[3] Confidential" in prompt_str
    assert "[s] Skip this question" in prompt_str
    assert "[q] Quit interview" in prompt_str


def test_prompt_user_option_number_selection():
    cluster = QuestionCluster(
        cluster_id="cl_1",
        question="Select your location",
        category="company",
        company="Neuralink",
        target_section="company_answers",
        target_key="Select your location",
        options=["Austin, TX", "Fremont, CA"],
    )
    out = io.StringIO()
    # User selects "1"
    ans = prompt_user(cluster, index=1, total=1, input_fn=lambda _: "1", output_stream=out)
    assert ans == "Austin, TX"

    # User selects "2"
    ans2 = prompt_user(cluster, index=1, total=1, input_fn=lambda _: "2", output_stream=out)
    assert ans2 == "Fremont, CA"


def test_prompt_user_custom_text():
    cluster = QuestionCluster(
        cluster_id="cl_1",
        question="Desired compensation",
        category="general",
        company=None,
        target_section="custom_answers",
        target_key="Desired compensation",
        options=[],
    )
    out = io.StringIO()
    ans = prompt_user(cluster, index=1, total=1, input_fn=lambda _: "$55/hr", output_stream=out)
    assert ans == "$55/hr"


def test_prompt_user_skip():
    cluster = QuestionCluster(
        cluster_id="cl_1",
        question="Optional question",
        category="general",
        company=None,
        target_section="custom_answers",
        target_key="Optional question",
    )
    out = io.StringIO()
    ans = prompt_user(cluster, index=1, total=1, input_fn=lambda _: "s", output_stream=out)
    assert ans is None


def test_prompt_user_quit_raises():
    cluster = QuestionCluster(
        cluster_id="cl_1",
        question="Stop here",
        category="general",
        company=None,
        target_section="custom_answers",
        target_key="Stop here",
    )
    out = io.StringIO()
    with pytest.raises(InterviewQuit):
        prompt_user(cluster, index=1, total=1, input_fn=lambda _: "q", output_stream=out)


# ==============================================================================
# 5. Atomic Profile YAML Updating Tests
# ==============================================================================


def test_update_profile_yaml_atomic(sample_profile_yaml: Path):
    updates = [
        ProfileUpdate(
            target_section="company_answers",
            subsection="Neuralink",
            target_key="Are you able to relocate on your own?",
            answer="Yes",
        ),
        ProfileUpdate(
            target_section="company_answers",
            subsection="Alstom",
            target_key="Prior Employment?",
            answer="No",
        ),
        ProfileUpdate(
            target_section="custom_answers",
            subsection=None,
            target_key="Salary Expectation",
            answer="$50/hr",
        ),
        ProfileUpdate(
            target_section="preferences",
            subsection=None,
            target_key="security_clearance",
            answer="No",
        ),
    ]

    update_profile_yaml(sample_profile_yaml, updates)

    # Verify updated content
    content = sample_profile_yaml.read_text(encoding="utf-8")
    assert "# Applicant Profile" in content  # Comments preserved!
    assert "Neuralink:" in content
    assert "Are you able to relocate on your own?" in content
    assert "Alstom:" in content
    assert "Prior Employment?" in content
    assert "Salary Expectation" in content
    assert "security_clearance" in content


# ==============================================================================
# 6. SQLite Knowledge Bank and Invalidation Tests
# ==============================================================================


def test_update_answers_bank_persists_to_sqlite(test_db: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", test_db)
    updates = [
        ProfileUpdate(
            target_section="company_answers",
            subsection="Alstom",
            target_key="Prior Employment?",
            answer="No",
            question_text="Prior Employment?",
        ),
        ProfileUpdate(
            target_section="custom_answers",
            subsection=None,
            target_key="Salary Expectation",
            answer="$50/hr",
            question_text="What are your salary expectations?",
        ),
    ]

    update_answers_bank(updates, db_path=test_db)

    rows = db.get_answers_bank()
    assert len(rows) == 2
    alstom_row = next(r for r in rows if r["company"] == "Alstom")
    assert alstom_row["question"] == "Prior Employment?"
    assert alstom_row["answer"] == "No"
    assert alstom_row["source"] == "user_interview"

    general_row = next(r for r in rows if not r["company"])
    assert general_row["question"] == "What are your salary expectations?"
    assert general_row["answer"] == "$50/hr"


# ==============================================================================
# 7. Complete Dry-Run Interview Simulation Tests
# ==============================================================================


def test_interview_simulation_full_flow(test_db: Path, sample_profile_yaml: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", test_db)
    now = 1700000000.0

    # Create 3 jobs in database with unanswered questions:
    # Job 1 (Copart): Salary Expectation
    # Job 2 (Astrolab): Salary Expectation (same general question!)
    # Job 3 (Alstom): Prior Employment at Alstom?
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_copart", "test", "Copart", "SE Intern", "https://copart.test/1", "copart.test/1", "copart|1", "other", now, "needs_review"),
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_copart", "needs_review", "missing fields", json.dumps(["What are your salary expectations?"]), now),
        )

        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_astrolab", "test", "Astrolab", "Backend Intern", "https://astrolab.test/2", "astrolab.test/2", "astrolab|2", "other", now, "needs_review"),
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_astrolab", "needs_review", "missing fields", json.dumps(["What are your salary expectations?"]), now),
        )

        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_alstom", "test", "Alstom", "ATC Intern", "https://alstom.test/3", "alstom.test/3", "alstom|3", "other", now, "needs_review"),
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_alstom", "needs_review", "missing fields", json.dumps([{"label": "Have you worked at Alstom before?", "kind": "radio", "options": ["Yes", "No"]}]), now),
        )

    # Mocked input answers:
    # 1. Salary expectation -> "$120k"
    # 2. Alstom prior employment -> "2" (maps to "No")
    inputs = iter(["$120k", "2"])
    mock_input = lambda _: next(inputs)
    out_buf = io.StringIO()

    unlocked = run_interview(
        profile_path=sample_profile_yaml,
        db_path=test_db,
        input_fn=mock_input,
        output_stream=out_buf,
    )

    # All 3 jobs should be unlocked!
    assert set(unlocked) == {"job_copart", "job_astrolab", "job_alstom"}

    # Database job statuses should now be reset to 'new'
    with sqlite3.connect(test_db) as conn:
        statuses = {row[0]: row[1] for row in conn.execute("SELECT id, status FROM jobs").fetchall()}
        assert statuses["job_copart"] == "new"
        assert statuses["job_astrolab"] == "new"
        assert statuses["job_alstom"] == "new"

    # Profile should contain both answers
    profile_text = sample_profile_yaml.read_text(encoding="utf-8")
    assert "$120k" in profile_text
    assert "Have you worked at Alstom before?" in profile_text
    assert "No" in profile_text


def test_interview_partial_answering_leaves_incomplete_job_locked(test_db: Path, sample_profile_yaml: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", test_db)
    now = 1700000000.0

    # Job has TWO required unanswered questions
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_multi", "test", "MultiCo", "Intern", "https://multico.test/1", "multico.test/1", "multico|1", "other", now, "needs_review"),
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_multi", "needs_review", "missing fields", json.dumps(["Question A", "Question B"]), now),
        )

    # User answers Question A, but skips Question B ("s")
    inputs = iter(["Answer A", "s"])
    mock_input = lambda _: next(inputs)
    out_buf = io.StringIO()

    unlocked = run_interview(
        profile_path=sample_profile_yaml,
        db_path=test_db,
        input_fn=mock_input,
        output_stream=out_buf,
    )

    # Job should NOT be unlocked because Question B was skipped!
    assert "job_multi" not in unlocked

    # Job status remains 'needs_review'
    with sqlite3.connect(test_db) as conn:
        status = conn.execute("SELECT status FROM jobs WHERE id='job_multi'").fetchone()[0]
        assert status == "needs_review"

    # But Question A was persisted
    profile_text = sample_profile_yaml.read_text(encoding="utf-8")
    assert "Answer A" in profile_text


def test_interview_quit_preserves_partial_progress(test_db: Path, sample_profile_yaml: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", test_db)
    now = 1700000000.0

    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_1", "test", "Company1", "Intern", "https://c1.test", "c1.test", "c1|1", "other", now, "needs_review"),
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_1", "needs_review", "missing", json.dumps(["First Question"]), now),
        )
        conn.execute(
            "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, first_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("job_2", "test", "Company2", "Intern", "https://c2.test", "c2.test", "c2|1", "other", now, "needs_review"),
        )
        conn.execute(
            "INSERT INTO applications (job_id, status, reason, unanswered_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("job_2", "needs_review", "missing", json.dumps(["Second Question"]), now),
        )

    # User answers first question, then quits with "q"
    inputs = iter(["Answer 1", "q"])
    mock_input = lambda _: next(inputs)
    out_buf = io.StringIO()

    unlocked = run_interview(
        profile_path=sample_profile_yaml,
        db_path=test_db,
        input_fn=mock_input,
        output_stream=out_buf,
    )

    # Job 1 was unlocked
    assert unlocked == ["job_1"]

    # First answer was saved to profile.yaml
    profile_text = sample_profile_yaml.read_text(encoding="utf-8")
    assert "Answer 1" in profile_text


def test_is_transient_question_filtered_from_clusters():
    from agent.interview import is_transient_question
    assert is_transient_question("Date Signed")
    assert is_transient_question("Signature Date")
    assert is_transient_question("Date")
    assert is_transient_question("Retype Email Address")
    assert is_transient_question("re-type email")
    assert is_transient_question("Turnstile CAPTCHA")
    assert not is_transient_question("Are you legally authorized to work in the US?")
    assert not is_transient_question("Expected graduation date")

    qs = [
        UnansweredQuestion(job_id="1", company="C", title="T", question="Signature Date"),
        UnansweredQuestion(job_id="1", company="C", title="T", question="Legally authorized to work in US?"),
    ]
    clusters = cluster_questions(qs)
    assert len(clusters) == 1
    assert "authorized" in clusters[0].question.lower()
