from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

import pytest

from agent import db, report, runner, scheduler
from agent.ats.base import Answer, ApplyContext, Field, blocked_result, submit_decision
from agent.models import ApplyResult, ApplyStatus, Job, normalize_url
from agent.filters import policy_fingerprint


_FILTERS = {
    "terms": ["Summer 2027"], "us_only": True,
    "software_categories": ["Software"], "ai_categories": [],
    "ai_title_keywords": [], "exclude_title_keywords": [],
}


def _test_config():
    return {"filters": _FILTERS}


def _attest_job(job, config, **changes):
    evidence = {
        "policy_fingerprint": policy_fingerprint(config), "posting_id": job.id,
        "normalized_url": normalize_url(job.url), "url": job.url, "source": job.source,
        "company": job.company, "title": job.title, "category": job.category,
        "terms": ["Summer 2027"], "locations": list(job.locations), "location_context_us": False,
    }
    evidence.update(changes)
    db.kv_set(f"filter_policy:{job.id}", policy_fingerprint(config))
    db.kv_set(f"filter_evidence:{job.id}", json.dumps(evidence, sort_keys=True))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(report, "LOG_DIR", tmp_path)
    db.init()
    return tmp_path


class FakePage:
    def screenshot(self, **kw):
        pass


class FakeBrowser:
    pages = [FakePage()]


@contextmanager
def fake_browser(config, visible=False, profile_suffix=""):
    yield FakeBrowser()


class ScriptedAdapter:
    name = "fake"

    def __init__(self, results, delay=0.0):
        self.results = results
        self.calls = []
        self.delay = delay
        self._lock = threading.Lock()

    def can_handle(self, job):
        return True

    def apply(self, job, ctx):
        with self._lock:
            self.calls.append((job.id, ctx.dry_run, ctx.submit_mode))
        time.sleep(self.delay)
        r = self.results[job.id]
        if isinstance(r, Exception):
            raise r
        return r


def _jobs(n, ats="greenhouse"):
    return [Job(id=f"t:{i}", company=f"Co{i}", title=f"Software Engineer Intern {i}", url=f"https://x.com/{i}",
                locations=["NYC"], category="Software", date_posted=1000 - i, source="test", ats=ats)
            for i in range(n)]


def _run(monkeypatch, results, delay=0.0, **kw):
    adapter = ScriptedAdapter(results, delay)
    monkeypatch.setattr(runner, "adapter_for", lambda job, auto: adapter)
    monkeypatch.setattr(runner, "open_browser", fake_browser)
    monkeypatch.setattr(runner, "new_page", lambda b: FakePage())
    attest = kw.pop("attest", True)
    config = {"auto_ats": ["greenhouse"], "seconds_between_applications": 0, "max_submissions_per_run": 25,
              "max_submissions_per_night": 120, "submit_mode": "full", "filters": _FILTERS,
              **kw.pop("config", {})}
    if attest:
        with db.connect() as c:
            job_ids = [r[0] for r in c.execute("SELECT id FROM jobs")]
        for job_id in job_ids:
            job = db.get_job(job_id)
            if job:
                _attest_job(job, config)
    return runner.apply_batch(config, {}, **kw), adapter


def test_statuses_and_records(tmp_db, monkeypatch):
    jobs = _jobs(3)
    db.upsert_jobs(jobs)
    stats, _ = _run(monkeypatch, {
        "t:0": ApplyResult(ApplyStatus.SUBMITTED, "ok"),
        "t:1": ApplyResult(ApplyStatus.NEEDS_REVIEW, "unanswered required: x"),
        "t:2": ApplyResult(ApplyStatus.FAILED, "timeout"),
    })
    assert (stats.submitted, stats.queued, stats.failed) == (1, 1, 1)
    counts = db.status_counts()
    assert counts == {"submitted": 1, "needs_review": 1, "new": 1}  # failed once -> retried later
    assert db.submissions_since(0) == 1


def test_failed_twice_becomes_failed(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(1))
    res = {"t:0": ApplyResult(ApplyStatus.FAILED, "timeout")}
    _run(monkeypatch, res)
    _run(monkeypatch, res)
    assert db.status_counts() == {"failed": 1}
    stats, adapter = _run(monkeypatch, res)
    assert adapter.calls == []


