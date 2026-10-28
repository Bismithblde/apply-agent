"""Runs every enabled source, filters, and stores new jobs. One failing source never stops the others."""
from __future__ import annotations

import fcntl
import json
import logging
import time
from typing import Any, Callable

from . import db
from .filters import keep, policy_fingerprint
from .models import Job, dedupe_key, normalize_url
from .sources import ats_boards, github_lists, simplify

log = logging.getLogger(__name__)
_FILTER_REVIEW_PREFIX = "source filter: "
_PENDING_STATUSES = {"new", "failed_retry"}

# Earlier sources win dedupe ties, so direct ATS board URLs are preferred over aggregator links.
SOURCES: list[tuple[str, Callable[[dict], list[Job]]]] = [
    ("ats_boards", ats_boards.fetch),
    ("simplify", simplify.fetch),
    ("speedyapply", github_lists.fetch_speedyapply),
    ("vanshb03", github_lists.fetch_vanshb03),
]


class FetchLockBusy(RuntimeError):
    """The apply or review process owns the shared lock; fetch should be retried."""


def _identity_keys(job: Job) -> set[str]:
    return {f"id:{job.id}", f"url:{normalize_url(job.url)}"}


def _queued_match(c, job: Job):
    norm_url = normalize_url(job.url)
    row = c.execute("SELECT id, status FROM jobs WHERE id=?", (job.id,)).fetchone()
    if row is None:
        row = c.execute("SELECT id, status FROM jobs WHERE norm_url=?", (norm_url,)).fetchone()
    return row


def _title_collision(c, job: Job):
    return c.execute(
        "SELECT id, url FROM jobs WHERE dedupe_key=? AND norm_url<>? LIMIT 1",
        (dedupe_key(job.company, job.title), normalize_url(job.url)),
    ).fetchone()


def _set_kv(c, key: str, value: str) -> None:
    c.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, value))


def _filter_evidence(job: Job, fingerprint: str) -> dict[str, Any]:
    terms = getattr(job, "terms", None) or []
    if isinstance(terms, str):
        terms = [terms]
    return {
        "policy_fingerprint": fingerprint,
        "posting_id": job.id,
        "normalized_url": normalize_url(job.url),
        "url": job.url,
        "source": job.source,
        "company": job.company,
        "title": job.title,
        "category": job.category,
        "terms": list(terms),
        "locations": list(job.locations),
        "location_context_us": bool(getattr(job, "location_context_us", False)),
    }


def _has_unresolved_submit_intent(c, job_id: str) -> bool:
    return c.execute(
        "SELECT 1 FROM submit_intents s JOIN runs r ON s.run_id=r.id WHERE s.job_id=? AND r.kind='apply' "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=s.job_id "
        "AND a.run_id=s.run_id AND a.status='submitted') AND NOT EXISTS "
        "(SELECT 1 FROM submit_intent_resolutions x WHERE x.job_id=s.job_id AND x.run_id=s.run_id) LIMIT 1",
        (job_id,),
    ).fetchone() is not None


