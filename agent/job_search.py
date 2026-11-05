"""Job-list filtering shared by the command line and terminal menu."""
from datetime import datetime, timedelta

from . import application_history, db, mail, prior_apps


def find_jobs(*, start_date=None, end_date=None, since_ts=None, since_last_application=False,
              email_check=None, include_applied=False, limit=None, emit=print):
    if start_date and end_date and start_date > end_date:
        raise ValueError('Start date must be on or before end date')
    if email_check is None:
        email_check = db.kv_get('pick_email_check') != 'false'
    application_history.initialize_from_records()
    if email_check and not include_applied:
        if mail.available():
            emit('Checking recent email for prior applications (up to 50 messages)…')
            try:
                prior_apps.sync_email_confirmations(max_results=50, raise_errors=True)
            except Exception:
                emit('Email sync failed; checking cached application confirmations only.')
        else:
            emit('Gmail unavailable; checking cached application confirmations only.')
        imported = application_history.initialize_from_cached_emails()
        if imported['unlinked']:
            emit(f"{imported['unlinked']} email confirmation(s) lack an exact posting identity; retained for review.")
    if since_last_application:
        with db.connect() as c:
            since_ts = c.execute('SELECT MAX(submitted_at) FROM application_history').fetchone()[0]
    clauses, params = ["status='new'"], []
    posted = 'CASE WHEN date_posted>0 THEN date_posted ELSE first_seen END'
    if since_ts:
        clauses.append('first_seen>=?')
        params.append(since_ts)
    if start_date:
        clauses.append(f'{posted}>=?')
        params.append(datetime.combine(start_date, datetime.min.time()).timestamp())
    if end_date:
        clauses.append(f'{posted}<?')
        params.append(datetime.combine(end_date + timedelta(days=1), datetime.min.time()).timestamp())
    with db.connect() as c:
        candidates = c.execute('SELECT * FROM jobs WHERE ' + ' AND '.join(clauses)
                               + f' ORDER BY {posted} DESC,company ASC', params).fetchall()
    rows, excluded = [], 0
    for row in candidates:
        if not include_applied and application_history.find_application(db._row_to_job(row)):
            excluded += 1
            continue
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows, excluded