def test_dry_run_keeps_job_queued_and_does_not_count_attempts(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(1))
    stats, adapter = _run(monkeypatch, {"t:0": ApplyResult(ApplyStatus.DRY_RUN_OK, "dry run")}, dry_run=True)
    assert adapter.calls == [("t:0", True, "full")]
    assert stats.dry_ok == 1
    assert db.status_counts() == {"new": 1}
    assert runner._attempts("t:0") == 0


def test_dry_run_restores_previous_status(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(1))
    db.set_job_status("t:0", "needs_review")
    _run(monkeypatch, {"t:0": ApplyResult(ApplyStatus.DRY_RUN_OK, "dry run")}, dry_run=True, job_ids=["t:0"])
    assert db.get_job_status("t:0") == "needs_review"


def test_job_flag_skips_submitted_unless_forced(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(1))
    db.set_job_status("t:0", "submitted")
    res = {"t:0": ApplyResult(ApplyStatus.DRY_RUN_OK, "dry run")}
    _, adapter = _run(monkeypatch, res, dry_run=True, job_ids=["t:0"])
    assert adapter.calls == []
    _, adapter = _run(monkeypatch, res, dry_run=True, job_ids=["t:0"], force=True)
    assert len(adapter.calls) == 1
    assert db.get_job_status("t:0") == "submitted"


def test_browser_crashes_exhaust_attempts(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(1))
    res = {"t:0": RuntimeError("Target page, context or browser has been closed")}
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    _run(monkeypatch, res)
    assert db.get_job_status("t:0") == "new"
    _run(monkeypatch, res)
    assert db.get_job_status("t:0") == "failed"


def test_job_watchdog_kills_hung_browser(tmp_db, monkeypatch):
    jobs = _jobs(2)
    db.upsert_jobs(jobs)
    for job in jobs:
        _attest_job(job, _test_config())
    killed = threading.Event()

    class Hangs:
        name = "hangs"

        def apply(self, job, ctx):
            if job.id == "t:0":
                killed.wait(10)
                raise RuntimeError("Target page, context or browser has been closed")
            return ApplyResult(ApplyStatus.DRY_RUN_OK, "dry run")

    monkeypatch.setattr(runner, "kill_browser", lambda user_dir: killed.set())
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    adapter = Hangs()
    monkeypatch.setattr(runner, "adapter_for", lambda job, auto: adapter)
    monkeypatch.setattr(runner, "open_browser", fake_browser)
    monkeypatch.setattr(runner, "new_page", lambda b: FakePage())
    t0 = time.time()
    stats = runner.apply_batch({"auto_ats": [], "seconds_between_applications": 0, "filters": _FILTERS,
                                "schedule": {"job_timeout": "1s"}},
                               {}, dry_run=True)
    assert time.time() - t0 < 8
    assert killed.is_set()
    assert "job timeout" in db.latest_application("t:0")["reason"]
    assert stats.failed == 1 and stats.dry_ok == 1


