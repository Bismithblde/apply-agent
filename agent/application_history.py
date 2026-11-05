"""Append-only application evidence. Title/company similarity is never proof.

Call initialize_from_emails only from the explicit email initialization mode.
Normal discovery can import cached evidence without contacting Gmail.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from urllib.parse import parse_qs, urlparse

from . import db, prior_apps
from .models import Job, normalize_url



def _init(connection=None) -> None:
    """Create schema on an optional caller connection without committing it."""
    if connection is not None:
        _schema(connection)
        return
    with db.connect() as c:
        _schema(c)


def _schema(c) -> None:
    schema = """
            CREATE TABLE IF NOT EXISTS application_history (
                id INTEGER PRIMARY KEY,
                evidence_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                submitted_at REAL NOT NULL,
                date_posted REAL NOT NULL DEFAULT 0,
                identities TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT ''
            );
            CREATE TRIGGER IF NOT EXISTS application_history_no_update
            BEFORE UPDATE ON application_history BEGIN
                SELECT RAISE(ABORT, 'application history is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS application_history_no_delete
            BEFORE DELETE ON application_history BEGIN
                SELECT RAISE(ABORT, 'application history is append-only');
            END;
        """
    # Triggers contain semicolons; sqlite's parser identifies complete statements.
    import sqlite3
    statement = ''
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            c.execute(statement)
            statement = ''


def _url_identities(url: str) -> set[str]:
    p = urlparse(url.strip())
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        return set()
    host = p.hostname.lower()
    path = [part for part in p.path.split('/') if part]
    query = parse_qs(p.query)
    # Listing pages and homepages cannot prove which position was applied to.
    listing = {'jobs', 'careers', 'career', 'positions', 'openings', 'search', 'en', 'en-us'}
    identity_keys = {'gh_jid', 'ashby_jid', 'requisitionId', 'requisition_id', 'jobId', 'job_id'}
    if not (identity_keys & query.keys()) and (not path or path[-1].lower() in listing):
        return set()
    identities = {'url:' + normalize_url(url)}
    tenant = host
    posting = None
    # Shared ATS hosts require their path tenant, not merely the ATS hostname.
    if host in ('boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards.eu.greenhouse.io'):
        tenant = 'greenhouse:' + path[0] if path else ''
        if len(path) >= 3 and path[-2] == 'jobs':
            posting = path[-1]
    elif host in ('jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com', 'jobs.smartrecruiters.com'):
        tenant = host + ':' + path[0] if path else ''
        if len(path) >= 2:
            posting = path[1]
    elif host.endswith('.myworkdayjobs.com'):
        site = next((part for part in path if not re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', part, re.I)), '')
        tenant = host + '/' + site
        # Workday sites on the same host can have separate requisitions.
        if 'job' in path and path:
            match = re.search(r'_([^_/]+)$', path[-1])
            posting = match.group(1) if match else None
    else:
        for key in ('gh_jid', 'ashby_jid', 'requisitionId', 'requisition_id', 'jobId', 'job_id'):
            if query.get(key):
                posting = query[key][0]
                break
    if host in ('boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards.eu.greenhouse.io',
                'jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com', 'jobs.smartrecruiters.com') and not posting:
        return set()
    if host.endswith('.myworkdayjobs.com') and not posting:
        return set()
    if posting and tenant:
        identities.add('posting:' + tenant + ':' + posting)
    return identities


def _identities(job: Job) -> set[str]:
    identities = _url_identities(job.url)
    p = urlparse(job.url)
    # Source IDs are qualified by the full tenant path on shared domains. They
    # are intentionally redundant when the source uses a URL hash as its ID.
    if identities and job.id and job.source:
        host = (p.hostname or '').lower()
        shared = host in ('boards.greenhouse.io', 'job-boards.greenhouse.io',
                          'boards.eu.greenhouse.io', 'jobs.lever.co', 'jobs.eu.lever.co',
                          'jobs.ashbyhq.com', 'jobs.smartrecruiters.com')
        parts = [x for x in p.path.split('/') if x]
        tenant = host + ('/' + parts[0] if shared and parts else '')
        if host.endswith('.myworkdayjobs.com'):
            tenant += '/' + next((part for part in parts if not re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', part, re.I)), '')
        if not shared or parts:
            identities.add('source:' + json.dumps([job.source, tenant, job.id]))
    return identities


def _append(*, evidence_key: str, source: str, submitted_at: float,
            identities: set[str], date_posted: float = 0,
            company: str = '', title: str = '') -> int:
    if not math.isfinite(submitted_at) or submitted_at <= 0:
        raise ValueError('submitted_at must be a positive finite Unix timestamp')
    with db.connect() as c:
        return c.execute(
            'INSERT OR IGNORE INTO application_history '
            '(evidence_key,source,submitted_at,date_posted,identities,company,title) VALUES (?,?,?,?,?,?,?)',
            (evidence_key, source, submitted_at, date_posted,
             json.dumps(sorted(identities)), company, title),
        ).rowcount


def record_application(job: Job, submitted_at: float, source: str = 'agent') -> None:
    """Record a confirmed submission. Repeated identical receipts are idempotent."""
    _init()
    identities = _identities(job)
    evidence = json.dumps([source, sorted(identities), float(submitted_at)], sort_keys=True)
    _append(evidence_key='receipt:' + hashlib.sha256(evidence.encode()).hexdigest(),
            source=source, submitted_at=float(submitted_at), identities=identities,
            date_posted=job.date_posted or 0, company=job.company, title=job.title)


def find_application(job: Job) -> dict | None:
    """Return newest exact evidence, excluding a known later posting cycle.

    Unknown posting dates retain exact identity matches; titles never match.
    """
    _init()
    identities = _identities(job)
    if not identities:
        return None
    with db.connect() as c:
        rows = c.execute('SELECT * FROM application_history ORDER BY submitted_at DESC,id DESC').fetchall()
    for row in rows:
        if job.date_posted and job.date_posted > row['submitted_at']:
            continue
        overlap = identities.intersection(json.loads(row['identities']))
        if overlap:
            result = dict(row)
            result['identities'] = json.loads(row['identities'])
            result['matched_identity'] = sorted(overlap)[0]
            return result
    return None


def initialize_from_records() -> int:
    """Import confirmed local receipts, ignoring every non-submitted status."""
    _init()
    with db.connect() as c:
        rows = c.execute(
            "SELECT j.*, a.id AS application_id, COALESCE(a.submitted_at,a.created_at) AS receipt_at "
            "FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='submitted'"
        ).fetchall()
    imported = 0
    for row in rows:
        job = Job(**{key: row[key] for key in Job.__dataclass_fields__})
        imported += _append(evidence_key='application:' + str(row['application_id']),
                            source='records', submitted_at=row['receipt_at'],
                            identities=_identities(job), date_posted=job.date_posted or 0,
                            company=job.company, title=job.title)
    return imported


def initialize_from_cached_emails() -> dict:
    """Import cached exact URLs; legacy and ambiguous emails stay unlinked."""
    _init()
    prior_apps.init_db()
    with db.connect() as c:
        rows = c.execute('SELECT * FROM email_confirmations').fetchall()
    counts = {'imported': 0, 'linked': 0, 'unlinked': 0}
    for row in rows:
        identities: set[str] = set()
        if 'job_urls' in row.keys():
            try:
                urls = json.loads(row['job_urls'] or '[]')
            except (ValueError, TypeError):
                urls = []
            if isinstance(urls, list):
                for url in urls:
                    if isinstance(url, str):
                        identities.update(_url_identities(url))
        digest = hashlib.sha256(json.dumps(sorted(identities)).encode()).hexdigest()
        added = _append(evidence_key='email:' + row['message_id'] + ':' + digest, source='email',
                        submitted_at=row['received_at'], identities=identities,
                        company=row['company'], title=row['title'] or '')
        counts['imported'] += added
        counts['linked' if identities else 'unlinked'] += added
    return counts


def initialize_from_emails(max_results: int | None = None) -> dict:
    """Explicit email initialization only. Sync failures propagate to the caller."""
    try:
        synced = prior_apps.sync_email_confirmations(max_results=max_results, raise_errors=True)
    except Exception:
        initialize_from_cached_emails()
        raise
    return {'synced': synced, **initialize_from_cached_emails()}


def record_receipt(connection, job: Job, application_id: int, submitted_at: float) -> int:
    """Snapshot a live receipt atomically in the caller's transaction; never commits."""
    _init(connection)
    if not math.isfinite(submitted_at) or submitted_at <= 0:
        raise ValueError('submitted_at must be a positive finite Unix timestamp')
    return connection.execute(
        'INSERT OR IGNORE INTO application_history '
        '(evidence_key,source,submitted_at,date_posted,identities,company,title) VALUES (?,?,?,?,?,?,?)',
        ('application:' + str(application_id), 'records', submitted_at, job.date_posted or 0,
         json.dumps(sorted(_identities(job))), job.company, job.title),
    ).rowcount
