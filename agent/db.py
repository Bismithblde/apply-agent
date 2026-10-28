"""SQLite persistence. Every function opens its own short-lived connection, so it is safe to call
from multiple threads/processes (WAL mode)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterator

from .config import DB_PATH
from .models import ApplyResult, ApplyStatus, Job, dedupe_key, normalize_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    norm_url TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    ats TEXT NOT NULL,
    locations TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT '',
    date_posted INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL,
    active_run_id INTEGER,
    active_run_previous_status TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_norm_url ON jobs(norm_url);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_id INTEGER,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    screenshot TEXT,
    final_url TEXT,
    answers_json TEXT NOT NULL DEFAULT '[]',
    generated_json TEXT NOT NULL DEFAULT '[]',
    unanswered_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    submitted_at REAL
);
CREATE INDEX IF NOT EXISTS applications_job ON applications(job_id);

CREATE TABLE IF NOT EXISTS accounts (
    tenant_key TEXT PRIMARY KEY,
    company TEXT NOT NULL DEFAULT '',
    login_url TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    password_enc TEXT NOT NULL,
    created_at REAL NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mail_seen (
    message_id TEXT PRIMARY KEY,
    consumed_at REAL NOT NULL,
    purpose TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    fetched INTEGER NOT NULL DEFAULT 0,
    new_jobs INTEGER NOT NULL DEFAULT 0,
    submitted INTEGER NOT NULL DEFAULT 0,
    queued INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS captcha_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    cost REAL NOT NULL,
    ok INTEGER NOT NULL,
    job_id TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submit_intents (
    job_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, run_id)
);
CREATE INDEX IF NOT EXISTS submit_intents_time ON submit_intents(created_at);

CREATE TABLE IF NOT EXISTS submit_intent_resolutions (
    job_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'not_submitted')),
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, run_id)
);
"""

# Job.status values
NEW, IN_PROGRESS = "new", "in_progress"
TERMINAL_STATUSES = {s.value for s in ApplyStatus}
RESOLVED_SUBMIT_INTENT_REASON = "submission confirmed by user while resolving submit intent"


class ScopeEvidenceRejected(RuntimeError):
    """The source attestation no longer proves that this queued posting is in scope."""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        columns = {row[1] for row in c.execute("PRAGMA table_info(jobs)")}
        if "active_run_id" not in columns:
            c.execute("ALTER TABLE jobs ADD COLUMN active_run_id INTEGER")
        if "active_run_previous_status" not in columns:
            c.execute("ALTER TABLE jobs ADD COLUMN active_run_previous_status TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_active_run ON jobs(active_run_id, status)")
        # Preserve intent markers from databases created before the per-run journal.
        c.execute(
            "INSERT OR IGNORE INTO submit_intents (job_id, run_id, created_at) "
            "SELECT substr(k.key, length('submit_intent:') + 1), r.id, r.started_at "
            "FROM kv k JOIN runs r ON k.value=CAST(r.id AS TEXT) "
            "WHERE k.key LIKE 'submit_intent:%' AND r.kind='apply'"
        )


def _row_to_job(r: sqlite3.Row) -> Job:
    job = Job(
        id=r["id"], company=r["company"], title=r["title"], url=r["url"],
        locations=json.loads(r["locations"]), category=r["category"],
        date_posted=r["date_posted"], source=r["source"], ats=r["ats"],
    )
    if "status" in r.keys():
        job._db_status = r["status"]
    return job


# ---------- jobs ----------

def upsert_jobs(jobs: list[Job]) -> int:
    """Insert jobs not seen before (by normalized URL or company+title). Returns number inserted."""
    inserted = 0
    now = time.time()
    with connect() as c:
        for j in jobs:
            nu, dk = normalize_url(j.url), dedupe_key(j.company, j.title)
            if c.execute("SELECT 1 FROM jobs WHERE norm_url=? OR dedupe_key=? OR id=?", (nu, dk, j.id)).fetchone():
                continue
            c.execute(
                "INSERT INTO jobs (id, source, company, title, url, norm_url, dedupe_key, ats, locations,"
                " category, date_posted, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (j.id, j.source, j.company, j.title, j.url, nu, dk, j.ats,
                 json.dumps(j.locations), j.category, j.date_posted, now),
            )
            inserted += 1
    return inserted


