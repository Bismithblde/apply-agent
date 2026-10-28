from __future__ import annotations

import time
import pytest
from agent import db, prior_apps


@pytest.fixture
def prior_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "apply.db")
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.init()
    prior_apps.init_db()
    return tmp_path


def test_is_purely_same_role_matches_exact_and_minor_synonyms():
    assert prior_apps.is_purely_same_role(
        "Enterprise Software Engineer Intern",
        "the Enterprise Software Engineering Intern (Summer 2027)",
    )
    assert prior_apps.is_purely_same_role(
        "Software Engineer Intern",
        "Software Engineering Intern (2027)",
    )


def test_is_purely_same_role_rejects_distinct_specialties():
    # Embedded vs Autonomy
    assert not prior_apps.is_purely_same_role(
        "Autonomy Engineer Intern, Computer Vision / Deep Learning",
        "Embedded Software Engineer Intern - Summer 2027",
    )
    # Flight vs Enterprise
    assert not prior_apps.is_purely_same_role(
        "Flight Software Engineer Intern",
        "Enterprise Software Engineering Intern",
    )
    # Route Planning vs General
    assert not prior_apps.is_purely_same_role(
        "Route Planning Software Engineering Intern",
        "Software Engineering Intern",
    )


def test_is_purely_same_role_allows_different_posting_times():
    # If job was posted 10 days after confirmation email was received, it's a re-post / new opening
    email_ts = 1792000000.0
    reposted_job_ts = email_ts + 86400 * 10
    assert not prior_apps.is_purely_same_role(
        "Software Engineer Intern",
        "Software Engineer Intern",
        job_posted_ts=reposted_job_ts,
        email_received_ts=email_ts,
    )

    # If job was posted shortly before or around the email, it IS the same application
    same_cycle_job_ts = email_ts - 86400 * 2
    assert prior_apps.is_purely_same_role(
        "Software Engineer Intern",
        "Software Engineer Intern",
        job_posted_ts=same_cycle_job_ts,
        email_received_ts=email_ts,
    )


def test_is_purely_same_role_rejects_prior_year_cycles():
    assert not prior_apps.is_purely_same_role(
        "Software Engineer Intern",
        "Software Engineer Intern (Summer 2026)",
    )


def test_find_prior_application_with_stored_confirmations(prior_db):
    with db.connect() as c:
        c.execute("""
            INSERT INTO email_confirmations
            (message_id, company, title, term, received_at, raw_subject, snippet)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "msg-1",
            "Impulse Space",
            "Enterprise Software Engineering Intern",
            "Summer 2027",
            time.time(),
            "Application Received",
            "Thanks for applying",
        ))

    # Same role matches
    found = prior_apps.find_prior_application(
        "Impulse Space",
        "Enterprise Software Engineer Intern",
    )
    assert found is not None
    assert found["company"] == "Impulse Space"

    # Different role does not match
    not_found = prior_apps.find_prior_application(
        "Impulse Space",
        "Flight Software Engineer Intern",
    )
    assert not_found is None
