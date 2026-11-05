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


def test_email_initialization_paginates_and_strips_nonidentity_query(prior_db, monkeypatch):
    import base64
    import json
    from agent import mail, application_history
    monkeypatch.setattr(mail, 'available', lambda: True)
    calls = []
    def gapi(path, **kwargs):
        calls.append((path, kwargs))
        if path == 'messages':
            if kwargs.get('pageToken') == 'second':
                return {'messages': [{'id': 'two'}]}
            return {'messages': [{'id': 'one'}], 'nextPageToken': 'second'}
        body = ('Application for Software Engineer role. '
                'https://boards.greenhouse.io/acme/jobs/123?token=SECRET&utm_source=email'
                if path.endswith('one') else 'Your application was received.')
        return {'id': path.split('/')[-1], 'internalDate': '1790000000000', 'payload': {
            'mimeType': 'text/plain', 'headers': [{'name': 'Subject', 'value': 'Thank you for applying to Acme!'}],
            'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}
    monkeypatch.setattr(mail, '_gapi', gapi)
    result = application_history.initialize_from_emails()
    assert result['synced'] == 2 and result['linked'] == 1 and result['unlinked'] == 1
    assert any(kwargs.get('pageToken') == 'second' for _, kwargs in calls)
    with db.connect() as c:
        urls = c.execute('SELECT job_urls FROM email_confirmations WHERE message_id=?', ('one',)).fetchone()[0]
    assert json.loads(urls) == ['https://boards.greenhouse.io/acme/jobs/123']
    assert 'SECRET' not in urls
    assert application_history.initialize_from_emails()['imported'] == 0


def test_submission_and_history_receipt_share_transaction(prior_db, monkeypatch):
    from agent import application_history
    from agent.models import Job, ApplyResult, ApplyStatus
    job = Job('atomic', 'Acme', 'Engineer', 'https://example.test/jobs/1', [], 'Software', 1)
    db.upsert_jobs([job])
    def unavailable(*args):
        raise RuntimeError('synthetic ledger failure')
    monkeypatch.setattr(application_history, 'record_receipt', unavailable)
    with pytest.raises(RuntimeError, match='ledger failure'):
        db.record_application(job, ApplyResult(ApplyStatus.SUBMITTED, 'fixture'), None)
    assert db.latest_application(job.id) is None


def test_recommendation_links_do_not_become_submission_evidence():
    url = 'https://boards.greenhouse.io/acme/jobs/123'
    role = 'Software Engineer'
    text = f'Application for {role} role. {url}'
    assert prior_apps._confirmed_urls(text, '', role) == [url]
    recommended = f'<p>Recommended jobs:</p><a href="{url}">{role}</a>'
    assert prior_apps._confirmed_urls('Your application was received.', recommended, role) == []
    confirmed = f'<p>We received your application for <a href="{url}">{role}</a>.</p>'
    assert prior_apps._confirmed_urls('Your application was received.', confirmed, role) == [url]
    assert prior_apps._confirmed_urls(text + ' https://boards.greenhouse.io/acme/jobs/456', '', role) == []
    assert prior_apps._posting_urls('https://acme.test/careers/detail?position=123') == []


def test_bad_email_does_not_prevent_later_messages_from_indexing(prior_db, monkeypatch):
    import base64
    from agent import mail, application_history
    monkeypatch.setattr(mail, 'available', lambda: True)
    def gapi(path, **kwargs):
        if path == 'messages':
            return {'messages': [{'id': 'bad'}, {'id': 'good'}]}
        if path.endswith('bad'):
            raise ValueError('malformed synthetic message')
        return {'id': 'good', 'internalDate': '1790000000000', 'payload': {
            'headers': [], 'mimeType': 'text/plain',
            'body': {'data': base64.urlsafe_b64encode(b'Your application was received.').decode()}}}
    monkeypatch.setattr(mail, '_gapi', gapi)
    with pytest.raises(RuntimeError, match='could not be indexed'):
        application_history.initialize_from_emails()
    with db.connect() as c:
        assert c.execute('SELECT message_id FROM email_confirmations').fetchone()[0] == 'good'
        assert c.execute("SELECT count(*) FROM application_history WHERE source='email'").fetchone()[0] == 1


def test_email_without_valid_date_cannot_poison_cached_history():
    with pytest.raises(ValueError, match='timestamp'):
        prior_apps._extract_email_info({'id': 'synthetic', 'payload': {'headers': []}})
