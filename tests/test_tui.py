from datetime import datetime
from types import SimpleNamespace

import pytest

from agent import __main__ as cli, db, mail, prior_apps, tui, application_history
from agent.models import Job


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'jobs.db')
    db.init()
    monkeypatch.setattr(cli, '_setup_logging', lambda _: None)
    monkeypatch.setattr(mail, 'available', lambda: False)


def add_job(name, posted, discovered):
    db.upsert_jobs([Job(name, name, 'Software Engineer', 'https://example.test/' + name,
                       [], 'Software', posted)])
    with db.connect() as c:
        c.execute('UPDATE jobs SET first_seen=? WHERE id=?', (discovered, name))


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def test_date_search_inclusive_boundaries_fallback_and_last_apply(local_db, capsys):
    add_job('start-hit', stamp('2026-10-01'), stamp('2026-11-01'))
    add_job('end-hit', stamp('2026-10-02T23:59:59'), stamp('2026-11-01'))
    add_job('unknown-hit', 0, stamp('2026-10-02T12:00:00'))
    add_job('before-miss', stamp('2026-09-30T23:59:59'), stamp('2026-10-02'))
    add_job('after-miss', stamp('2026-10-03'), stamp('2026-10-02'))
    db.kv_set('last_apply', str(stamp('2026-12-01')))
    assert cli.main(['pick', '--from-date', '2026-10-01', '--to-date', '2026-10-02',
                     '--no-email-check', '--list-only']) == 0
    output = capsys.readouterr().out
    assert all(name in output for name in ['start-hit', 'end-hit', 'unknown-hit'])
    assert 'before-miss' not in output and 'after-miss' not in output


@pytest.mark.parametrize('flags', [[], ['--email-check'], ['--no-email-check']])
@pytest.mark.parametrize('saved', ['true', 'false'])
def test_email_preference_and_override_run_before_list(local_db, monkeypatch, capsys, flags, saved):
    add_job('Candidate', 1, 1)
    db.kv_set('pick_email_check', saved)
    monkeypatch.setattr(mail, 'available', lambda: True)
    calls = []
    def sync(**kwargs):
        assert 'Candidate' not in capsys.readouterr().out
        calls.append('sync')
    monkeypatch.setattr(prior_apps, 'sync_email_confirmations', sync)
    monkeypatch.setattr(application_history, 'find_application', lambda *a, **kw: calls.append('wal'))
    cli.main(['pick', '--all', '--list-only', *flags])
    enabled = flags == ['--email-check'] or (not flags and saved == 'true')
    assert calls == (['sync', 'wal'] if enabled else ['wal'])


def test_email_failure_still_checks_cached_confirmations(local_db, monkeypatch, capsys):
    add_job('AlreadyApplied', 1, 1)
    monkeypatch.setattr(mail, 'available', lambda: True)
    def failed(**kwargs):
        raise RuntimeError('synthetic sync failure')
    monkeypatch.setattr(prior_apps, 'sync_email_confirmations', failed)
    monkeypatch.setattr(application_history, 'find_application', lambda *a, **kw: {'id': 'synthetic'})
    cli.main(['pick', '--all', '--list-only'])
    output = capsys.readouterr().out
    assert 'cached' in output and 'AlreadyApplied' not in output


@pytest.mark.parametrize('args', [['--from-date', 'bad'], ['--to-date', '2026-02-30'], ['--limit', '0']])
def test_invalid_search_inputs_rejected(local_db, args):
    with pytest.raises(SystemExit) as exc:
        cli.main(['pick', '--list-only', *args])
    assert exc.value.code == 2


def test_reversed_dates_rejected_before_mail(local_db, monkeypatch):
    monkeypatch.setattr(mail, 'available', lambda: pytest.fail('must validate before checking mail'))
    assert cli.main(['pick', '--from-date', '2026-11-02', '--to-date', '2026-11-01']) == 2


def test_tui_search_builds_cli_arguments_and_cancel_has_no_command(local_db, monkeypatch):
    answers = iter(['2026-10-01', '2026-10-03', '5'])
    monkeypatch.setattr(tui, '_input', lambda *a: next(answers))
    monkeypatch.setattr(tui, '_choose', lambda *a: 0)
    assert tui._search(None) == ['pick', '--all', '--limit', '5', '--from-date', '2026-10-01',
                                 '--to-date', '2026-10-03', '--list-only']
    monkeypatch.setattr(tui, '_input', lambda *a: None)
    assert tui._search(None) is None


def test_settings_persist_email_toggle(local_db, monkeypatch):
    choices = iter([0, 3])
    monkeypatch.setattr(tui, '_choose', lambda *a: next(choices))
    tui._settings(None)
    assert db.kv_get('pick_email_check') == 'false'
    choices = iter([0, 3])
    tui._settings(None)
    assert db.kv_get('pick_email_check') == 'true'


def test_historical_cycle_can_match_explicit_historical_role():
    assert prior_apps.is_purely_same_role('Software Engineer Intern Summer 2026',
                                         'Software Engineer Intern Summer 2026')
    assert not prior_apps.is_purely_same_role('Software Engineer Intern Summer 2027',
                                             'Software Engineer Intern Summer 2026')


def test_wal_filters_exact_posting_even_with_email_off(local_db, monkeypatch, capsys):
    from agent.models import ApplyResult, ApplyStatus
    add_job('known', 1, 1)
    add_job('distinct', 1, 1)
    db.record_application(db.get_job('known'), ApplyResult(ApplyStatus.SUBMITTED, 'fixture receipt'), None)
    monkeypatch.setattr(mail, 'available', lambda: pytest.fail('email disabled'))
    cli.main(['pick', '--all', '--no-email-check', '--list-only'])
    output = capsys.readouterr().out
    assert 'https://example.test/known' not in output
    assert 'https://example.test/distinct' in output
    assert 'WAL' in output


def test_initialize_local_wal_is_idempotent_and_never_reads_email(local_db, monkeypatch):
    monkeypatch.setattr(mail, 'available', lambda: pytest.fail('records-only must stay local'))
    assert cli.main(['init-wal', '--source', 'records']) == 0
    assert cli.main(['init-wal', '--source', 'records']) == 0


def test_tui_initialization_dispatch_and_default_application_mode(local_db, monkeypatch):
    choices = iter([12, 1])
    monkeypatch.setattr(tui, '_choose', lambda *a: next(choices))
    assert tui._menu(SimpleNamespace(keypad=lambda _: None)) == ['init-wal', '--source', 'emails']
    monkeypatch.setattr(tui, '_choose', lambda *a: 0)
    assert tui._application_options(None) == ['--dry-run']


def test_launcher_reuses_cli_without_shell(local_db, monkeypatch):
    commands = iter([['status'], None])
    monkeypatch.setattr(tui.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(tui.sys, 'stdout', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(tui.curses, 'wrapper', lambda _: next(commands))
    called = []
    monkeypatch.setattr(tui.subprocess, 'run', lambda args, **kw: called.append((args, kw)))
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert tui.run() == 0
    assert called == [([tui.sys.executable, '-m', 'agent', 'status'], {'check': False})]


def test_scheduled_loop_requires_explicit_real_submission_choice(local_db, monkeypatch):
    choices = iter([10, 0, None])
    monkeypatch.setattr(tui, '_choose', lambda *a: next(choices))
    screen = SimpleNamespace(keypad=lambda _: None)
    assert tui._menu(screen) is None
    choices = iter([10, 1])
    assert tui._menu(screen) == ['loop']
