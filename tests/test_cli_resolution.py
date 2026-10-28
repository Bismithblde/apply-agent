from __future__ import annotations

import fcntl
from contextlib import contextmanager

import pytest

from agent import __main__ as cli
from agent import db, report
from agent.models import ApplyResult, ApplyStatus, Job


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "apply.db")
    monkeypatch.setattr(cli, "_setup_logging", lambda _verbose: None)
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.init()
    return tmp_path


def _job(job_id: str) -> Job:
    return Job(job_id, f"Example Co {job_id}", f"Software Engineer Intern {job_id}",
               f"https://example.test/{job_id}", [],
               "Software", 1, source="test", ats="greenhouse")


def _add_intent(job_id: str) -> int:
    run_id = db.start_run("apply")
    with db.connect() as c:
        c.execute("INSERT INTO submit_intents (job_id, run_id, created_at) VALUES (?,?,?)",
                  (job_id, run_id, 1.0))
    db.end_run(run_id)
    return run_id


def _assert_lock_is_held(lock_path):
    with open(lock_path, "r+") as probe:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("outcome, expected_status", [
    ("submitted", "submitted"),
    ("not_submitted", "new"),
])
def test_resolve_cli_records_user_confirmed_outcome(cli_db, capsys, outcome, expected_status):
    job = _job("job:resolve")
    db.upsert_jobs([job])
    db.set_job_status(job.id, "needs_review")
    run_id = _add_intent(job.id)

    result = cli.main(["resolve", job.id, "--outcome", outcome])

    assert result == 0
    assert db.get_job_status(job.id) == expected_status
    assert not db.has_uncertain_submit_intent(job.id)
    with db.connect() as c:
        resolutions = c.execute(
            "SELECT outcome FROM submit_intent_resolutions WHERE job_id=? AND run_id=?",
            (job.id, run_id),
        ).fetchall()
        applications = c.execute(
            "SELECT run_id, created_at, submitted_at, reason FROM applications "
            "WHERE job_id=? AND status='submitted'", (job.id,),
        ).fetchall()
    assert [row["outcome"] for row in resolutions] == [outcome]
    if outcome == "submitted":
        assert db.submissions_since(0) == 1
        assert db.uncertain_submissions_since(0) == 0
        assert len(applications) == 1
        assert applications[0]["run_id"] == run_id
        assert applications[0]["created_at"] == 1.0
        assert applications[0]["submitted_at"] >= applications[0]["created_at"]
        assert applications[0]["reason"] == db.RESOLVED_SUBMIT_INTENT_REASON
    else:
        assert not applications
        assert db.submissions_since(0) == 0
        assert db.uncertain_submissions_since(0) == 1
    assert f"as {outcome}" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["mark", "requeue", "resolve"])
def test_mutation_cli_defers_when_shared_lock_is_busy(cli_db, capsys, command):
    job = _job(f"job:{command}-busy")
    db.upsert_jobs([job])
    if command == "mark":
        argv = ["mark", job.id, "--status", "submitted"]
    elif command == "requeue":
        db.set_job_status(job.id, "failed")
        argv = ["requeue", "--status", "failed"]
    else:
        db.set_job_status(job.id, "needs_review")
        _add_intent(job.id)
        argv = ["resolve", job.id, "--outcome", "not_submitted"]

    with open(cli_db / "data" / "apply.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cli.main(argv)

    assert result == cli.EX_TEMPFAIL
    assert "queue lock" in capsys.readouterr().err
    assert db.get_job_status(job.id) == ("new" if command == "mark" else
                                         "failed" if command == "requeue" else "needs_review")
    assert db.has_uncertain_submit_intent(job.id) is (command == "resolve")


@pytest.mark.parametrize("command", ["mark", "requeue", "resolve"])
def test_mutation_cli_holds_shared_lock_during_database_work(cli_db, monkeypatch, command):
    job = _job(f"job:{command}-lock")
    db.upsert_jobs([job])
    if command == "mark":
        argv = ["mark", job.id, "--status", "submitted"]
    elif command == "requeue":
        db.set_job_status(job.id, "failed")
        argv = ["requeue", "--status", "failed"]
    else:
        db.set_job_status(job.id, "needs_review")
        _add_intent(job.id)
        argv = ["resolve", job.id, "--outcome", "not_submitted"]

    original_connect = db.connect

    @contextmanager
    def checked_connect():
        _assert_lock_is_held(cli_db / "data" / "apply.lock")
        with original_connect() as conn:
            yield conn

    monkeypatch.setattr(db, "connect", checked_connect)
    assert cli.main(argv) == 0


def test_mark_requeue_and_resolve_preserve_unresolved_or_active_jobs(cli_db, capsys):
    unresolved = _job("job:uncertain")
    active = _job("job:active")
    db.upsert_jobs([unresolved, active])
    db.set_job_status(unresolved.id, "failed")
    _add_intent(unresolved.id)
    active_run = db.start_run("apply")
    assert db.mark_job_in_progress(active.id, active_run, previous_status="new")
    _add_intent(active.id)

    assert cli.main(["mark", unresolved.id, "--status", "submitted"]) == 1
    assert cli.main(["requeue", "--status", "failed"]) == 1
    assert cli.main(["mark", active.id, "--status", "submitted"]) == 1
    assert cli.main(["requeue", "--status", "in_progress"]) == 1
    assert cli.main(["resolve", active.id, "--outcome", "submitted"]) == 1

    err = capsys.readouterr().err
    assert "unresolved submit intent" in err
    assert "python -m agent resolve <job_id> --outcome submitted|not_submitted" in err
    assert "job is active" in err
    assert db.get_job_status(unresolved.id) == "failed"
    assert db.get_job_status(active.id) == db.IN_PROGRESS
    with db.connect() as c:
        row = c.execute("SELECT active_run_id FROM jobs WHERE id=?", (active.id,)).fetchone()
    assert row["active_run_id"] == active_run
    assert db.has_uncertain_submit_intent(unresolved.id)
    assert db.has_uncertain_submit_intent(active.id)


def test_mark_submitted_records_report_and_nightly_count_once(cli_db, monkeypatch):
    job = _job("job:manual-submission")
    db.upsert_jobs([job])
    report_dir = cli_db / "reports"
    report_dir.mkdir()
    monkeypatch.setattr(report, "LOG_DIR", report_dir)

    assert cli.main(["mark", job.id, "--status", "submitted"]) == 0
    assert db.get_job_status(job.id) == "submitted"
    assert db.submissions_since(0) == 1
    path, summary = report.build(since=0)
    assert summary.startswith("1 submitted")
    assert "Example Co job:manual-submission" in path.read_text()

    assert cli.main(["mark", job.id, "--status", "submitted"]) == 0
    assert db.submissions_since(0) == 1
    with db.connect() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM applications WHERE job_id=? AND status='submitted'", (job.id,),
        ).fetchone()[0]
    assert count == 1


