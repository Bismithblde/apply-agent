from __future__ import annotations

import fcntl
from types import SimpleNamespace

import pytest

from agent import __main__ as cli
from agent import db, fetch, scheduler


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scheduler.db")
    db.init()
    return tmp_path


def _assert_lock_is_held(lock_path):
    with open(lock_path, "r+") as probe:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_fetch_once_persists_last_fetch_before_releasing_lock(temp_db, monkeypatch):
    lock_path = temp_db / "apply.lock"
    monkeypatch.setattr(fetch, "SOURCES", [])
    kv_set = db.kv_set
    stamps = []

    def checked_kv_set(key, value):
        if key == "last_fetch":
            _assert_lock_is_held(lock_path)
            stamps.append(value)
        kv_set(key, value)

    monkeypatch.setattr(db, "kv_set", checked_kv_set)

    stats = scheduler.fetch_once({})

    assert stats["fetched"] == 0
    assert len(stamps) == 1
    assert db.kv_get("last_fetch") == stamps[0]


def test_fetch_cli_reports_lock_busy_with_tempfail(temp_db, monkeypatch, capsys):
    lock_path = temp_db / "apply.lock"
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose: None)

    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code = cli.main(["fetch"])

    assert exit_code == cli.EX_TEMPFAIL
    assert "fetch deferred" in capsys.readouterr().err
    assert db.kv_get("last_fetch") is None


def test_apply_scheduler_retries_lock_busy_without_advancing_last_apply(temp_db, monkeypatch):
    lock_path = temp_db / "apply.lock"
    db.kv_set("last_apply", "123")
    monkeypatch.setattr(scheduler.report, "build", lambda: pytest.fail("report should not run on lock busy"))

    class ApplyCliStub:
        def __init__(self, *args, **kwargs):
            _assert_lock_is_held(lock_path)

        def wait(self, timeout=None):
            return cli.EX_TEMPFAIL

    monkeypatch.setattr(scheduler.subprocess, "Popen", ApplyCliStub)

    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scheduler.apply_once({"schedule": {"max_run_time": "1m"}})

    assert db.kv_get("last_apply") == "123"
    assert scheduler._due("last_apply", "30m")


def test_apply_cli_reports_lock_busy_with_tempfail(temp_db, monkeypatch, capsys):
    lock_path = temp_db / "apply.lock"
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "load_profile", lambda: {})
    monkeypatch.setattr(cli, "_setup_logging", lambda verbose: None)

    def apply_batch(*args, **kwargs):
        _assert_lock_is_held(lock_path)
        return SimpleNamespace(run_id=0)

    monkeypatch.setattr("agent.runner.apply_batch", apply_batch)

    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exit_code = cli.main(["apply"])

    assert exit_code == cli.EX_TEMPFAIL
    assert "apply skipped" in capsys.readouterr().err
