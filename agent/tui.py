"""Terminal launcher for the existing CLI commands (standard-library curses)."""
from __future__ import annotations

import argparse
import curses
import json
import logging
import threading
import time
import sys

from concurrent.futures import CancelledError

from . import db


def _draw(screen, y, text, selected=False):
    height, width = screen.getmaxyx()
    if y < height - 1 and width > 2:
        try:
            screen.addnstr(y, 1, text, width - 2, curses.A_REVERSE if selected else 0)
        except curses.error:
            pass


def _choose(screen, title, options, *, email_toggle=False):
    selected = 0
    while True:
        screen.erase()
        _draw(screen, 0, title)
        count = max(1, screen.getmaxyx()[0] - 4)
        first = max(0, selected - count + 1)
        for i, label in enumerate(options[first:first + count], first):
            _draw(screen, i - first + 2, label, i == selected)
        footer = '↑/↓ move · Enter select · q back'
        if email_toggle:
            footer += ' · e email ' + ('ON' if db.kv_get('pick_email_check') != 'false' else 'OFF')
        _draw(screen, screen.getmaxyx()[0] - 2, footer)
        screen.refresh()
        key = screen.getch()
        if email_toggle and key == ord('e'):
            db.kv_set('pick_email_check', 'false' if db.kv_get('pick_email_check') != 'false' else 'true')
        if key in (27, ord('q')):
            return None
        if key in (curses.KEY_UP, ord('k')):
            selected = (selected - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord('j')):
            selected = (selected + 1) % len(options)
        elif key in (10, 13, curses.KEY_ENTER):
            return selected


def _input(screen, label, default=''):
    value = default
    edited = False
    while True:
        screen.erase()
        _draw(screen, 0, label)
        _draw(screen, 2, value + '▏')
        screen.refresh()
        key = screen.getch()
        if key == 27:
            return None
        if key in (10, 13, curses.KEY_ENTER):
            return value.strip()
        if key in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
            edited = True
        elif 32 <= key <= 126 and len(value) < 100:
            if default and not edited:
                value = ''
            value += chr(key)
            edited = True


def _queue():
    try:
        value = json.loads(db.kv_get('tui_queue') or '[]')
        return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()
    except ValueError:
        return set()


def _save_selection(rows, selected):
    queue = (_queue() - {row['id'] for row in rows}) | selected
    db.kv_set('tui_queue', json.dumps(sorted(queue)))


def _working(screen, title, work):
    """Keep curses on the main thread while a finite operation runs."""
    done, stop, result = threading.Event(), threading.Event(), {}
    progress = [title]

    def emit(message):
        progress[0] = str(message)

    class Progress(logging.Handler):
        def emit(self, record):
            emit(record.getMessage())

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    streams = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.FileHandler)]
    handler = Progress()
    for stream in streams:
        root.removeHandler(stream)
    root.addHandler(handler)

    def worker():
        try:
            result['value'] = work(emit, stop)
        except BaseException as error:
            result['error'] = error
        finally:
            done.set()

    thread = threading.Thread(target=worker, name='tui-operation')
    frame = 0
    try:
        screen.nodelay(True)
        thread.start()
        while frame == 0 or not done.is_set():
            try:
                screen.erase()
                _draw(screen, 0, f'⚙ {"⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[frame % 10]} {title}')
                _draw(screen, 2, progress[0])
                _draw(screen, screen.getmaxyx()[0] - 2, 'q / Esc / Ctrl-C: stop after current operation')
                screen.refresh()
                if screen.getch() in (ord('q'), 27, 3):
                    stop.set()
                if stop.is_set():
                    emit('Stopping after the current operation…')
                done.wait(0.1)
                frame += 1
            except KeyboardInterrupt:
                stop.set()
                emit('Stopping after the current operation…')
        thread.join()
    finally:
        # A worker must finish before terminal ownership or logging is restored.
        if thread.is_alive():
            stop.set()
        while thread.is_alive():
            try:
                thread.join(0.1)
            except KeyboardInterrupt:
                pass
        root.handlers[:] = original_handlers
        screen.nodelay(False)
        try:
            curses.flushinp()
        except curses.error:
            pass
    if 'error' in result:
        raise result['error']
    return result['value']


def _dates(screen):
    from .__main__ import _date_arg
    while True:
        values = []
        for label in ('From date (YYYY-MM-DD, blank for any)', 'Through date (YYYY-MM-DD, blank for any)'):
            text = _input(screen, label)
            if text is None:
                return None
            try:
                values.append(_date_arg(text) if text else None)
            except argparse.ArgumentTypeError:
                break
        if len(values) == 2 and not (all(values) and values[0] > values[1]):
            return values
        if _choose(screen, 'Use valid dates with From on or before Through', ['Try again']) is None:
            return None


def _load_jobs(emit, stop, *, dates=None, latest=False):
    from .config import load_config
    from .job_search import find_jobs
    from .scheduler import fetch_once
    emit('Refreshing job sources…')
    fetch_once(load_config())
    if stop.is_set():
        raise CancelledError()
    notes = []

    def progress(message):
        notes.append(message)
        emit(message)

    rows, _ = find_jobs(start_date=dates[0] if dates else None, end_date=dates[1] if dates else None,
                        since_last_application=latest, emit=progress)
    if stop.is_set():
        raise CancelledError()
    return rows, notes[-1] if notes else ''


