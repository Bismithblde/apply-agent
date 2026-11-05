"""Terminal launcher for the existing CLI commands (standard-library curses)."""
from __future__ import annotations

import argparse
import curses
import subprocess
import sys

from . import db


def _draw(screen, y, text, selected=False):
    height, width = screen.getmaxyx()
    if y < height - 1 and width > 2:
        try:
            screen.addnstr(y, 1, text, width - 2, curses.A_REVERSE if selected else 0)
        except curses.error:
            pass


def _choose(screen, title, options):
    selected = 0
    while True:
        screen.erase()
        _draw(screen, 0, title)
        count = max(1, screen.getmaxyx()[0] - 4)
        first = max(0, selected - count + 1)
        for i, label in enumerate(options[first:first + count], first):
            _draw(screen, i - first + 2, label, i == selected)
        _draw(screen, screen.getmaxyx()[0] - 2, '↑/↓ move · Enter select · Esc back · q quit')
        screen.refresh()
        key = screen.getch()
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


def _number(screen):
    from .__main__ import _positive_int
    while True:
        value = _input(screen, 'Maximum jobs', db.kv_get('tui_limit') or '25')
        if value is None:
            return None
        try:
            _positive_int(value)
            db.kv_set('tui_limit', value)
            return value
        except argparse.ArgumentTypeError:
            if _choose(screen, 'Enter a positive whole number', ['Try again']) is None:
                return None


def _application_options(screen):
    choice = _choose(screen, 'Application mode', ['Dry run', 'Fill only', 'Submit real applications'])
    if choice is None:
        return None
    flags = [['--dry-run'], ['--mode', 'fill_only'], ['--mode', 'full']][choice]
    if db.kv_get('tui_visible') == 'true':
        flags += ['--visible']
    return flags


def _search(screen):
    from .__main__ import _date_arg
    while True:
        dates = []
        for label in ('From date (YYYY-MM-DD, blank for any)', 'Through date (YYYY-MM-DD, blank for any)'):
            value = _input(screen, label)
            if value is None:
                return None
            dates.append(value)
        try:
            parsed = [_date_arg(v) if v else None for v in dates]
            if all(parsed) and parsed[0] > parsed[1]:
                raise ValueError
            break
        except (ValueError, argparse.ArgumentTypeError):
            if _choose(screen, 'Use valid dates with From on or before Through', ['Try again']) is None:
                return None
    limit = _number(screen)
    if limit is None:
        return None
    choice = _choose(screen, 'Search jobs', ['Show list', 'Select jobs to apply'])
    if choice is None:
        return None
    flags = ['pick', '--all', '--limit', limit]
    for flag, value in zip(('--from-date', '--to-date'), dates):
        if value:
            flags += [flag, value]
    if choice == 0:
        flags += ['--list-only']
    else:
        options = _application_options(screen)
        if options is None:
            return None
        flags += options
    return flags


def _settings(screen):
    while True:
        email = db.kv_get('pick_email_check') != 'false'
        visible = db.kv_get('tui_visible') == 'true'
        choice = _choose(screen, 'Settings', [
            f'Check recent email before listing jobs: {"ON" if email else "OFF"}',
            f'Show application browser: {"ON" if visible else "OFF"}',
            'Default maximum jobs', 'Back'])
        if choice is None or choice == 3:
            return
        if choice == 0:
            db.kv_set('pick_email_check', 'false' if email else 'true')
        elif choice == 1:
            db.kv_set('tui_visible', 'false' if visible else 'true')
        else:
            _number(screen)


def _menu(screen):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    options = ['Search saved jobs by date', 'Fetch latest jobs', 'Apply to queued jobs',
               'Review jobs', 'Interview', 'Status', 'Report', 'Settings',
               'Check setup', 'Authorize Gmail', 'Scheduled loop', 'Saved accounts', 'Initialize application WAL', 'Quit']
    while True:
        choice = _choose(screen, 'Application agent', options)
        if choice is None or choice == 13:
            return None
        if choice == 0:
            result = _search(screen)
            if result:
                return result
        elif choice == 7:
            _settings(screen)
        elif choice == 10:
            start = _choose(screen, 'Scheduled loop can submit real applications',
                            ['Cancel', 'Start configured loop (real submissions allowed)'])
            if start == 1:
                return ['loop']
        elif choice == 12:
            source = _choose(screen, 'Initialize application WAL',
                             ['Past application records (local)', 'Confirmation emails (Gmail)', 'Both'])
            if source is not None:
                return ['init-wal', '--source', ['records', 'emails', 'both'][source]]
        elif choice in (2, 3):
            limit = _number(screen)
            if limit is None:
                continue
            if choice == 3:
                return ['review', '--limit', limit]
            flags = _application_options(screen)
            if flags is not None:
                return ['apply', '--limit', limit, *flags]
        else:
            return {1: ['fetch'], 4: ['interview'], 5: ['status'], 6: ['report'],
                    8: ['doctor'], 9: ['auth', 'gmail'], 11: ['accounts', 'list']}[choice]


def run() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('The terminal menu needs an interactive terminal. Use python -m agent --help for CLI modes.',
              file=sys.stderr)
        return 2
    try:
        while True:
            command = curses.wrapper(_menu)
            if command is None:
                return 0
            try:
                subprocess.run([sys.executable, '-m', 'agent', *command], check=False)
            except KeyboardInterrupt:
                print('\nCommand interrupted.')
            input('\nPress Enter to return to the menu…')
    except (KeyboardInterrupt, EOFError):
        return 0
    except curses.error:
        print('Could not initialize the terminal menu; use python -m agent --help.', file=sys.stderr)
        return 2
