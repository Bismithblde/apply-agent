"""Durable journal recovery uses a synthetic local SQLite database only."""
import pytest

from agent import db, fetch
from agent.models import Job


@pytest.fixture
def claimed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "flow.db")
    db.init()
    job = Job(id="flow", company="Fixture", title="Engineer",
              url="https://example.test/job", source="test", locations=[], category="", date_posted=0)
    db.upsert_jobs([job])
    run = db.start_run("apply")
    assert db.mark_job_in_progress(job.id, run)
    return job, run


@pytest.mark.parametrize("state", ["prepared", "advanced", "uncertain"])
def test_crash_never_requeues_or_force_claims_checkpoint(claimed, state):
    job, run = claimed
    action = db.begin_flow_action(job.id, run, "structure-before")
    if state != "prepared":
        db.finish_flow_action(action, state, "structure-after")
    recovery = db.start_run("apply")
    assert db.reset_stuck_jobs(99, current_run_id=recovery) == 0
    assert db.get_job_status(job.id) == "needs_review"
    assert db.has_uncertain_flow_action(job.id)
    # A forced status reset cannot bypass either queue selection or preflight.
    db.set_job_status(job.id, "new")
    assert not db.next_jobs(10)
    assert not db.mark_job_in_progress(job.id, recovery)
    outcome, reason, _ = db.preflight_and_claim_job(job.id, recovery, "new", {})
    assert outcome == "needs_review"
    assert "flow action" in reason
    assert db.get_job_status(job.id) == "needs_review"


def test_same_run_advances_then_confirms_all_actions_and_recovers_receipt(claimed):
    job, run = claimed
    first = db.begin_flow_action(job.id, run, "one")
    with pytest.raises(RuntimeError):
        db.begin_flow_action(job.id, run, "duplicate")
    db.finish_flow_action(first, "advanced", "two")
    final = db.begin_flow_action(job.id, run, "two")
    db.finish_flow_action(final, "confirmed", "thanks")
    assert not db.has_uncertain_flow_action(job.id)
    with db.connect() as c:
        assert {r[0] for r in c.execute("SELECT state FROM flow_actions")} == {"confirmed"}
        # Simulate legacy/interrupted writer with a confirmation but no receipt.
        c.execute("DELETE FROM applications")
    db.reset_stuck_jobs()
    assert db.get_job_status(job.id) == "submitted"
    assert db.latest_application(job.id)["status"] == "submitted"
    db.reset_stuck_jobs()
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM applications WHERE status='submitted'").fetchone()[0] == 1


@pytest.mark.parametrize("outcome", ["submitted", "not_submitted"])
@pytest.mark.parametrize("with_submit", [False, True])
def test_explicit_resolution_is_atomic_for_both_journals(claimed, outcome, with_submit):
    job, run = claimed
    action = db.begin_flow_action(job.id, run, "one")
    db.finish_flow_action(action, "advanced", "two")
    if with_submit:
        with db.connect() as c:
            c.execute("INSERT INTO submit_intents VALUES (?,?,?)", (job.id, run, 1))
    with pytest.raises(RuntimeError):
        db.resolve_submit_intents(job.id, outcome)
    db.reset_stuck_jobs()
    assert db.resolve_submit_intents(job.id, outcome) == 1 + int(with_submit)
    assert not db.has_uncertain_flow_action(job.id)
    assert not db.has_uncertain_submit_intent(job.id)
    assert db.get_job_status(job.id) == ("submitted" if outcome == "submitted" else "new")
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM flow_actions").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM applications WHERE status='submitted'").fetchone()[0] == int(outcome == "submitted")
    with pytest.raises(LookupError, match="no unresolved submit intents"):
        db.resolve_submit_intents(job.id, outcome)


def test_invalid_run_and_privacy_and_cross_run_guard(claimed):
    job, run = claimed
    dry = db.start_run("apply-dry")
    with pytest.raises(RuntimeError):
        db.begin_flow_action(job.id, dry, "shape")
    action = db.begin_flow_action(job.id, run, "synthetic page content")
    db.finish_flow_action(action, "advanced", "synthetic next page")
    with db.connect() as c:
        row = c.execute("SELECT * FROM flow_actions").fetchone()
        assert len(row["before_fingerprint"]) == 64
        assert "synthetic" not in row["after_fingerprint"]
        # Even a manually reassigned claim cannot consume a prior run's checkpoint.
        other = db.start_run("apply")
        c.execute("UPDATE jobs SET active_run_id=? WHERE id=?", (other, job.id))
    with pytest.raises(RuntimeError):
        db.begin_flow_action(job.id, other, "shape")


def test_fetch_cannot_clear_filter_hold_with_unresolved_flow(claimed):
    job, run = claimed
    action = db.begin_flow_action(job.id, run, "one")
    db.finish_flow_action(action, "advanced", "two")
    db.reset_stuck_jobs()
    with db.connect() as c:
        c.execute("INSERT INTO applications (job_id,status,reason,created_at) VALUES (?,'needs_review',?,1)",
                  (job.id, "source filter: missing"))
    db.kv_set("filter_review_reason:flow", "missing")
    assert fetch._mark_verified(job, "synthetic-policy")
    assert db.get_job_status(job.id) == "needs_review"


def test_runner_enriches_confirmed_receipt_without_double_count(claimed):
    from agent.models import ApplyResult, ApplyStatus

    job, run = claimed
    action = db.begin_flow_action(job.id, run, "before")
    db.finish_flow_action(action, "confirmed", "after")
    receipt = db.latest_application(job.id)
    result = ApplyResult(status=ApplyStatus.SUBMITTED, reason="verified by browser",
                         final_url="https://example.test/thanks")
    assert db.record_application(job, result, run) == receipt["id"]
    assert db.submissions_since(0) == 1
    latest = db.latest_application(job.id)
    assert latest["reason"] == result.reason
    assert latest["final_url"] == result.final_url
    assert latest["submitted_at"] == receipt["submitted_at"]


@pytest.mark.parametrize("status", ["failed", "needs_review"])
def test_durable_confirmation_wins_over_later_worker_error(claimed, status):
    from agent import runner
    from agent.models import ApplyResult, ApplyStatus

    job, run = claimed
    action = db.begin_flow_action(job.id, run, "before")
    db.finish_flow_action(action, "confirmed", "receipt")
    result = runner._guard_uncertain_submit(
        job, ApplyResult(ApplyStatus(status), "worker failed after confirmation"), False, run)
    assert result.status == ApplyStatus.SUBMITTED
    assert runner._consumed_submit_slot(job, run, False)
    assert not runner._consumed_submit_slot(job, run, True)
    db.record_application(job, result, run)
    db.set_job_status(job.id, runner._job_status_for(result, job, False, "new"))
    assert db.get_job_status(job.id) == "submitted"
    assert db.submissions_since(0) == 1
    assert db.next_jobs(10) == []
