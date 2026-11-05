from datetime import datetime
import threading
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





class Screen:
    def __init__(self, keys=()):
        self.keys = iter(keys)
        self.lines = []
    def getmaxyx(self):
        return 24, 120
    def erase(self):
        pass
    def refresh(self):
        pass
    def keypad(self, value):
        pass
    def addnstr(self, y, x, text, *args):
        self.lines.append(text)
    def getch(self):
        return next(self.keys, -1)

    def nodelay(self, value):
        pass


def test_select_all_then_deselect_individually_and_preserve_other_queue(local_db):
    import json
    for name in ('one', 'two', 'three', 'outside'):
        add_job(name, 1, 1)
    db.kv_set('tui_queue', json.dumps(['outside']))
    with db.connect() as c:
        rows = c.execute("SELECT * FROM jobs WHERE id<>'outside' ORDER BY id").fetchall()
    screen = Screen([ord('a'), tui.curses.KEY_DOWN, ord(' '), 10])
    tui._select_jobs(screen, rows)
    assert tui._queue() == {'outside', rows[0]['id'], rows[2]['id']}
    tui._select_jobs(Screen([ord(' '), 10]), rows)
    assert tui._queue() == {'outside', rows[2]['id']}
    tui._select_jobs(Screen([ord('n'), 10]), rows)
    assert tui._queue() == {'outside'}
    tui._select_jobs(Screen([ord('a'), ord('q')]), rows)
    assert tui._queue() == {'outside'}


def test_latest_uses_last_confirmed_application_not_last_attempt(local_db, monkeypatch):
    from agent.models import ApplyResult, ApplyStatus
    receipt = stamp('2026-10-02T12:00:00')
    add_job('old', 1, receipt - 1)
    add_job('new', 1, receipt + 1)
    for i in range(60):
        add_job(f'additional-{i}', 1, receipt + 1)
    add_job('applied', 1, 1)
    application_history.record_application(db.get_job('applied'), receipt)
    db.kv_set('last_apply', str(receipt + 10000))
    db.kv_set('pick_email_check', 'false')
    calls = []
    monkeypatch.setattr('agent.config.load_config', lambda: {})
    monkeypatch.setattr('agent.scheduler.fetch_once', lambda config: calls.append('fetch'))
    rows, _ = tui._load_jobs(lambda _: None, threading.Event(), latest=True)
    assert calls == ['fetch']
    assert {row['id'] for row in rows} == {'new', *(f'additional-{i}' for i in range(60))}


def test_apply_processes_entire_queue_and_keeps_retryable_jobs(local_db, monkeypatch):
    import json
    for name in ('one', 'two', 'retry'):
        add_job(name, 1, 1)
    db.kv_set('tui_queue', json.dumps(['one', 'two', 'retry']))
    monkeypatch.setattr('agent.config.load_config', lambda: {})
    monkeypatch.setattr('agent.config.load_profile', lambda: {})
    calls = []
    def apply(config, profile, **kwargs):
        calls.append(kwargs)
        db.set_job_status('one', 'submitted')
        db.set_job_status('two', 'needs_review')
        return SimpleNamespace(run_id=1)
    monkeypatch.setattr('agent.runner.apply_batch', apply)
    stop = threading.Event()
    assert tui._apply_queue(lambda _: None, stop).run_id == 1
    assert calls == [{'limit': 3, 'job_ids': ['one', 'retry', 'two'], 'submit_mode': 'full', 'stop_event': stop}]
    assert tui._queue() == {'retry', 'two'}
    tui._apply_queue(lambda _: None, stop)
    assert calls[-1]['job_ids'] == ['retry']


def test_spinner_animates_and_restores_logging_on_success_and_failure(local_db):
    import logging
    import time
    root = logging.getLogger()
    handlers = list(root.handlers)
    screen = Screen()
    def work(emit, stop):
        emit('Synthetic operation')
        time.sleep(0.25)
        return 42
    assert tui._working(screen, 'Working', work) == 42
    frames = {line for line in screen.lines if line.startswith('⚙')}
    assert len(frames) >= 2
    assert root.handlers == handlers
    def fail(emit, stop):
        raise ValueError('synthetic')
    with pytest.raises(ValueError, match='synthetic'):
        tui._working(screen, 'Working', fail)
    assert root.handlers == handlers


def test_menu_is_three_actions_and_cancel_cannot_apply(local_db, monkeypatch):
    import json
    db.kv_set('tui_queue', json.dumps(['synthetic']))
    choices = iter([2, None])
    menus = []
    def choose(screen, title, options, **kwargs):
        menus.append(options)
        return next(choices)
    monkeypatch.setattr(tui, '_choose', choose)
    monkeypatch.setattr(tui, '_apply_queue', lambda *a: pytest.fail('cancelled apply'))
    monkeypatch.setattr(tui, '_select_jobs', lambda *a, **kw: False)
    tui._menu(Screen())
    assert menus[0] == ['Get latest jobs', 'Search by date', 'Apply to queued jobs (1)', 'Quit']


def test_queue_lock_contention_does_not_drop_jobs(local_db, monkeypatch):
    import json
    add_job('one', 1, 1)
    db.kv_set('tui_queue', json.dumps(['one']))
    monkeypatch.setattr('agent.config.load_config', lambda: {})
    monkeypatch.setattr('agent.config.load_profile', lambda: {})
    monkeypatch.setattr('agent.runner.apply_batch', lambda *a, **kw: SimpleNamespace(run_id=0))
    assert tui._apply_queue(lambda _: None, threading.Event()).run_id == 0
    assert tui._queue() == {'one'}


def test_working_consumes_cancel_and_discards_buffered_input(local_db, monkeypatch):
    flushed = []
    monkeypatch.setattr(tui.curses, 'flushinp', lambda: flushed.append(True))
    def work(emit, stop):
        assert stop.wait(1), 'cancel was not forwarded'
        return 'stopped'
    assert tui._working(Screen([ord('q'), 10]), 'Working', work) == 'stopped'
    assert flushed == [True]