def next_jobs(limit: int, ats_allowed: list[str] | None = None, max_attempts: int = 2) -> list[Job]:
    """Jobs still to apply to, newest posting first."""
    sql = (
        "SELECT * FROM jobs WHERE status IN ('new','failed_retry') AND attempts < ? "
        "AND NOT EXISTS (SELECT 1 FROM submit_intents s JOIN runs r ON s.run_id=r.id "
        "WHERE s.job_id=jobs.id AND r.kind='apply' "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=s.job_id "
        "AND a.run_id=s.run_id AND a.status='submitted') "
        "AND NOT EXISTS (SELECT 1 FROM submit_intent_resolutions x "
        "WHERE x.job_id=s.job_id AND x.run_id=s.run_id))"
    )
    args: list[Any] = [max_attempts]
    if ats_allowed is not None:
        sql += f" AND ats IN ({','.join('?' * len(ats_allowed))})"
        args += ats_allowed
    sql += " ORDER BY date_posted DESC, first_seen DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [_row_to_job(r) for r in c.execute(sql, args)]


def get_job(job_id: str) -> Job | None:
    with connect() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(r) if r else None


def set_job_status(job_id: str, status: str, bump_attempt: bool = False) -> None:
    with connect() as c:
        if bump_attempt:
            c.execute("UPDATE jobs SET status=?, attempts=attempts+1, last_attempt=?, active_run_id=NULL, "
                      "active_run_previous_status=NULL WHERE id=?",
                      (status, time.time(), job_id))
        else:
            c.execute("UPDATE jobs SET status=?, active_run_id=NULL, active_run_previous_status=NULL WHERE id=?",
                      (status, job_id))


def mark_job_in_progress(job_id: str, run_id: int, previous_status: str | None = None) -> bool:
    """Claim a job only if its status still matches the status observed by the caller."""
    with connect() as c:
        active = c.execute("SELECT 1 FROM runs WHERE id=? AND kind='apply' AND ended_at IS NULL",
                           (run_id,)).fetchone()
        if active is None:
            raise RuntimeError(f"apply run {run_id} is not active")
        if previous_status is None:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            previous_status = row["status"]
        if previous_status == IN_PROGRESS:
            return False
        changed = c.execute(
            "UPDATE jobs SET status=?, attempts=attempts+1, last_attempt=?, active_run_id=?, "
            "active_run_previous_status=? WHERE id=? AND status=?",
            (IN_PROGRESS, time.time(), run_id, previous_status, job_id, previous_status),
        ).rowcount
        return changed == 1