def test_overlapping_batches_are_serialized(tmp_db, monkeypatch):
    import fcntl
    db.upsert_jobs(_jobs(1))
    with open(db.DB_PATH.with_name("apply.lock"), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        stats, adapter = _run(monkeypatch, {"t:0": ApplyResult(ApplyStatus.SUBMITTED, "ok")})
    assert adapter.calls == [] and stats.run_id == 0
    _, adapter = _run(monkeypatch, {"t:0": ApplyResult(ApplyStatus.SUBMITTED, "ok")})
    assert len(adapter.calls) == 1


def test_stuck_job_with_submitted_record_stays_submitted(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    db.record_application(job, ApplyResult(ApplyStatus.SUBMITTED, "ok"), 1)
    db.set_job_status(job.id, db.IN_PROGRESS)
    db.reset_stuck_jobs(2)
    assert db.get_job_status(job.id) == "submitted"


def test_reset_stuck_job_uses_current_run_intent_and_keeps_journal(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    _attest_job(job, _test_config())
    crashed_run = db.start_run("apply")
    db.mark_job_in_progress(job.id, crashed_run)
    db.record_submit_intent(job.id, crashed_run, _test_config())

    recovery_run = db.start_run("apply")
    db.reset_stuck_jobs(5, current_run_id=recovery_run)

    assert db.get_job_status(job.id) == "needs_review"
    assert db.submit_intent_exists(job.id, crashed_run)
    assert db.uncertain_submissions_since(0) == 1
    db.end_run(recovery_run)


def test_claim_compare_and_set_preserves_fetch_hold_and_run_cap(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(2))
    actual_claim = db.preflight_and_claim_job

    def fetch_races_claim(job_id, run_id, expected_status, config, *, claim=True, allowed_statuses=None,
                          allow_newly_submitted=False, disallow_submitted=False):
        if job_id == "t:0":
            db.set_job_status(job_id, "needs_review")
        return actual_claim(job_id, run_id, expected_status, config, claim=claim,
                            allowed_statuses=allowed_statuses,
                            allow_newly_submitted=allow_newly_submitted,
                            disallow_submitted=disallow_submitted)

    monkeypatch.setattr(db, "preflight_and_claim_job", fetch_races_claim)
    results = {f"t:{i}": ApplyResult(ApplyStatus.SUBMITTED, "ok") for i in range(2)}
    stats, adapter = _run(monkeypatch, results, job_ids=["t:0", "t:1"],
                          config={"max_submissions_per_run": 1})

    assert [job_id for job_id, _, _ in adapter.calls] == ["t:1"]
    assert stats.skipped == 1 and stats.submitted == 1
    assert db.get_job_status("t:0") == "needs_review"
    assert runner._attempts("t:0") == 0
    assert db.get_job_status("t:1") == "submitted"


def test_automatic_queue_skips_job_that_became_submitted_after_selection(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(2))
    actual_claim = db.preflight_and_claim_job

    def mark_submitted_before_claim(job_id, run_id, expected_status, config, **kwargs):
        if job_id == "t:0":
            db.set_job_status(job_id, "submitted")
        return actual_claim(job_id, run_id, expected_status, config, **kwargs)

    monkeypatch.setattr(db, "preflight_and_claim_job", mark_submitted_before_claim)
    results = {f"t:{i}": ApplyResult(ApplyStatus.SUBMITTED, "ok") for i in range(2)}
    stats, adapter = _run(monkeypatch, results, config={"max_submissions_per_run": 1})

    assert [call[0] for call in adapter.calls] == ["t:1"]
    assert stats.skipped == 1 and stats.submitted == 1
    assert db.get_job_status("t:0") == "submitted"
    assert runner._attempts("t:0") == 0


@pytest.mark.parametrize("force, should_apply", [(False, False), (True, True)])
def test_explicit_target_rechecks_submitted_status_against_force(tmp_db, monkeypatch, force, should_apply):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    actual_claim = db.preflight_and_claim_job

    def mark_submitted_before_claim(job_id, run_id, expected_status, config, **kwargs):
        db.set_job_status(job_id, "submitted")
        return actual_claim(job_id, run_id, expected_status, config, **kwargs)

    monkeypatch.setattr(db, "preflight_and_claim_job", mark_submitted_before_claim)
    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.FAILED, "pre-click failure")},
                          job_ids=[job.id], force=force)

    assert bool(adapter.calls) is should_apply
    assert db.get_job_status(job.id) == "submitted"
    if should_apply:
        assert stats.failed == 1
        assert runner._attempts(job.id) == 1
    else:
        assert stats.skipped == 1
        assert runner._attempts(job.id) == 0


def test_explicit_submitted_snapshot_cannot_be_smuggled_through_two_status_changes(tmp_db, monkeypatch):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    db.set_job_status(job.id, "submitted")
    actual_claim = db.preflight_and_claim_job
    original_status_reader = db.get_job_status
    selection_read = True

    def racing_status_read(job_id):
        nonlocal selection_read
        if selection_read:
            selection_read = False
            return "new"  # Selection's submitted check races with a temporary requeue.
        return original_status_reader(job_id)

    def status_flaps_before_claim(job_id, run_id, expected_status, config, **kwargs):
        db.set_job_status(job_id, "new")
        db.set_job_status(job_id, "submitted")
        return actual_claim(job_id, run_id, expected_status, config, **kwargs)

    monkeypatch.setattr(db, "get_job_status", racing_status_read)
    monkeypatch.setattr(db, "preflight_and_claim_job", status_flaps_before_claim)
    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.FAILED, "must not run")},
                          job_ids=[job.id], force=False)

    assert adapter.calls == []
    assert stats.skipped == 1
    assert db.get_job_status(job.id) == "submitted"
    assert runner._attempts(job.id) == 0


