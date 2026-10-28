from __future__ import annotations

import fcntl
import logging
import queue
import threading
import time
from dataclasses import dataclass, field, replace

from . import db
from .ats.base import ApplyContext
from .ats.registry import adapter_for
from .browser import kill_browser, new_page, open_browser, profile_dir
from .config import parse_duration
from .models import ApplyResult, ApplyStatus, Job

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
_BROWSER_DEAD = ("Target page, context or browser has been closed", "Browser has been closed",
                 "Connection closed", "browser has disconnected")


@dataclass
class BatchStats:
    run_id: int
    submitted: int = 0
    queued: int = 0
    failed: int = 0
    dry_ok: int = 0
    skipped: int = 0
    lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _job_status_for(result: ApplyResult, job: Job, dry_run: bool, prev: str = "new") -> str:
    if dry_run:
        return prev or "new"
    if (prev == "submitted" and result.status == ApplyStatus.NEEDS_REVIEW
            and result.reason.startswith("final submit blocked: source filter:")):
        return "submitted"
    if result.status == ApplyStatus.FAILED:
        if prev == "submitted":
            return "submitted"
        return "new" if _attempts(job.id) < MAX_ATTEMPTS else "failed"
    return result.status.value


def _attempts(job_id: str) -> int:
    with db.connect() as c:
        r = c.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        return r[0] if r else 0


def _guard_uncertain_submit(job: Job, result: ApplyResult, dry_run: bool, run_id: int) -> ApplyResult:
    if dry_run or result.status != ApplyStatus.FAILED:
        return result
    if db.submit_intent_exists(job.id, run_id):
        return replace(result, status=ApplyStatus.NEEDS_REVIEW,
                       reason=f"possible submission in run {run_id}; verify manually: {result.reason}"[:500])
    return result


def _consumed_submit_slot(job: Job, run_id: int, dry_run: bool) -> bool:
    return not dry_run and db.submit_intent_exists(job.id, run_id)