def _filter_evidence_reason(c: sqlite3.Connection, job: sqlite3.Row, config: dict) -> str | None:
    """Validate the persisted source evidence against the current filter policy."""
    from .filters import keep, policy_fingerprint

    try:
        current_fingerprint = policy_fingerprint(config)
    except Exception as e:
        return f"current filter policy could not be fingerprinted: {type(e).__name__}: {e}"

    policy_row = c.execute("SELECT value FROM kv WHERE key=?", (f"filter_policy:{job['id']}",)).fetchone()
    evidence_row = c.execute("SELECT value FROM kv WHERE key=?", (f"filter_evidence:{job['id']}",)).fetchone()
    if policy_row is None:
        return "missing source filter policy attestation"
    if evidence_row is None:
        return "missing source filter evidence"
    try:
        evidence = json.loads(evidence_row["value"])
    except (TypeError, json.JSONDecodeError) as e:
        return f"malformed source filter evidence: {type(e).__name__}"
    if not isinstance(evidence, dict):
        return "malformed source filter evidence: expected an object"

    fingerprint = evidence.get("policy_fingerprint")
    if not all(isinstance(v, str) and v for v in (policy_row["value"], fingerprint)):
        return "missing source filter policy fingerprint"
    if policy_row["value"] != fingerprint or fingerprint != current_fingerprint:
        return "source filter policy fingerprint is stale or mismatched"

    required_strings = ("posting_id", "normalized_url", "url", "source", "company", "title")
    missing = [key for key in required_strings if not isinstance(evidence.get(key), str) or not evidence[key]]
    if missing:
        return "source filter evidence has missing or invalid fields: " + ", ".join(missing)
    if not isinstance(evidence.get("category"), str):
        return "source filter evidence has invalid category"
    if not isinstance(evidence.get("terms"), list) or any(not isinstance(v, str) for v in evidence["terms"]):
        return "source filter evidence has invalid terms"
    if not isinstance(evidence.get("locations"), list) or any(
            not isinstance(v, str) for v in evidence["locations"]):
        return "source filter evidence has invalid locations"
    if type(evidence.get("location_context_us")) is not bool:
        return "source filter evidence has invalid US location context"
    try:
        evidence_url = normalize_url(evidence["url"])
    except Exception as e:
        return f"source filter evidence URL is invalid: {type(e).__name__}"
    if not evidence_url or evidence_url != evidence["normalized_url"]:
        return "source filter evidence normalized URL does not match its raw URL"
    if evidence["normalized_url"] != job["norm_url"]:
        return "source filter evidence destination differs from the queued posting URL"

    try:
        attested_job = Job(
            id=evidence["posting_id"], company=evidence["company"], title=evidence["title"],
            url=evidence["url"], locations=evidence["locations"], category=evidence["category"],
            date_posted=job["date_posted"], source=evidence["source"], ats=job["ats"],
        )
        attested_job.terms = evidence["terms"]
        attested_job.location_context_us = evidence["location_context_us"]
        accepted, reason = keep(attested_job, config)
    except Exception as e:
        return f"source filter evidence could not be revalidated: {type(e).__name__}: {e}"
    return None if accepted else f"current filters rejected source evidence: {reason}"


def _unresolved_submit_intent(c: sqlite3.Connection, job_id: str) -> bool:
    return c.execute(
        "SELECT 1 FROM submit_intents s JOIN runs r ON s.run_id=r.id WHERE s.job_id=? AND r.kind='apply' "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=s.job_id "
        "AND a.run_id=s.run_id AND a.status='submitted') AND NOT EXISTS "
        "(SELECT 1 FROM submit_intent_resolutions x WHERE x.job_id=s.job_id AND x.run_id=s.run_id) LIMIT 1",
        (job_id,),
    ).fetchone() is not None