def test_legacy_queued_job_without_attestation_is_held_not_discarded(tmp_db, monkeypatch):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.SUBMITTED, "must not run")},
                          attest=False)

    assert adapter.calls == []
    assert stats.queued == 1
    assert db.get_job(job.id) is not None
    assert db.get_job_status(job.id) == "needs_review"
    assert runner._attempts(job.id) == 0
    assert "missing source filter" in db.latest_application(job.id)["reason"]


@pytest.mark.parametrize("case, expected_reason", [
    ("missing", "missing source filter"),
    ("stale_kv", "fingerprint"),
    ("stale_evidence", "fingerprint"),
    ("wrong_posting", "queued posting URL"),
    ("same_id_other_url", "queued posting URL"),
    ("unknown_term", "term unknown"),
])
def test_forced_target_requires_current_matching_source_attestation(tmp_db, monkeypatch, case, expected_reason):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    if case != "missing":
        _attest_job(job, config)
    if case == "stale_kv":
        db.kv_set(f"filter_policy:{job.id}", "old-policy")
    elif case == "stale_evidence":
        _attest_job(job, config, policy_fingerprint="old-policy")
    elif case == "wrong_posting":
        _attest_job(job, config, posting_id="other:posting", url="https://other.test/job",
                    normalized_url=normalize_url("https://other.test/job"))
    elif case == "same_id_other_url":
        _attest_job(job, config, url="https://other.test/job",
                    normalized_url=normalize_url("https://other.test/job"))
    elif case == "unknown_term":
        _attest_job(job, config, terms=[])

    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.SUBMITTED, "must not run")},
                          attest=False, config=config, job_ids=[job.id], force=True)

    assert adapter.calls == []
    assert stats.queued == 1
    assert db.get_job_status(job.id) == "needs_review"
    assert runner._attempts(job.id) == 0
    assert expected_reason in db.latest_application(job.id)["reason"]
    assert not db.submit_intent_exists(job.id, stats.run_id)


def test_empty_category_is_valid_when_title_filtering_accepts(tmp_db, monkeypatch):
    job = _jobs(1)[0]
    job.category = ""
    db.upsert_jobs([job])
    _attest_job(job, _test_config())

    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.SUBMITTED, "ok")},
                          attest=False, config=_test_config())

    assert [call[0] for call in adapter.calls] == [job.id]
    assert stats.submitted == 1


@pytest.mark.parametrize("previous_status", ["new", "submitted", "needs_review"])
def test_dry_run_scope_rejection_preserves_queue_state_and_history(tmp_db, monkeypatch, previous_status):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    old_application = None
    if previous_status == "submitted":
        old_application = db.record_application(job, ApplyResult(ApplyStatus.SUBMITTED, "historical"), None)
    elif previous_status == "needs_review":
        old_application = db.record_application(job, ApplyResult(ApplyStatus.NEEDS_REVIEW, "unrelated reason"), None)
    db.set_job_status(job.id, previous_status)

    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.DRY_RUN_OK, "should not reach adapter")},
                          attest=False, dry_run=True, job_ids=[job.id], force=True)

    assert adapter.calls == []
    assert stats.queued == 1
    assert db.get_job_status(job.id) == previous_status
    assert runner._attempts(job.id) == 0
    latest = db.latest_application(job.id)
    assert (latest["id"] if latest else None) == old_application
    assert db.kv_get(f"filter_review_reason:{job.id}") is None