def _select_jobs(screen, rows, note='', *, queue_view=False):
    if not rows:
        _choose(screen, 'No matching jobs', ['Back'])
        return False
    cursor, selected = 0, _queue() & {row['id'] for row in rows}
    while True:
        screen.erase()
        height = screen.getmaxyx()[0]
        _draw(screen, 0, f'{"Queued jobs" if queue_view else "Jobs"} ({len(rows)}) · Selected ({len(selected)})')
        _draw(screen, 1, note)
        count = max(1, height - 6)
        first = max(0, cursor - count + 1)
        for index, row in enumerate(rows[first:first + count], first):
            date = time.strftime('%Y-%m-%d', time.localtime(row['date_posted'] or row['first_seen']))
            mark = 'x' if row['id'] in selected else ' '
            _draw(screen, index - first + 3, f"[{mark}] {date} [{row['status']}] {row['company']} — {row['title']}", index == cursor)
        _draw(screen, height - 3, rows[cursor]['url'])
        _draw(screen, height - 2, 'Space toggle · a all · n none · Enter ' + ('submit ready jobs' if queue_view else 'queue') + ' · q cancel')
        screen.refresh()
        key = screen.getch()
        if key in (27, ord('q')):
            return False
        if key in (curses.KEY_UP, ord('k')):
            cursor = (cursor - 1) % len(rows)
        elif key in (curses.KEY_DOWN, ord('j')):
            cursor = (cursor + 1) % len(rows)
        elif key == ord(' '):
            job_id = rows[cursor]['id']
            selected.symmetric_difference_update({job_id})
        elif key == ord('a'):
            selected = {row['id'] for row in rows}
        elif key == ord('n'):
            selected.clear()
        elif key in (10, 13, curses.KEY_ENTER):
            _save_selection(rows, selected)
            return True


def _apply_queue(emit, stop):
    from .config import load_config, load_profile
    from .runner import apply_batch
    from . import application_history
    application_history.initialize_from_records()
    queued = _queue()
    retained = {job_id for job_id in queued
                if (job := db.get_job(job_id)) is not None and not application_history.find_application(job)}
    jobs = sorted(job_id for job_id in retained if db.get_job_status(job_id) in ('new', 'failed_retry'))
    db.kv_set('tui_queue', json.dumps(sorted(retained)))
    if not jobs:
        return None
    stats = apply_batch(load_config(), load_profile(), limit=len(jobs), job_ids=jobs, submit_mode='full', stop_event=stop)
    if stats.run_id:
        remaining = {job_id for job_id in jobs if db.get_job_status(job_id) in ('new', 'failed_retry', 'needs_review', 'failed')}
        db.kv_set('tui_queue', json.dumps(sorted((_queue() - set(jobs)) | remaining)))
    return stats


def _menu(screen):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    while True:
        choice = _choose(screen, 'Application agent', ['Get latest jobs', 'Search by date',
                         f'Apply to queued jobs ({len(_queue())})', 'Quit'], email_toggle=True)
        if choice is None or choice == 3:
            return
        try:
            if choice in (0, 1):
                dates = _dates(screen) if choice == 1 else None
                if choice == 1 and dates is None:
                    continue
                rows, note = _working(screen, 'Getting jobs',
                                      lambda emit, stop: _load_jobs(emit, stop, dates=dates, latest=choice == 0))
                _select_jobs(screen, rows, note)
            elif not _queue():
                _choose(screen, 'The application queue is empty', ['Back'])
            else:
                queued = _queue()
                with db.connect() as c:
                    rows = [row for row in c.execute('SELECT * FROM jobs ORDER BY company,title')
                            if row['id'] in queued]
                if not _select_jobs(screen, rows, 'Enter submits ready jobs; review items stay queued.', queue_view=True):
                    continue
                stats = _working(screen, 'Applying to queued jobs', _apply_queue)
                if stats and stats.run_id:
                    _choose(screen, f'Submitted {stats.submitted} · Review {stats.queued} · Failed {stats.failed}', ['Back'])
                elif stats is not None:
                    _choose(screen, 'Queue is busy; queued jobs were kept', ['Back'])
                else:
                    _choose(screen, 'No ready jobs; review items remain queued', ['Back'])
        except CancelledError:
            continue
        except Exception as error:
            logging.getLogger(__name__).exception('TUI operation failed')
            _choose(screen, f'Operation failed ({type(error).__name__}); see logs', ['Back'])


def run() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('The terminal menu needs an interactive terminal. Use python -m agent --help for CLI modes.', file=sys.stderr)
        return 2
    try:
        curses.wrapper(_menu)
        return 0
    except (KeyboardInterrupt, EOFError):
        return 0
    except curses.error:
        print('Could not initialize the terminal menu; use python -m agent --help.', file=sys.stderr)
        return 2