def preflight_and_claim_job(job_id: str, run_id: int, expected_status: str, config: dict,
                            *, claim: bool = True, allowed_statuses: set[str] | None = None,
                            allow_newly_submitted: bool = False,
                            disallow_submitted: bool = False) -> tuple[str, str, str]:
    """Revalidate source scope and optionally claim, serialized with concurrent SQLite writers.

    Returns (outcome, reason, prior_status). A forced explicit target may follow a newly
    submitted status; automatic queue claims remain bound to their selected eligible status.
    """
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT kind FROM runs WHERE id=? AND ended_at IS NULL", (run_id,)).fetchone()
        allowed_kinds = {"apply"} if claim else {"apply", "apply-dry"}
        if run is None or run["kind"] not in allowed_kinds:
            raise RuntimeError(f"run {run_id} is not active for filter preflight")
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return "status_changed", "job no longer exists", expected_status
        if disallow_submitted and (expected_status == "submitted" or row["status"] == "submitted"):
            return "status_changed", "already submitted; --force is required to re-apply", row["status"]
        prior_status = expected_status
        if row["status"] != expected_status:
            if allow_newly_submitted and row["status"] == "submitted":
                prior_status = "submitted"
            else:
                return "status_changed", "status changed before claim", expected_status
        if prior_status == IN_PROGRESS or (allowed_statuses is not None and prior_status not in allowed_statuses):
            return "status_changed", "status is no longer eligible for the automatic queue", prior_status

        if _unresolved_submit_intent(c, job_id):
            reason = "unresolved prior submit intent; confirm its outcome before retrying"
            filter_reason = None
        else:
            filter_reason = _filter_evidence_reason(c, row, config)
            reason = f"source filter: {filter_reason}" if filter_reason else ""

        if reason:
            if not claim:
                return "needs_review", reason, prior_status
            latest = c.execute(
                "SELECT status, reason FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
            marker = c.execute("SELECT value FROM kv WHERE key=?", (f"filter_review_reason:{job_id}",)).fetchone()
            filter_only_hold = bool(
                latest and latest["status"] == "needs_review" and marker
                and latest["reason"] == f"source filter: {marker['value']}"
            )
            preserve_status = bool(filter_reason and (
                expected_status == "submitted"
                or (expected_status == "needs_review" and not filter_only_hold)
            ))
            visible_reason = (
                f"scope preflight blocked: {reason}"
                if filter_reason and preserve_status else reason
            )
            if not preserve_status:
                changed = c.execute(
                    "UPDATE jobs SET status='needs_review', active_run_id=NULL, active_run_previous_status=NULL "
                    "WHERE id=? AND status=?",
                    (job_id, prior_status),
                ).rowcount
                if not changed:
                    return "status_changed", "status changed before claim", prior_status
            if filter_reason and not preserve_status:
                c.execute(
                    "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"filter_review_reason:{job_id}", filter_reason),
                )
            if latest is None or latest["status"] != "needs_review" or latest["reason"] != visible_reason:
                c.execute(
                    "INSERT INTO applications (job_id, run_id, status, reason, screenshot, final_url, answers_json, "
                    "generated_json, unanswered_json, created_at, submitted_at) "
                    "VALUES (?, ?, 'needs_review', ?, NULL, NULL, '[]', '[]', '[]', ?, NULL)",
                    (job_id, run_id, visible_reason, time.time()),
                )
            return "needs_review", visible_reason, prior_status

        if not claim:
            return "ready", "", prior_status
        changed = c.execute(
            "UPDATE jobs SET status=?, attempts=attempts+1, last_attempt=?, active_run_id=?, "
            "active_run_previous_status=? WHERE id=? AND status=?",
            (IN_PROGRESS, time.time(), run_id, prior_status, job_id, prior_status),
        ).rowcount
        if changed != 1:
            return "status_changed", "status changed before claim", prior_status
        return "claimed", "", prior_status


def reset_stuck_jobs(max_attempts: int = 2, current_run_id: int | None = None) -> int:
    """Recover orphaned attempts, using only that attempt's submit intent."""
    now = time.time()
    with connect() as c:
        stuck = c.execute(
            "SELECT id, attempts, active_run_id, active_run_previous_status, last_attempt "
            "FROM jobs WHERE status=?",
            (IN_PROGRESS,),
        ).fetchall()
        requeued = 0
        for job in stuck:
            attempt_run_id = job["active_run_id"]
            if current_run_id is not None and attempt_run_id == current_run_id:
                continue

            if attempt_run_id is None and job["last_attempt"] is not None:
                query = "SELECT id FROM runs WHERE kind='apply' AND ended_at IS NULL AND started_at<=?"
                args: tuple[Any, ...] = (job["last_attempt"],)
                if current_run_id is not None:
                    query += " AND id<>?"
                    args += (current_run_id,)
                query += " ORDER BY started_at DESC, id DESC LIMIT 1"
                run = c.execute(query, args).fetchone()
                attempt_run_id = run[0] if run else None

            current_intent = bool(attempt_run_id is not None and c.execute(
                "SELECT 1 FROM submit_intents s WHERE s.job_id=? AND s.run_id=? "
                "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=s.job_id "
                "AND a.run_id=s.run_id AND a.status='submitted') "
                "AND NOT EXISTS (SELECT 1 FROM submit_intent_resolutions x "
                "WHERE x.job_id=s.job_id AND x.run_id=s.run_id) LIMIT 1",
                (job["id"], attempt_run_id),
            ).fetchone())
            submitted_this_attempt = bool(attempt_run_id is not None and c.execute(
                "SELECT 1 FROM applications WHERE job_id=? AND run_id=? AND status='submitted' LIMIT 1",
                (job["id"], attempt_run_id),
            ).fetchone())
            submitted_before = bool(c.execute(
                "SELECT 1 FROM applications WHERE job_id=? AND status='submitted' LIMIT 1",
                (job["id"],),
            ).fetchone())

            if submitted_this_attempt:
                status = "submitted"
            elif current_intent:
                status = "needs_review"
            else:
                previous_status = job["active_run_previous_status"]
                if previous_status == "submitted" or (previous_status is None and submitted_before):
                    status = "submitted"
                elif previous_status in {"new", "failed_retry"}:
                    status = "failed" if job["attempts"] >= max_attempts else previous_status
                elif previous_status is not None and previous_status != IN_PROGRESS:
                    status = previous_status
                elif job["attempts"] >= max_attempts:
                    status = "failed"
                else:
                    status = "new"

            changed = c.execute(
                "UPDATE jobs SET status=?, active_run_id=NULL, active_run_previous_status=NULL "
                "WHERE id=? AND status=?",
                (status, job["id"], IN_PROGRESS),
            ).rowcount
            if status == "new":
                requeued += changed

        c.execute("UPDATE jobs SET status='failed' WHERE status='new' AND attempts>=?", (max_attempts,))

        # The apply lock is held by the caller, so any other unended apply run is an orphan.
        if current_run_id is None:
            c.execute(
                "UPDATE runs SET ended_at=?, note=CASE WHEN note='' THEN 'recovered after interrupted apply' "
                "ELSE note END WHERE kind IN ('apply', 'apply-dry') AND ended_at IS NULL",
                (now,),
            )
        else:
            c.execute(
                "UPDATE runs SET ended_at=?, note=CASE WHEN note='' THEN 'recovered after interrupted apply' "
                "ELSE note END WHERE kind IN ('apply', 'apply-dry') AND ended_at IS NULL AND id<>?",
                (now, current_run_id),
            )
        return requeued


def get_job_status(job_id: str) -> str | None:
    with connect() as c:
        r = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return r[0] if r else None


def jobs_by_status(status: str) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM jobs WHERE status=? ORDER BY last_attempt DESC", (status,)).fetchall()


def status_counts() -> dict[str, int]:
    with connect() as c:
        return {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}


# ---------- applications ----------

def record_application(job: Job, result: ApplyResult, run_id: int | None) -> int:
    now = time.time()
    with connect() as c:
        cur = c.execute(
            "INSERT INTO applications (job_id, run_id, status, reason, screenshot, final_url, answers_json,"
            " generated_json, unanswered_json, created_at, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job.id, run_id, result.status.value, result.reason, result.screenshot, result.final_url,
             json.dumps([asdict(a) for a in result.answers], default=str),
             json.dumps(result.generated_answers), json.dumps(result.unanswered_required), now,
             now if result.status == ApplyStatus.SUBMITTED else None),
        )
        return int(cur.lastrowid)