def test_forced_scope_block_preserves_submitted_status_and_application_history(tmp_db, monkeypatch):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    old_application = db.record_application(job, ApplyResult(ApplyStatus.SUBMITTED, "historical"), None)
    db.set_job_status(job.id, "submitted")
    _attest_job(job, _test_config(), terms=[])

    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.SUBMITTED, "must not run")},
                          attest=False, job_ids=[job.id], force=True)

    assert adapter.calls == []
    assert stats.queued == 1
    assert db.get_job_status(job.id) == "submitted"
    assert db.submit_intent_exists(job.id, stats.run_id) is False
    assert db.kv_get(f"filter_review_reason:{job.id}") is None
    latest = db.latest_application(job.id)
    assert latest["id"] != old_application
    assert latest["reason"].startswith("scope preflight blocked: source filter:")
    with db.connect() as c:
        assert c.execute("SELECT reason FROM applications WHERE id=?", (old_application,)).fetchone()[0] == "historical"


def test_final_click_scope_recheck_preserves_forced_submitted_status(tmp_db, monkeypatch):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    db.record_application(job, ApplyResult(ApplyStatus.SUBMITTED, "historical"), None)
    db.set_job_status(job.id, "submitted")
    config = {"auto_ats": ["greenhouse"], "filters": _FILTERS, "seconds_between_applications": 0,
              "max_submissions_per_run": 5, "max_submissions_per_night": 20, "submit_mode": "full"}
    _attest_job(job, config)

    class LateScopeChange:
        name = "late-scope-change"

        def apply(self, job, ctx):
            db.kv_set(f"filter_policy:{job.id}", "changed-after-claim")
            with pytest.raises(db.ScopeEvidenceRejected):
                ctx.mark_submit_attempt(job.id)
            return ApplyResult(ApplyStatus.FAILED, "adapter saw blocked intent")

    monkeypatch.setattr(runner, "adapter_for", lambda job, auto: LateScopeChange())
    monkeypatch.setattr(runner, "open_browser", fake_browser)
    monkeypatch.setattr(runner, "new_page", lambda browser: FakePage())
    stats = runner.apply_batch(config, {}, job_ids=[job.id], force=True)

    assert stats.queued == 1
    assert db.get_job_status(job.id) == "submitted"
    assert not db.submit_intent_exists(job.id, stats.run_id)
    assert db.latest_application(job.id)["reason"].startswith("final submit blocked: source filter:")


def test_scope_block_does_not_replace_unrelated_review_reason_or_mark_it_auto_clearable(tmp_db, monkeypatch):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    old_application = db.record_application(job, ApplyResult(ApplyStatus.NEEDS_REVIEW, "captcha requires review"), None)
    db.set_job_status(job.id, "needs_review")
    _attest_job(job, _test_config(), terms=[])

    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.SUBMITTED, "must not run")},
                          attest=False, job_ids=[job.id])

    assert adapter.calls == []
    assert stats.queued == 1
    assert db.get_job_status(job.id) == "needs_review"
    assert db.kv_get(f"filter_review_reason:{job.id}") is None
    with db.connect() as c:
        assert c.execute("SELECT reason FROM applications WHERE id=?", (old_application,)).fetchone()[0] \
            == "captcha requires review"
    assert db.latest_application(job.id)["reason"].startswith("scope preflight blocked: source filter:")