def _record_filter_review(c, job_id: str, reason: str, transitioned: bool) -> tuple[bool, bool]:
    message = _FILTER_REVIEW_PREFIX + reason
    previous = c.execute("SELECT status, reason FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                         (job_id,)).fetchone()
    filter_only_hold = bool(previous and previous["status"] == "needs_review"
                            and previous["reason"].startswith(_FILTER_REVIEW_PREFIX))
    if not transitioned and not filter_only_hold:
        return False, False
    if previous and previous["status"] == "needs_review" and previous["reason"] == message:
        _set_kv(c, f"filter_review_reason:{job_id}", reason)
        return True, False
    c.execute(
        "INSERT INTO applications (job_id, run_id, status, reason, screenshot, final_url, answers_json, "
        "generated_json, unanswered_json, created_at, submitted_at) "
        "VALUES (?, NULL, 'needs_review', ?, NULL, NULL, '[]', '[]', '[]', ?, NULL)",
        (job_id, message, time.time()),
    )
    _set_kv(c, f"filter_review_reason:{job_id}", reason)
    return True, True


def _hold_for_review(jobs: list[tuple[Job, str]], verified_keys: set[str],
                     fingerprint: str) -> tuple[int, int, int, dict[str, str], int]:
    """Hold unresolved listings idempotently, without transferring evidence across postings."""
    if not jobs:
        return 0, 0, 0, {}, 0
    candidates = [(job, reason) for job, reason in jobs if not (_identity_keys(job) & verified_keys)]
    suppressed = len(jobs) - len(candidates)
    if not candidates:
        return 0, 0, suppressed, {}, 0
    inserted = db.upsert_jobs([job for job, _ in candidates])
    held = 0
    orphans = {}
    claim_races = 0
    for job, reason in candidates:
        with db.connect() as c:
            row = _queued_match(c, job)
            if row is None:
                collision = _title_collision(c, job)
                orphan = {**_filter_evidence(job, fingerprint), "reason": reason,
                          "collision_job_id": collision["id"] if collision else None,
                          "collision_url": collision["url"] if collision else None}
                _set_kv(c, f"filter_review_orphan:{job.id}", json.dumps(orphan, sort_keys=True))
                orphans[job.id] = reason
                log.warning("cannot place %s in review queue without matching its exact URL or ID%s",
                            job.id, f"; company/title dedupe collided with {collision['id']}" if collision else "")
                continue
            job_id, status = row["id"], row["status"]
            transitioned = False
            if status in _PENDING_STATUSES:
                changed = c.execute(
                    "UPDATE jobs SET status='needs_review' WHERE id=? AND status IN ('new', 'failed_retry')",
                    (job_id,),
                ).rowcount
                if changed:
                    status = "needs_review"
                    transitioned = True
                else:
                    current = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                    status = current["status"] if current else ""
            if status != "needs_review":
                if status == "in_progress":
                    claim_races += 1
                    log.warning("could not hold %s: the apply runner already claimed it; runner preflight is required",
                                job.id)
                continue
            if _has_unresolved_submit_intent(c, job_id):
                log.warning("did not replace the review reason for %s because its submit intent is unresolved", job.id)
                continue
            active, added = _record_filter_review(c, job_id, reason, transitioned)
            if active:
                held += 1
                log.info("held %s for review: %s%s", job.id, reason, "" if added else " (already recorded)")
            else:
                log.debug("did not replace existing review reason for %s", job.id)
    return inserted, held, suppressed, orphans, claim_races


def _mark_verified(job: Job, fingerprint: str) -> bool:
    """Persist evidence only to the identical ID or normalized URL already stored.

    Runner preflight reads ``filter_policy:<job_id>`` and ``filter_evidence:<job_id>``;
    the latter preserves source evidence that the jobs row cannot represent.
    """
    with db.connect() as c:
        row = _queued_match(c, job)
        evidence = _filter_evidence(job, fingerprint)
        if row is None:
            collision = _title_collision(c, job)
            evidence["collision_job_id"] = collision["id"] if collision else None
            evidence["collision_url"] = collision["url"] if collision else None
            _set_kv(c, f"filter_attestation_orphan:{job.id}", json.dumps(evidence, sort_keys=True))
            log.warning("cannot attest %s onto a different posting with the same company/title", job.id)
            return False
        job_id = row["id"]
        _set_kv(c, f"filter_policy:{job_id}", fingerprint)
        _set_kv(c, f"filter_evidence:{job_id}", json.dumps(evidence, sort_keys=True))
        marker_key = f"filter_review_reason:{job_id}"
        marker = c.execute("SELECT value FROM kv WHERE key=?", (marker_key,)).fetchone()
        if row["status"] != "needs_review" or marker is None or _has_unresolved_submit_intent(c, job_id):
            return True
        previous = c.execute("SELECT id, status, reason FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                             (job_id,)).fetchone()
        if not previous or previous["status"] != "needs_review" \
                or previous["reason"] != _FILTER_REVIEW_PREFIX + marker["value"]:
            return True
        changed = c.execute("UPDATE jobs SET status='new' WHERE id=? AND status='needs_review'", (job_id,)).rowcount
        if not changed:
            return True
        c.execute(
            "INSERT INTO applications (job_id, run_id, status, reason, screenshot, final_url, answers_json, "
            "generated_json, unanswered_json, created_at, submitted_at) "
            "VALUES (?, NULL, 'skipped', ?, NULL, NULL, '[]', '[]', '[]', ?, NULL)",
            (job_id, f"source filter hold cleared under policy {fingerprint[:12]}", time.time()),
        )
        _set_kv(c, marker_key, "verified")
        return True


def fetch_all(config: dict) -> dict[str, Any]:
    """Fetch and persist while holding the same lock as apply and review."""
    lock_path = db.DB_PATH.with_name("apply.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FetchLockBusy(f"fetch deferred: apply/review lock is busy ({lock_path})") from exc
        stats = _fetch_all_locked(config)
        db.kv_set("last_fetch", str(time.time()))
        return stats


def _fetch_all_locked(config: dict) -> dict[str, Any]:
    stats: dict[str, Any] = {"fetched": 0, "matched": 0, "new": 0, "held_for_review": 0,
                             "unrepresented_verified": 0, "claim_races": 0,
                             "by_source": {}, "errors": {}}
    fingerprint = policy_fingerprint(config)
    batches = []
    for name, fn in SOURCES:
        if not (config.get("sources", {}).get(name) or {}).get("enabled", False):
            continue
        t0 = time.time()
        entry: dict[str, Any] = {"fetched": 0, "matched": 0, "new": 0, "held_for_review": 0,
                                 "review_reasons": {}, "excluded": 0, "suppressed_by_verified": 0,
                                 "unrepresented_verified": 0, "claim_races": 0}
        try:
            jobs = fn(config)
            matched, held, excluded = [], [], {}
            for job in jobs:
                ok, reason = keep(job, config)
                if ok:
                    matched.append(job)
                elif "unknown" in reason:
                    held.append((job, reason))
                else:
                    excluded[reason] = excluded.get(reason, 0) + 1
            entry.update(fetched=len(jobs), matched=len(matched),
                         review_reasons={reason: sum(r == reason for _, r in held)
                                         for reason in {r for _, r in held}},
                         excluded=sum(excluded.values()), excluded_reasons=excluded)
            batches.append({"name": name, "entry": entry, "matched": matched, "held": held})
        except Exception as e:
            log.exception("source %s failed", name)
            entry["error"] = f"{type(e).__name__}: {e}"
            stats["errors"][name] = entry["error"]
        entry["seconds"] = round(time.time() - t0, 1)
        stats["by_source"][name] = entry

    verified_keys: set[str] = set()
    for batch in batches:
        entry, matched = batch["entry"], batch["matched"]
        try:
            auto_new = db.upsert_jobs(matched) if matched else 0
            for job in matched:
                if _mark_verified(job, fingerprint):
                    verified_keys.update(_identity_keys(job))
                else:
                    entry["unrepresented_verified"] = entry.get("unrepresented_verified", 0) + 1
                    stats["unrepresented_verified"] += 1
            entry["new"] += auto_new
        except Exception as e:
            log.exception("source %s matched listings could not be stored", batch["name"])
            entry["error"] = f"{type(e).__name__}: {e}"
            stats["errors"][batch["name"]] = entry["error"]

    for batch in batches:
        entry = batch["entry"]
        try:
            review_new, held_count, suppressed, orphans, claim_races = _hold_for_review(
                batch["held"], verified_keys, fingerprint)
            entry["new"] += review_new
            entry["held_for_review"] = held_count
            entry["suppressed_by_verified"] = suppressed
            entry["review_orphans"] = orphans
            stats["held_for_review"] += held_count
            entry["unrepresented_for_review"] = len(orphans)
            stats.setdefault("unrepresented_for_review", 0)
            stats["unrepresented_for_review"] += len(orphans)
            entry["claim_races"] = claim_races
            stats["claim_races"] += claim_races
        except Exception as e:
            log.exception("source %s review listings could not be stored", batch["name"])
            entry["error"] = f"{type(e).__name__}: {e}"
            stats["errors"][batch["name"]] = entry["error"]

    for entry in stats["by_source"].values():
        for k in ("fetched", "matched", "new"):
            stats[k] += entry[k]
    return stats