def submissions_since(ts: float) -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM applications WHERE status='submitted' AND created_at>=?",
                         (ts,)).fetchone()[0]


def uncertain_submissions_since(ts: float) -> int:
    """Reserve a nightly slot for each unconfirmed final click, including forced retries."""
    with connect() as c:
        return int(c.execute(
            "SELECT COUNT(*) FROM submit_intents s JOIN runs r ON s.run_id=r.id "
            "WHERE s.created_at>=? AND r.kind='apply' "
            "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=s.job_id "
            "AND a.run_id=s.run_id AND a.status='submitted')",
            (ts,),
        ).fetchone()[0])


def applications_since(ts: float) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT a.*, j.company, j.title, j.url, j.ats FROM applications a JOIN jobs j ON j.id=a.job_id"
            " WHERE a.created_at>=? ORDER BY a.created_at", (ts,)).fetchall()


def latest_application(job_id: str) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                         (job_id,)).fetchone()


# ---------- runs ----------

def start_run(kind: str) -> int:
    with connect() as c:
        return int(c.execute("INSERT INTO runs (kind, started_at) VALUES (?,?)", (kind, time.time())).lastrowid)


def end_run(run_id: int, **counts: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in counts)
    with connect() as c:
        c.execute(f"UPDATE runs SET ended_at=?{', ' + cols if cols else ''} WHERE id=?",
                  (time.time(), *counts.values(), run_id))