def test_submit_intent_requires_non_dry_claimed_run(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    _attest_job(job, config)
    run_id = db.start_run("apply")
    ctx = ApplyContext(FakePage(), None, config, {}, "full", run_id=run_id)

    with pytest.raises(RuntimeError, match="not active in apply run"):
        ctx.mark_submit_attempt(job.id)
    assert not db.submit_intent_exists(job.id, run_id)

    assert db.mark_job_in_progress(job.id, run_id, previous_status="new")
    ctx.mark_submit_attempt(job.id)
    assert db.submit_intent_exists(job.id, run_id)
    db.end_run(run_id)


def test_submit_intent_revalidates_scope_evidence_at_final_click(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    _attest_job(job, config)
    run_id = db.start_run("apply")
    assert db.mark_job_in_progress(job.id, run_id, previous_status="new")
    db.kv_set(f"filter_policy:{job.id}", "changed-after-claim")
    ctx = ApplyContext(FakePage(), None, config, {}, "full", run_id=run_id)

    with pytest.raises(db.ScopeEvidenceRejected, match="fingerprint"):
        ctx.mark_submit_attempt(job.id)

    assert ctx.scope_block_reason and "source filter" in ctx.scope_block_reason
    assert not db.submit_intent_exists(job.id, run_id)
    db.end_run(run_id)


def test_recovery_uses_current_intent_and_restores_pre_attempt_status(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    _attest_job(job, config)
    prior_run = db.start_run("apply")
    db.mark_job_in_progress(job.id, prior_run)
    db.record_submit_intent(job.id, prior_run, config)
    db.set_job_status(job.id, "new")
    db.end_run(prior_run)

    crashed_run = db.start_run("apply")
    db.mark_job_in_progress(job.id, crashed_run, previous_status="new")
    recovery_run = db.start_run("apply")
    db.reset_stuck_jobs(5, current_run_id=recovery_run)

    assert db.get_job_status(job.id) == "new"
    assert job.id not in {j.id for j in db.next_jobs(10, max_attempts=5)}
    assert db.has_uncertain_submit_intent(job.id)
    assert db.submit_intent_exists(job.id, prior_run)
    assert db.resolve_submit_intents(job.id, "not_submitted") == 1
    assert db.get_job_status(job.id) == "new"
    assert not db.has_uncertain_submit_intent(job.id)
    assert db.submit_intent_exists(job.id, prior_run)
    assert job.id in {j.id for j in db.next_jobs(10, max_attempts=5)}
    assert db.uncertain_submissions_since(0) == 1
    db.end_run(recovery_run)


def test_resolving_submit_as_submitted_moves_reservation_to_submitted_count(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    _attest_job(job, config)
    run_id = db.start_run("apply")
    assert db.mark_job_in_progress(job.id, run_id, previous_status="new")
    db.record_submit_intent(job.id, run_id, config)
    db.set_job_status(job.id, "needs_review")
    db.end_run(run_id)

    assert db.resolve_submit_intents(job.id, "submitted") == 1

    assert db.get_job_status(job.id) == "submitted"
    assert not db.has_uncertain_submit_intent(job.id)
    assert db.submit_intent_exists(job.id, run_id)
    assert db.submissions_since(0) == 1
    assert db.uncertain_submissions_since(0) == 0


def test_resolve_submitted_records_each_intent_once_at_original_click_time(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    _attest_job(job, config)
    run_ids, click_times = [], []
    for index in range(2):
        run_id = db.start_run("apply")
        assert db.mark_job_in_progress(job.id, run_id, previous_status="new")
        db.record_submit_intent(job.id, run_id, config)
        click_time = time.time() - (120 - index * 30)
        with db.connect() as c:
            c.execute("UPDATE submit_intents SET created_at=? WHERE job_id=? AND run_id=?",
                      (click_time, job.id, run_id))
        run_ids.append(run_id)
        click_times.append(click_time)
        db.set_job_status(job.id, "needs_review" if index else "new")
        db.end_run(run_id)

    since = min(click_times) - 1
    before = db.submissions_since(since) + db.uncertain_submissions_since(since)
    assert before == 2
    confirmed_at = time.time()

    assert db.resolve_submit_intents(job.id, "submitted") == 2

    assert db.get_job_status(job.id) == "submitted"
    assert db.submit_intent_exists(job.id, run_ids[0]) and db.submit_intent_exists(job.id, run_ids[1])
    assert db.submissions_since(since) == 2
    assert db.uncertain_submissions_since(since) == 0
    assert db.submissions_since(since) + db.uncertain_submissions_since(since) == before
    with db.connect() as c:
        rows = c.execute(
            "SELECT run_id, created_at, submitted_at, reason FROM applications "
            "WHERE job_id=? AND status='submitted' ORDER BY run_id", (job.id,),
        ).fetchall()
        resolutions = c.execute(
            "SELECT run_id, outcome FROM submit_intent_resolutions WHERE job_id=? ORDER BY run_id", (job.id,),
        ).fetchall()
    assert [row["run_id"] for row in rows] == sorted(run_ids)
    assert [row["created_at"] for row in rows] == sorted(click_times)
    assert all(row["submitted_at"] >= confirmed_at for row in rows)
    assert all("confirmed by user" in row["reason"] for row in rows)
    assert [(row["run_id"], row["outcome"]) for row in resolutions] == [
        (run_id, "submitted") for run_id in sorted(run_ids)
    ]
    report_path, summary = report.build(since=since)
    report_text = report_path.read_text()
    assert summary.startswith("2 submitted, 0 need review")
    assert report_text.count("User-confirmed after apply run") == 2
    with pytest.raises(LookupError, match="no unresolved submit intents"):
        db.resolve_submit_intents(job.id, "submitted")
    assert db.submissions_since(since) == 2


def test_force_does_not_bypass_unresolved_intent_and_retry_can_fail_before_click(tmp_db, monkeypatch):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    _attest_job(job, config)
    prior_run = db.start_run("apply")
    db.mark_job_in_progress(job.id, prior_run)
    db.record_submit_intent(job.id, prior_run, config)
    db.set_job_status(job.id, "submitted")
    db.end_run(prior_run)
    monkeypatch.setattr(runner, "MAX_ATTEMPTS", 5)

    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.FAILED, "pre-click failure")},
                          job_ids=[job.id], force=True)
    assert adapter.calls == []
    assert stats.queued == 1
    assert db.get_job_status(job.id) == "needs_review"
    assert "unresolved prior submit intent" in db.latest_application(job.id)["reason"]

    db.resolve_submit_intents(job.id, "not_submitted")
    assert db.get_job_status(job.id) == "new"
    assert db.submit_intent_exists(job.id, prior_run)
    assert db.uncertain_submissions_since(0) == 1
    stats, adapter = _run(monkeypatch, {job.id: ApplyResult(ApplyStatus.FAILED, "pre-click failure")},
                          job_ids=[job.id], force=True)
    assert len(adapter.calls) == 1
    assert stats.failed == 1
    assert db.latest_application(job.id)["status"] == ApplyStatus.FAILED.value
    assert db.get_job_status(job.id) == "new"


def test_resolved_prior_intent_does_not_guard_new_failure(tmp_db):
    job = _jobs(1)[0]
    db.upsert_jobs([job])
    config = _test_config()
    _attest_job(job, config)
    prior_run = db.start_run("apply")
    db.mark_job_in_progress(job.id, prior_run)
    db.record_submit_intent(job.id, prior_run, config)
    db.record_application(job, ApplyResult(ApplyStatus.SUBMITTED, "confirmed"), prior_run)
    db.set_job_status(job.id, "submitted")
    db.end_run(prior_run)

    current_run = db.start_run("apply")
    db.mark_job_in_progress(job.id, current_run)
    failure = ApplyResult(ApplyStatus.FAILED, "failed before submit")

    guarded = runner._guard_uncertain_submit(job, failure, False, current_run)
    assert guarded.status == ApplyStatus.FAILED
    assert runner._job_status_for(guarded, job, False, "submitted") == "submitted"
    db.set_job_status(job.id, "submitted")
    db.end_run(current_run)


def test_reset_stuck_jobs_fails_exhausted(tmp_db):
    db.upsert_jobs(_jobs(2))
    for _ in range(2):
        db.set_job_status("t:0", db.IN_PROGRESS, bump_attempt=True)
    db.set_job_status("t:1", db.IN_PROGRESS, bump_attempt=True)
    db.reset_stuck_jobs(2)
    assert db.get_job_status("t:0") == "failed"
    assert db.get_job_status("t:1") == "new"


def test_run_cap_stops_after_limit(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(5))
    results = {f"t:{i}": ApplyResult(ApplyStatus.SUBMITTED, "ok") for i in range(5)}
    stats, adapter = _run(monkeypatch, results, config={"max_submissions_per_run": 2})
    assert stats.submitted == 2 and len(adapter.calls) == 2


def test_cap_holds_with_parallel_workers(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(6))
    results = {f"t:{i}": ApplyResult(ApplyStatus.SUBMITTED, "ok") for i in range(6)}
    stats, adapter = _run(monkeypatch, results, delay=0.05,
                          config={"max_submissions_per_run": 1, "schedule": {"concurrency": 3}})
    assert stats.submitted == 1 and len(adapter.calls) == 1


def test_nightly_cap_already_reached(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(2))
    _run(monkeypatch, {"t:0": ApplyResult(ApplyStatus.SUBMITTED, "ok"),
                       "t:1": ApplyResult(ApplyStatus.SUBMITTED, "ok")}, config={"max_submissions_per_night": 1})
    assert db.submissions_since(0) == 1
    assert db.status_counts().get("new") == 1


def test_adapter_crash_is_recorded_not_raised(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(1))
    stats, _ = _run(monkeypatch, {"t:0": ValueError("boom")})
    assert stats.failed == 1
    assert "adapter crash" in db.latest_application("t:0")["reason"]


def test_report_lists_sections(tmp_db, monkeypatch):
    db.upsert_jobs(_jobs(2))
    _run(monkeypatch, {"t:0": ApplyResult(ApplyStatus.SUBMITTED, "ok"),
                       "t:1": ApplyResult(ApplyStatus.NEEDS_REVIEW, "captcha: unsupported")})
    path, summary = report.build(since=0)
    text = path.read_text()
    assert "1 submitted, 1 need review" in summary
    assert "## Submitted (1)" in text and "Co0" in text and "captcha" in text


def test_submit_decision_modes():
    fields = [Field("a", "Name", "text", True), Field("b", "Why us", "textarea", True)]
    answers = [Answer("a", "Ryan", "profile"), Answer("b", "Because", "generated")]
    assert submit_decision(fields, answers, "full", False) == (True, "ok")
    assert not submit_decision(fields, answers, "hybrid", False)[0]
    assert not submit_decision(fields, answers, "fill_only", False)[0]
    assert not submit_decision(fields, answers, "full", True)[0]
    assert not submit_decision(fields, [answers[0], Answer("b", None, "unknown")], "full", False)[0]


def test_invalid_submit_mode_fails_closed_during_dry_run():
    fields = [Field("a", "Name", "text", True)]
    answers = [Answer("a", "Ryan", "profile")]
    ok, why = submit_decision(fields, answers, "fulll", True)
    assert not ok and "unsupported submit mode" in why
    assert blocked_result(fields, answers, why, True, None, None).status == ApplyStatus.NEEDS_REVIEW
    assert not submit_decision(fields, answers, ["full"], True)[0]


def test_hybrid_dry_run_with_generated_answers_needs_review():
    fields = [Field("a", "Name", "text", True), Field("b", "Why us", "textarea", True)]
    answers = [Answer("a", "Ryan", "profile"), Answer("b", "Because", "generated")]
    _, why = submit_decision(fields, answers, "hybrid", True)
    assert blocked_result(fields, answers, why, True, None, None).status == ApplyStatus.NEEDS_REVIEW
    _, why = submit_decision(fields, answers, "full", True)
    assert blocked_result(fields, answers, why, True, None, None).status == ApplyStatus.DRY_RUN_OK


def test_scheduler_helpers(tmp_db):
    assert scheduler._in_active_hours("00:00-23:59")
    assert scheduler._in_active_hours("22:00-08:00") == (time.strftime("%H:%M") >= "22:00" or time.strftime("%H:%M") <= "08:00")
    assert scheduler._due("never_run", "30m")
    db.kv_set("just_ran", str(time.time()))
    assert not scheduler._due("just_ran", "30m")


def test_hung_apply_subprocess_is_killed(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = f"import subprocess,time; p=subprocess.Popen(['sleep','60']); open(r'{marker}','w').write(str(p.pid)); time.sleep(60)"
    t0 = time.time()
    scheduler._run_apply_subprocess(1, cmd=[scheduler.sys.executable, "-c", script])
    assert time.time() - t0 < 15
    pid = int(marker.read_text())
    time.sleep(0.2)
    try:
        import os
        os.kill(pid, 0)
        with open(f"/proc/{pid}/status") as f:
            assert any(line.startswith("State:") and "Z" in line for line in f)
    except (ProcessLookupError, FileNotFoundError):
        pass


def test_apply_once_records_last_apply_even_on_error(tmp_db, monkeypatch):
    def boom(timeout):
        raise RuntimeError("spawn failed")
    monkeypatch.setattr(scheduler, "_run_apply_subprocess", boom)
    with pytest.raises(RuntimeError):
        scheduler.apply_once({})
    assert not scheduler._due("last_apply", "30m")