def test_mark_submitted_does_not_duplicate_an_existing_submission(cli_db):
    job = _job("job:already-counted")
    db.upsert_jobs([job])
    db.record_application(job, ApplyResult(ApplyStatus.SUBMITTED, "previously recorded"), None)
    db.set_job_status(job.id, "failed")

    assert cli.main(["mark", job.id, "--status", "submitted"]) == 0

    assert db.get_job_status(job.id) == "submitted"
    assert db.submissions_since(0) == 1
    with db.connect() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM applications WHERE job_id=? AND status='submitted'", (job.id,),
        ).fetchone()[0]
    assert count == 1


def test_mark_submission_failure_rolls_back_status_update(cli_db, capsys):
    job = _job("job:atomic-mark")
    db.upsert_jobs([job])
    with db.connect() as c:
        c.execute(
            "CREATE TRIGGER fail_manual_submission AFTER UPDATE OF status ON jobs "
            "WHEN NEW.id='job:atomic-mark' AND NEW.status='submitted' "
            "BEGIN SELECT RAISE(ABORT, 'forced application insert failure'); END"
        )

    result = cli.main(["mark", job.id, "--status", "submitted"])

    assert result == 1
    assert "IntegrityError" in capsys.readouterr().err
    assert db.get_job_status(job.id) == "new"
    assert db.submissions_since(0) == 0
    assert job.id in {row.id for row in db.next_jobs(10)}


def test_resolve_does_not_echo_database_exception_details(cli_db, monkeypatch, capsys):
    job = _job("job:error")
    db.upsert_jobs([job])
    _add_intent(job.id)

    def fail_resolution(_job_id, _outcome):
        raise RuntimeError("SENSITIVE-DB-DETAIL")

    monkeypatch.setattr(db, "resolve_submit_intents", fail_resolution)
    result = cli.main(["resolve", job.id, "--outcome", "not_submitted"])

    assert result == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "SENSITIVE-DB-DETAIL" not in err


def test_resolve_help_explains_user_confirmation(cli_db, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["resolve", "--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "user-confirmed outcome" in help_text
    assert "unresolved final-submit attempt" in help_text


def test_pick_cli_list_only(cli_db, capsys):
    job = _job("job:pick-1")
    db.upsert_jobs([job])
    code = cli.main(["pick", "--list-only"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Found 1 new posting(s)" in out
    assert "Example Co job:pick-1" in out


def test_pick_cli_select_and_apply(cli_db, monkeypatch, capsys):
    job = _job("job:pick-2")
    db.upsert_jobs([job])

    called_with = []

    def fake_apply_batch(*args, **kwargs):
        called_with.append(kwargs)
        from types import SimpleNamespace
        return SimpleNamespace(run_id=1, lines=["applied ok"], submitted=1, queued=0, failed=0, dry_ok=0)

    from agent import runner
    monkeypatch.setattr(runner, "apply_batch", fake_apply_batch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")

    code = cli.main(["pick", "--dry-run"])
    assert code == 0
    assert len(called_with) == 1
    assert called_with[0]["job_ids"] == [job.id]
    assert called_with[0]["dry_run"] is True