# ---------- accounts (password_enc is produced by vault.py; never store plaintext) ----------

def get_account_row(tenant_key: str) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM accounts WHERE tenant_key=? COLLATE NOCASE", (tenant_key,)).fetchone()


def put_account_row(tenant_key: str, company: str, login_url: str, email: str, password_enc: str,
                    verified: bool = False, notes: str = "") -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO accounts (tenant_key, company, login_url, email, password_enc, created_at, verified, notes)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tenant_key) DO UPDATE SET company=excluded.company,"
            " login_url=excluded.login_url, email=excluded.email, password_enc=excluded.password_enc,"
            " verified=excluded.verified, notes=excluded.notes",
            (tenant_key, company, login_url, email, password_enc, time.time(), int(verified), notes),
        )


def set_account_verified(tenant_key: str, verified: bool = True) -> None:
    with connect() as c:
        c.execute("UPDATE accounts SET verified=? WHERE tenant_key=?", (int(verified), tenant_key))


def list_account_rows() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()


def accounts_created_since(ts: float) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM accounts WHERE created_at>=?", (ts,)).fetchall()


# ---------- mail ----------

def mail_is_seen(message_id: str) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM mail_seen WHERE message_id=?", (message_id,)).fetchone() is not None


def mark_mail_seen(message_id: str, purpose: str = "") -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO mail_seen (message_id, consumed_at, purpose) VALUES (?,?,?)",
                  (message_id, time.time(), purpose))


# ---------- captcha spend ----------

def add_captcha_spend(kind: str, cost: float, ok: bool, job_id: str | None = None) -> None:
    with connect() as c:
        c.execute("INSERT INTO captcha_spend (ts, kind, cost, ok, job_id) VALUES (?,?,?,?,?)",
                  (time.time(), kind, cost, int(ok), job_id))


def captcha_spend_since(ts: float) -> float:
    with connect() as c:
        return float(c.execute("SELECT COALESCE(SUM(cost),0) FROM captcha_spend WHERE ts>=?", (ts,)).fetchone()[0])


# ---------- kv (ETags, scheduler timestamps) ----------