def _run_one(job: Job, ctx_browser, config: dict, profile: dict, submit_mode: str, dry_run: bool,
             run_id: int) -> ApplyResult:
    adapter = adapter_for(job, config.get("auto_ats", []))
    page = new_page(ctx_browser)
    ctx = ApplyContext(page=page, browser=ctx_browser, config=config, profile=profile,
                       submit_mode=submit_mode, dry_run=dry_run, run_id=run_id)
    if adapter is None:
        reason = f"no adapter enabled for ats={job.ats}"
        try:
            page.goto(job.url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            reason += f"; page load failed: {type(e).__name__}: {str(e)[:180]}"
            log.info("could not load unsupported ATS page %s: %s", job.url, e)
        return ApplyResult(ApplyStatus.NEEDS_REVIEW, reason, screenshot=ctx.screenshot(job, "no_adapter"),
                           final_url=getattr(page, "url", None))
    log.info("applying [%s] %s - %s (%s)", adapter.name, job.company, job.title, job.url)
    try:
        result = adapter.apply(job, ctx)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if any(s in msg for s in _BROWSER_DEAD):
            raise
        log.exception("adapter crashed on %s", job.url)
        result = ApplyResult(ApplyStatus.FAILED, f"adapter crash: {msg[:300]}",
                             screenshot=ctx.screenshot(job, "crash"))
    if ctx.scope_block_reason:
        return ApplyResult(ApplyStatus.NEEDS_REVIEW, f"final submit blocked: {ctx.scope_block_reason}"[:500],
                           screenshot=result.screenshot, final_url=result.final_url,
                           fields_total=result.fields_total, unanswered_required=result.unanswered_required,
                           generated_answers=result.generated_answers, answers=result.answers)
    return result


def _worker(wid: int, jobs: "queue.Queue[Job]", stats: BatchStats, config: dict, profile: dict,
            submit_mode: str, dry_run: bool, cap: list[int], visible: bool) -> None:
    gap = float(config.get("seconds_between_applications", 20))
    suffix = "" if wid == 0 else f"-w{wid}"
    job_timeout = parse_duration(str(config.get("schedule", {}).get("job_timeout", "15m")))
    user_dir = profile_dir(config, suffix)
    restarts = 0
    while not jobs.empty() and restarts <= 3:
        restarts += 1
        try:
            with open_browser(config, visible=visible, profile_suffix=suffix) as browser:
                while True:
                    with stats.lock:
                        if cap[0] <= 0:
                            return
                        try:
                            job = jobs.get_nowait()
                        except queue.Empty:
                            return
                        cap[0] -= 1  # reserve a slot so parallel workers can't overshoot the cap
                    prev = getattr(job, "_runner_expected_status", None)
                    if prev is None:
                        prev = getattr(job, "_db_status", None) or db.get_job_status(job.id) or "new"
                    claim_status, claim_reason, prev = db.preflight_and_claim_job(
                        job.id, stats.run_id, prev, config, claim=not dry_run,
                        allowed_statuses=None if getattr(job, "_runner_explicit", False)
                        else {"new", "failed_retry"},
                        allow_newly_submitted=bool(getattr(job, "_runner_explicit", False)
                                                   and getattr(job, "_runner_force", False)),
                        disallow_submitted=bool(getattr(job, "_runner_explicit", False)
                                                and not getattr(job, "_runner_force", False)),
                    )
                    if claim_status not in {"claimed", "ready"}:
                        with stats.lock:
                            cap[0] += 1
                            if claim_status == "needs_review":
                                stats.queued += 1
                                line = f"needs_review {job.company} - {job.title}: {claim_reason[:120]}"
                            else:
                                stats.skipped += 1
                                line = f"skipped      {job.company} - {job.title}: {claim_reason[:120]}"
                            stats.lines.append(line)
                            log.info(line)
                        continue
                    timed_out = threading.Event()

                    def _expire(job=job):
                        timed_out.set()
                        log.error("job %s exceeded %ds; killing its browser", job.id, job_timeout)
                        kill_browser(user_dir)

                    watchdog = threading.Timer(job_timeout, _expire)
                    watchdog.daemon = True
                    watchdog.start()
                    try:
                        result = _run_one(job, browser, config, profile, submit_mode, dry_run, stats.run_id)
                    except Exception as e:
                        why = f"job timeout ({job_timeout}s)" if timed_out.is_set() else f"browser died: {e}"
                        crash = _guard_uncertain_submit(job, ApplyResult(ApplyStatus.FAILED, why[:300]),
                                                        dry_run, stats.run_id)
                        db.record_application(job, crash, stats.run_id)
                        db.set_job_status(job.id, _job_status_for(crash, job, dry_run, prev))
                        with stats.lock:
                            if not _consumed_submit_slot(job, stats.run_id, dry_run):
                                cap[0] += 1
                            if crash.status == ApplyStatus.NEEDS_REVIEW:
                                stats.queued += 1
                            else:
                                stats.failed += 1
                            stats.lines.append(f"{crash.status.value:12} {job.company} - {job.title}: {crash.reason[:120]}")
                        log.warning("browser died (%s); restarting", why[:200])
                        raise
                    finally:
                        watchdog.cancel()
                    result = _guard_uncertain_submit(job, result, dry_run, stats.run_id)
                    db.record_application(job, result, stats.run_id)
                    db.set_job_status(job.id, _job_status_for(result, job, dry_run, prev))
                    with stats.lock:
                        if result.status != ApplyStatus.SUBMITTED and not _consumed_submit_slot(job, stats.run_id, dry_run):
                            cap[0] += 1
                        line = f"{result.status.value:12} {job.company} - {job.title}: {result.reason[:120]}"
                        stats.lines.append(line)
                        log.info(line)
                        if result.status == ApplyStatus.SUBMITTED:
                            stats.submitted += 1
                        elif result.status == ApplyStatus.NEEDS_REVIEW:
                            stats.queued += 1
                        elif result.status == ApplyStatus.DRY_RUN_OK:
                            stats.dry_ok += 1
                        elif result.status == ApplyStatus.SKIPPED:
                            stats.skipped += 1
                        else:
                            stats.failed += 1
                    if timed_out.is_set():
                        raise RuntimeError("browser killed by job watchdog")
                    if not jobs.empty():
                        time.sleep(gap)
        except Exception as e:
            log.error("worker %d browser error: %s", wid, e)
            time.sleep(5)


def apply_batch(config: dict, profile: dict, *, limit: int | None = None, dry_run: bool = False,
                submit_mode: str | None = None, job_ids: list[str] | None = None,
                ats: list[str] | None = None, visible: bool = False, force: bool = False) -> BatchStats:
    """One apply batch. Batches are serialized with a file lock: they share the browser profile, and
    reset_stuck_jobs is only safe when no other batch has jobs in flight."""
    lock_path = db.DB_PATH.with_name("apply.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("another apply run is in progress; skipping this one")
            return BatchStats(run_id=0)
        return _apply_batch_locked(config, profile, limit=limit, dry_run=dry_run, submit_mode=submit_mode,
                                   job_ids=job_ids, ats=ats, visible=visible, force=force)


def _apply_batch_locked(config: dict, profile: dict, *, limit: int | None, dry_run: bool, submit_mode: str | None,
                        job_ids: list[str] | None, ats: list[str] | None, visible: bool, force: bool) -> BatchStats:
    submit_mode = submit_mode or config.get("submit_mode", "hybrid")
    since = db.night_start()
    night_left = (int(config.get("max_submissions_per_night", 120))
                  - db.submissions_since(since) - db.uncertain_submissions_since(since))
    per_run = int(config.get("max_submissions_per_run", 25))
    cap = [max(0, min(night_left, per_run)) if not dry_run else 10**6]
    run_id = db.start_run("apply-dry" if dry_run else "apply")
    stats = BatchStats(run_id=run_id)
    db.reset_stuck_jobs(MAX_ATTEMPTS, current_run_id=run_id)
    if cap[0] <= 0:
        log.info("nightly submission cap reached; nothing to do")
        db.end_run(run_id, note="cap reached")
        return stats

    if job_ids:
        jobs = [j for j in (db.get_job(i) for i in job_ids) if j]
        if not force:
            done = [j for j in jobs if db.get_job_status(j.id) == "submitted"]
            for j in done:
                log.info("skipping %s (%s): already submitted; pass --force to re-apply", j.id, j.company)
            jobs = [j for j in jobs if j not in done]
    else:
        jobs = db.next_jobs(limit or per_run * 3, ats_allowed=ats, max_attempts=MAX_ATTEMPTS)
    if limit:
        jobs = jobs[:limit]
    for job in jobs:
        job._runner_expected_status = getattr(job, "_db_status", None) or db.get_job_status(job.id) or "new"
        job._runner_explicit = bool(job_ids)
        job._runner_force = bool(force)
    log.info("run %d: %d jobs, mode=%s dry_run=%s cap=%s", run_id, len(jobs), submit_mode, dry_run, cap[0])

    q: "queue.Queue[Job]" = queue.Queue()
    for j in jobs:
        q.put(j)
    n = max(1, min(int(config.get("schedule", {}).get("concurrency", 1)), 3, len(jobs) or 1))
    threads = [threading.Thread(target=_worker, args=(i, q, stats, config, profile, submit_mode, dry_run, cap, visible),
                                daemon=True, name=f"apply-w{i}") for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.end_run(run_id, submitted=stats.submitted, queued=stats.queued, failed=stats.failed)
    return stats
