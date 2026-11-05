import json
import sqlite3
from dataclasses import replace

import pytest

from agent import application_history as history, db, prior_apps
from agent.models import Job

DAY = 86400
NOW = 1800000000


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'history.db')
    db.init()


@pytest.fixture
def job():
    return Job('1', 'Example', 'Software Engineer',
               'https://jobs.lever.co/example/abc', [], '', NOW - DAY)


def test_exact_identity_reposts_and_distinct_postings(job):
    history.record_application(job, NOW)
    assert history.find_application(replace(job, url=job.url + '/?utm_source=test'))
    assert history.find_application(replace(job, date_posted=0))
    assert history.find_application(replace(job, date_posted=NOW))
    assert history.find_application(replace(job, date_posted=NOW + 1)) is None
    assert history.find_application(replace(job, date_posted=NOW + 5 * DAY + 1)) is None
    assert history.find_application(replace(job, id='2', url=job.url.replace('abc', 'def'))) is None
    assert history.find_application(replace(job, url=job.url.replace('example', 'other'))) is None


def test_qualified_requisition_matches_changed_slug_but_not_workday_site(job):
    original = replace(job, url='https://example.wd1.myworkdayjobs.com/en-US/External/job/US/Engineer_R123')
    history.record_application(original, NOW)
    assert history.find_application(replace(original, id='2', url=original.url.replace('Engineer_', 'Developer_')))
    assert history.find_application(replace(original, url=original.url.replace('External', 'Internal'))) is None


def test_bootstrap_only_confirmed_and_idempotent(job):
    db.upsert_jobs([job])
    with db.connect() as c:
        for status in ('submitted', 'dry_run_ok', 'failed'):
            c.execute('INSERT INTO applications(job_id,status,created_at,submitted_at) VALUES (?,?,?,?)',
                      (job.id, status, NOW, NOW))
    assert history.initialize_from_records() == 1
    assert history.initialize_from_records() == 0
    assert history.find_application(job)['source'] == 'records'


def test_receipt_atomic_and_snapshot_survives_job_refresh(job):
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            history._init(c)
            assert c.in_transaction
            history.record_receipt(c, job, 11, NOW)
            raise RuntimeError('crash')
    assert history.find_application(job) is None
    with db.connect() as c:
        assert history.record_receipt(c, job, 11, NOW) == 1
        assert history.record_receipt(c, replace(job, url='https://other.test/jobs/new'), 11, NOW) == 0
    assert history.find_application(job)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as c:
            c.execute('DELETE FROM application_history')


def test_cached_email_exact_urls_legacy_and_enrichment(job):
    prior_apps.init_db()
    with db.connect() as c:
        columns = {r[1] for r in c.execute('PRAGMA table_info(email_confirmations)')}
        if 'job_urls' not in columns:
            c.execute("ALTER TABLE email_confirmations ADD COLUMN job_urls TEXT DEFAULT '[]'")
        c.execute('INSERT INTO email_confirmations(message_id,company,title,received_at,job_urls) VALUES (?,?,?,?,?)',
                  ('m1', job.company, job.title, NOW, '[]'))
    assert history.initialize_from_cached_emails() == {'imported': 1, 'linked': 0, 'unlinked': 1}
    assert history.find_application(job) is None
    with db.connect() as c:
        c.execute('UPDATE email_confirmations SET job_urls=?', (json.dumps([job.url]),))
    assert history.initialize_from_cached_emails() == {'imported': 1, 'linked': 1, 'unlinked': 0}
    assert history.initialize_from_cached_emails()['imported'] == 0
    assert history.find_application(job)['source'] == 'email'


@pytest.mark.parametrize('url', ['https://example.test', 'https://example.test/careers',
                                     'https://example.test/jobs/', 'https://example.test/en-us/search',
                                     'https://jobs.lever.co/example',
                                     'https://jobs.ashbyhq.com/example',
                                     'https://example.wd1.myworkdayjobs.com/en-US/External'])
def test_listing_urls_never_identify_application(job, url):
    history.record_application(replace(job, url=url), NOW)
    assert history.find_application(replace(job, url=url)) is None


def test_email_sync_only_explicit_and_propagates_failure(monkeypatch):
    calls = []
    def sync(**kwargs):
        calls.append(kwargs)
        return 3
    monkeypatch.setattr(prior_apps, 'sync_email_confirmations', sync)
    assert history.initialize_from_cached_emails()['imported'] == 0
    assert not calls
    assert history.initialize_from_emails()['synced'] == 3
    assert calls == [{'max_results': None, 'raise_errors': True}]