def kv_get(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else default


def kv_set(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def record_submit_intent(job_id: str, run_id: int, config: dict) -> None:
    """Journal the final-submit intent only after revalidating current source evidence."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        active = c.execute("SELECT 1 FROM runs WHERE id=? AND kind='apply' AND ended_at IS NULL",
                           (run_id,)).fetchone()
        if active is None:
            raise RuntimeError(f"apply run {run_id} is not active")
        claimed = c.execute(
            "SELECT * FROM jobs WHERE id=? AND status=? AND active_run_id=?",
            (job_id, IN_PROGRESS, run_id),
        ).fetchone()
        if claimed is None:
            raise RuntimeError(f"job {job_id!r} is not active in apply run {run_id}")
        scope_reason = _filter_evidence_reason(c, claimed, config)
        if scope_reason:
            raise ScopeEvidenceRejected(f"source filter: {scope_reason}")
        c.execute("INSERT OR IGNORE INTO submit_intents (job_id, run_id, created_at) VALUES (?,?,?)",
                  (job_id, run_id, time.time()))
        c.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (f"submit_intent:{job_id}", str(run_id)))


def submit_intent_exists(job_id: str, run_id: int) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM submit_intents WHERE job_id=? AND run_id=?",
                         (job_id, run_id)).fetchone() is not None


def has_uncertain_submit_intent(job_id: str) -> bool:
    """Whether any run still has an unconfirmed submit intent for this job."""
    with connect() as c:
        return c.execute(
            "SELECT 1 FROM submit_intents s JOIN runs r ON s.run_id=r.id "
            "WHERE s.job_id=? AND r.kind='apply' AND NOT EXISTS ("
            "SELECT 1 FROM applications a WHERE a.job_id=s.job_id "
            "AND a.run_id=s.run_id AND a.status='submitted') AND NOT EXISTS ("
            "SELECT 1 FROM submit_intent_resolutions x "
            "WHERE x.job_id=s.job_id AND x.run_id=s.run_id) LIMIT 1",
            (job_id,),
        ).fetchone() is not None


def resolve_submit_intents(job_id: str, outcome: str) -> int:
    """Record a user-confirmed outcome while retaining every original intent for cap accounting."""
    if outcome not in ("submitted", "not_submitted"):
        raise ValueError("outcome must be 'submitted' or 'not_submitted'")
    now = time.time()
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        job = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise LookupError(f"job {job_id!r} does not exist")
        if job["status"] == IN_PROGRESS:
            raise RuntimeError(f"job {job_id!r} is active; resolve its outcome after recovery")
        intents = c.execute(
            "SELECT s.run_id, s.created_at FROM submit_intents s WHERE s.job_id=? "
            "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=s.job_id "
            "AND a.run_id=s.run_id AND a.status='submitted') "
            "AND NOT EXISTS (SELECT 1 FROM submit_intent_resolutions x "
            "WHERE x.job_id=s.job_id AND x.run_id=s.run_id)",
            (job_id,),
        ).fetchall()
        if not intents:
            raise LookupError(f"no unresolved submit intents exist for job {job_id!r}")
        for intent in intents:
            c.execute(
                "INSERT INTO submit_intent_resolutions (job_id, run_id, outcome, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(job_id, run_id) DO UPDATE SET "
                "outcome=excluded.outcome, created_at=excluded.created_at",
                (job_id, intent["run_id"], outcome, now),
            )
            if outcome == "submitted":
                c.execute(
                    "INSERT INTO applications (job_id, run_id, status, reason, screenshot, final_url, answers_json, "
                    "generated_json, unanswered_json, created_at, submitted_at) "
                    "VALUES (?, ?, 'submitted', ?, NULL, NULL, '[]', '[]', '[]', ?, ?)",
                    (job_id, intent["run_id"], RESOLVED_SUBMIT_INTENT_REASON,
                     intent["created_at"], now),
                )
        if outcome == "submitted":
            status = "submitted"
        else:
            has_submission = c.execute(
                "SELECT 1 FROM applications WHERE job_id=? AND status='submitted' LIMIT 1",
                (job_id,),
            ).fetchone()
            status = "submitted" if has_submission else NEW
        c.execute(
            "UPDATE jobs SET status=?, active_run_id=NULL, active_run_previous_status=NULL WHERE id=?",
            (status, job_id),
        )
        return len(intents)


def night_start(now: float | None = None) -> float:
    """Start of the current 'night' window: 6 PM local yesterday/today. Caps and spend reset at 6 PM."""
    now = now or time.time()
    lt = time.localtime(now)
    six_pm = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 18, 0, 0, 0, 0, -1))
    return six_pm if now >= six_pm else six_pm - 86400


init()
