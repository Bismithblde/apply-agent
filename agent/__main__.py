from __future__ import annotations

import argparse
import fcntl
import logging
import sys
import time
from datetime import date
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler

from . import db
from .config import LOG_DIR, load_config, load_profile

EX_TEMPFAIL = 75


def _setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "agent.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _brief_error(error: Exception) -> str:
    lines = str(error).splitlines()
    return (lines[0] if lines else type(error).__name__)[:200]


class _ApplyLockBusy(Exception):
    pass


@contextmanager
def _apply_mutation_lock():
    """Serialize queue mutations with apply, review, and fetch."""
    lock_path = db.DB_PATH.with_name("apply.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _ApplyLockBusy from exc
        yield


def _job_state(job_id: str):
    with db.connect() as c:
        return c.execute("SELECT status, active_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()


def _mutation_block_reason(job_id: str, state) -> str | None:
    if state["status"] == db.IN_PROGRESS or state["active_run_id"] is not None:
        return ("job is active or has a stale active-run marker; run `python -m agent apply` once to recover it, "
                "then retry")
    if db.has_uncertain_flow_action(job_id):
        return "interrupted application flow; verify its outcome, then use `python -m agent resolve <job_id> --outcome submitted|not_submitted`"
    if db.has_uncertain_submit_intent(job_id):
        return ("unresolved submit intent; confirm its outcome with `python -m agent resolve <job_id> "
                "--outcome submitted|not_submitted`")
    return None


def _mark_job_status(job_id: str, status: str) -> str | None:
    """Atomically guard and update one job, recording manual submissions exactly once."""
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        state = c.execute("SELECT status, active_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if state is None:
            return "job was not found"
        if state["status"] == db.IN_PROGRESS or state["active_run_id"] is not None:
            return ("job is active or has a stale active-run marker; run `python -m agent apply` once to recover it, "
                    "then retry")
        if db._unresolved_flow_action(c, job_id):
            return "interrupted application flow; verify its outcome, then use `python -m agent resolve <job_id> --outcome submitted|not_submitted`"
        if db._unresolved_submit_intent(c, job_id):
            return ("unresolved submit intent; confirm its outcome with `python -m agent resolve <job_id> "
                    "--outcome submitted|not_submitted`")
        has_submission = c.execute(
            "SELECT 1 FROM applications WHERE job_id=? AND status='submitted' LIMIT 1", (job_id,),
        ).fetchone() is not None
        changed = c.execute(
            "UPDATE jobs SET status=?, active_run_id=NULL, active_run_previous_status=NULL "
            "WHERE id=? AND status=? AND active_run_id IS NULL",
            (status, job_id, state["status"]),
        ).rowcount
        if not changed:
            return "job state changed; retry after the current run finishes"
        if status == "submitted" and not has_submission:
            now = time.time()
            receipt = c.execute(
                "INSERT INTO applications (job_id, run_id, status, reason, created_at, submitted_at) "
                "VALUES (?, NULL, 'submitted', ?, ?, ?)",
                (job_id, "manually confirmed submitted", now, now),
            )
            db._journal_receipt(c, int(receipt.lastrowid))
    return None


def _print_mutation_lock_busy(command: str) -> int:
    print(f"{command} deferred: another apply, review, or fetch run holds the queue lock; retry when it finishes",
          file=sys.stderr)
    return EX_TEMPFAIL


def cmd_fetch(a) -> int:
    from .fetch import FetchLockBusy
    from .scheduler import fetch_once
    try:
        s = fetch_once(load_config())
    except FetchLockBusy as exc:
        print(f"{exc}; retry after the current run finishes", file=sys.stderr)
        return EX_TEMPFAIL
    print(f"fetched {s['fetched']}, matched {s['matched']}, new {s['new']}")
    for src, st in s.get("by_source", {}).items():
        print(f"  {src}: {st}")
    return 0


def cmd_apply(a) -> int:
    from . import report
    from .runner import apply_batch
    stats = apply_batch(load_config(), load_profile(), limit=a.limit, dry_run=a.dry_run, submit_mode=a.mode,
                        job_ids=a.job, ats=a.ats, visible=a.visible, force=a.force)
    if stats.run_id == 0:
        print("apply skipped: the apply/review lock is busy; retry after the current run finishes", file=sys.stderr)
        return EX_TEMPFAIL
    print("\n".join(stats.lines))
    print(f"\nsubmitted {stats.submitted}, queued {stats.queued}, failed {stats.failed}, dry-run ok {stats.dry_ok}")
    path, summary = report.build()
    print(f"report: {path}")
    if stats.queued > 0 and sys.stdin.isatty() and not a.dry_run:
        try:
            prompt = input(f"\n[!] Batch complete with {stats.queued} jobs needing review. Run interactive interview now? [Y/n]: ")
            if prompt.strip().lower() in ("y", "yes", ""):
                from .interview import run_interview
                cfg, prof = load_config(), load_profile()
                unlocked = run_interview(cfg, prof)
                if unlocked:
                    re_apply = input(f"Re-apply to {len(unlocked)} unlocked job(s) now? [Y/n]: ")
                    if re_apply.strip().lower() in ("y", "yes", ""):
                        apply_batch(cfg, prof, job_ids=unlocked)
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


def cmd_loop(a) -> None:
    from .scheduler import loop
    loop(apply_every=a.every)


def cmd_report(a) -> None:
    from . import report
    path, summary = report.build()
    print(summary)
    print(path.read_text())


def cmd_status(a) -> None:
    for status, n in sorted(db.status_counts().items()):
        print(f"{status:14} {n}")
    with db.connect() as c:
        print("\nby ATS (pending):")
        for r in c.execute("SELECT ats, COUNT(*) FROM jobs WHERE status='new' GROUP BY ats ORDER BY 2 DESC"):
            print(f"  {r[0]:16} {r[1]}")
    print(f"\nsubmitted since 6 PM: {db.submissions_since(db.night_start())}, "
          f"captcha spend: ${db.captcha_spend_since(db.night_start()):.2f}")


def cmd_review(a) -> None:
    """Open queued jobs in visible ATS-filled tabs for manual finishing."""
    import fcntl

    from .ats.base import ApplyContext
    from .ats.registry import adapter_for
    from .browser import open_browser
    config = load_config()
    review_config = dict(config)
    review_config["accounts"] = dict(config.get("accounts", {}))
    review_config["accounts"]["create_in_dry_run"] = False
    review_config["accounts"]["create_in_fill_only"] = False
    review_config["submit_mode"] = "fill_only"

    lock_path = db.DB_PATH.with_name("apply.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another apply or review run is using the browser profile; review not opened")
            return

        rows = db.jobs_by_status("needs_review")[:max(0, a.limit)]
        if not rows:
            print("review queue is empty")
            return

        try:
            profile = load_profile()
            with open_browser(review_config, visible=True) as browser:
                print(f"Opening {len(rows)} review job(s); each tab will be filled when its ATS adapter supports it.")
                for row in rows:
                    job_id = row["id"]
                    job = db.get_job(job_id)
                    last = db.latest_application(job_id)
                    print(f"\n- {row['company']}: {row['title']}\n  {row['url']}"
                          f"\n  queued reason: {last['reason'] if last else '?'}")
                    if job is None:
                        print("  result: job is no longer in the database")
                        continue

                    try:
                        page = browser.new_page()
                    except Exception as e:
                        print(f"  result: could not create tab ({type(e).__name__}: {_brief_error(e)})")
                        continue

                    try:
                        adapter = adapter_for(job, review_config.get("auto_ats", []))
                        if adapter is None:
                            try:
                                page.goto(job.url, wait_until="domcontentloaded")
                                print(f"  result: no adapter enabled for ATS {job.ats}; posting opened for manual review")
                            except Exception as e:
                                print(f"  result: no adapter for ATS {job.ats}; page failed ({type(e).__name__}: "
                                      f"{_brief_error(e)})")
                            continue
                        context = ApplyContext(
                            page=page,
                            browser=browser,
                            config=review_config,
                            profile=profile,
                            submit_mode="fill_only",
                            dry_run=True,
                            run_id=None,
                        )
                        result = adapter.apply(job, context)
                        print(f"  adapter: {adapter.name}; result: {result.status.value}: {result.reason}")
                        print(f"  filled fields: {result.fields_total}; unanswered required: "
                              f"{len(result.unanswered_required)}")
                    except Exception as e:
                        print(f"  result: adapter/page failure ({type(e).__name__}: {_brief_error(e)})")

                try:
                    input("\nFinish the open tabs manually, then press Enter to close them. "
                          "Review does not submit or change job status.\n"
                      "After submitting manually, record it with `python -m agent mark <job_id> --status submitted`. "
                      "If an unresolved submit intent exists, use `python -m agent resolve <job_id> "
                      "--outcome submitted` instead.\n")
                except EOFError:
                    print("\nNo interactive input; closing review tabs.")
        except Exception as e:
            print(f"review browser failed: {type(e).__name__}: {_brief_error(e)}")


def cmd_pick(a) -> int:
    """Interactively list unapplied postings since last apply and apply to selected ones."""
    import json
    from .runner import apply_batch

    start_date, end_date = getattr(a, "from_date", None), getattr(a, "to_date", None)
    if start_date and end_date and start_date > end_date:
        print("Start date must be on or before end date.", file=sys.stderr)
        return 2
    from .job_search import find_jobs
    last_apply_val = db.kv_get("last_apply")
    since_ts = (float(last_apply_val) if last_apply_val and not a.all
                and not (start_date or end_date) else None)
    rows, excluded_count = find_jobs(start_date=start_date, end_date=end_date, since_ts=since_ts,
                                     email_check=getattr(a, "email_check", None),
                                     include_applied=a.include_applied, limit=a.limit)

    if not rows:
        msg = "No new unapplied postings found"
        if start_date or end_date:
            msg += f" in date range {start_date or 'any'} to {end_date or 'any'}"
        if since_ts:
            msg += f" since last apply ({time.strftime('%Y-%m-%d %H:%M', time.localtime(since_ts))})"
        if excluded_count:
            msg += f" ({excluded_count} were filtered because you already applied according to the application WAL)"
        print(msg + " (use --all or --include-applied to inspect).")
        return 0

    header = f"Found {len(rows)} new posting(s)"
    if start_date or end_date:
        header += f" ({start_date or 'any date'} to {end_date or 'any date'})"
    if since_ts:
        header += f" since last apply ({time.strftime('%Y-%m-%d %H:%M', time.localtime(since_ts))})"
    if excluded_count:
        header += f" [{excluded_count} previously applied in application WAL omitted]"
    print(f"\n{header}:\n")

    for i, r in enumerate(rows, 1):
        locs = json.loads(r["locations"]) if r["locations"] else []
        loc_str = f" [{', '.join(locs[:2])}]" if locs else ""
        print(f"[{i:2d}] {r['company']} - {r['title']} ({r['ats']}){loc_str}")
        print(f"     {r['url']}")

    if a.export:
        out_path = LOG_DIR / "new_jobs.md"
        lines = [f"# New Internship Postings ({time.strftime('%Y-%m-%d %H:%M')})", ""]
        for i, r in enumerate(rows, 1):
            locs = json.loads(r["locations"]) if r["locations"] else []
            loc_str = f" ({', '.join(locs[:2])})" if locs else ""
            lines.append(f"{i}. **{r['company']}** - [{r['title']}]({r['url']}) `{r['ats']}`{loc_str} (ID: `{r['id']}`)")
        out_path.write_text("\n".join(lines))
        print(f"\nExported list to {out_path}")

    if a.list_only:
        return 0

    print("\nEnter job numbers to apply for (e.g. 1, 3, 5-8, 'all', or 'q' to quit):")
    try:
        choice = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 0

    if not choice or choice.lower() in ("q", "quit", "exit"):
        print("Cancelled.")
        return 0

    selected_indices: set[int] = set()
    if choice.lower() == "all":
        selected_indices = set(range(1, len(rows) + 1))
    else:
        for part in choice.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    s_str, e_str = part.split("-", 1)
                    selected_indices.update(range(int(s_str), int(e_str) + 1))
                except ValueError:
                    pass
            else:
                try:
                    selected_indices.add(int(part))
                except ValueError:
                    pass

    selected_jobs = [rows[i - 1]["id"] for i in sorted(selected_indices) if 1 <= i <= len(rows)]
    if not selected_jobs:
        print("No valid jobs selected.")
        return 0

    print(f"\nApplying to {len(selected_jobs)} selected job(s)...")
    stats = apply_batch(
        load_config(),
        load_profile(),
        limit=len(selected_jobs),
        dry_run=a.dry_run,
        submit_mode=a.mode,
        job_ids=selected_jobs,
        visible=a.visible,
    )
    if stats.run_id == 0:
        print("apply skipped: the apply/review lock is busy; retry after the current run finishes", file=sys.stderr)
        return EX_TEMPFAIL
    print("\n".join(stats.lines))
    print(f"\nsubmitted {stats.submitted}, queued {stats.queued}, failed {stats.failed}, dry-run ok {stats.dry_ok}")
    return 0


def cmd_mark(a) -> int:
    marked = 0
    blocked = 0
    try:
        with _apply_mutation_lock():
            for jid in dict.fromkeys(a.job_id):
                try:
                    reason = _mark_job_status(jid, a.status)
                    if reason:
                        print(f"mark blocked {jid!r}: {reason}", file=sys.stderr)
                        blocked += 1
                    else:
                        marked += 1
                except Exception as exc:
                    print(f"mark failed for {jid!r}: database operation failed ({type(exc).__name__})",
                          file=sys.stderr)
                    blocked += 1
    except _ApplyLockBusy:
        return _print_mutation_lock_busy("mark")
    except Exception as exc:
        print(f"mark failed: database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"marked {marked} job(s) {a.status}")
    return 1 if blocked else 0


def cmd_requeue(a) -> int:
    requeued = 0
    blocked = 0
    try:
        with _apply_mutation_lock():
            with db.connect() as c:
                rows = c.execute("SELECT id, status, active_run_id FROM jobs WHERE status=? ORDER BY id",
                                 (a.status,)).fetchall()
            for row in rows:
                jid = row["id"]
                try:
                    reason = _mutation_block_reason(jid, row)
                    if reason:
                        print(f"requeue blocked {jid!r}: {reason}", file=sys.stderr)
                        blocked += 1
                        continue
                    with db.connect() as c:
                        changed = c.execute(
                            "UPDATE jobs SET status='new', attempts=0 "
                            "WHERE id=? AND status=? AND active_run_id IS NULL",
                            (jid, a.status),
                        ).rowcount
                    if changed:
                        requeued += 1
                    else:
                        print(f"requeue skipped {jid!r}: job state changed; retry after the current run finishes",
                              file=sys.stderr)
                        blocked += 1
                except Exception as exc:
                    print(f"requeue failed for {jid!r}: database operation failed ({type(exc).__name__})",
                          file=sys.stderr)
                    blocked += 1
    except _ApplyLockBusy:
        return _print_mutation_lock_busy("requeue")
    except Exception as exc:
        print(f"requeue failed: database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"requeued {requeued} job(s) from {a.status}")
    return 1 if blocked else 0


def cmd_resolve(a) -> int:
    try:
        with _apply_mutation_lock():
            state = _job_state(a.job_id)
            if state is None:
                print(f"resolve failed {a.job_id!r}: job was not found", file=sys.stderr)
                return 1
            if state["status"] == db.IN_PROGRESS or state["active_run_id"] is not None:
                print(f"resolve deferred {a.job_id!r}: job has an active-run marker; run `python -m agent apply` "
                      "once to recover it, then retry", file=sys.stderr)
                return 1
            if not (db.has_uncertain_submit_intent(a.job_id) or db.has_uncertain_flow_action(a.job_id)):
                print(f"resolve failed {a.job_id!r}: no unresolved submit intent exists; nothing changed",
                      file=sys.stderr)
                return 1
            try:
                resolved = db.resolve_submit_intents(a.job_id, a.outcome)
            except LookupError as exc:
                detail = str(exc)
                if "no unresolved submit intents" in detail:
                    print(f"resolve failed {a.job_id!r}: no unresolved submit intent exists; nothing changed",
                          file=sys.stderr)
                elif "does not exist" in detail:
                    print(f"resolve failed {a.job_id!r}: job was not found", file=sys.stderr)
                else:
                    print(f"resolve failed {a.job_id!r}: lookup failed", file=sys.stderr)
                return 1
            except RuntimeError as exc:
                if "is active; resolve its outcome after recovery" in str(exc):
                    print(f"resolve deferred {a.job_id!r}: job has an active-run marker; run `python -m agent apply` "
                          "once to recover it, then retry", file=sys.stderr)
                else:
                    print(f"resolve failed {a.job_id!r}: database operation failed (RuntimeError)",
                          file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"resolve failed {a.job_id!r}: database operation failed ({type(exc).__name__})",
                      file=sys.stderr)
                return 1
    except _ApplyLockBusy:
        return _print_mutation_lock_busy("resolve")
    except Exception as exc:
        print(f"resolve failed {a.job_id!r}: database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"resolved {resolved} action intent(s) for {a.job_id!r} as {a.outcome}")
    return 0


def cmd_accounts(a) -> None:
    from . import vault
    if a.action == "list":
        for r in db.list_account_rows():
            print(f"{r['tenant_key']:60} {r['email']:40} {'verified' if r['verified'] else 'unverified'}")
    elif a.action == "show":
        acct = vault.get(a.tenant)
        if acct is None:
            print("not found")
        else:
            print(acct.email)
            print(acct.password if a.show_password else "password hidden (pass --show-password to reveal)")


def cmd_auth(a) -> None:
    from . import mail
    mail.authorize_interactive()
    print("Gmail authorized." if mail.available() else "Authorization finished but Gmail is not reachable.")


def cmd_interview(a) -> int:
    from .interview import run_interview
    config = load_config()
    profile = load_profile()
    unlocked = run_interview(
        config=config,
        profile=profile,
        job_ids=a.job,
        auto_reapply=a.reapply,
    )
    if unlocked and not a.reapply and sys.stdin.isatty():
        try:
            prompt = input(f"\nRe-apply to the {len(unlocked)} unlocked job(s) now? [Y/n]: ")
            if prompt.strip().lower() in ("y", "yes", ""):
                from .runner import apply_batch
                apply_batch(config, profile, job_ids=unlocked)
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


def cmd_doctor(a) -> None:
    from . import doctor
    sys.exit(doctor.main())


def _positive_int(value: str) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def _date_arg(value: str) -> date:
    try:
        if len(value) != 10 or value[4] != "-" or value[7] != "-":
            raise ValueError
        parsed = date.fromisoformat(value)
        if parsed == date.max:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("use a valid date in YYYY-MM-DD format")


def cmd_init_wal(a) -> int:
    from . import application_history
    if a.source in ("records", "both"):
        count = application_history.initialize_from_records()
        print(f"Imported {count} confirmed local application record(s) into the WAL.")
    if a.source in ("emails", "both"):
        print("Initializing WAL from confirmation emails…")
        try:
            result = application_history.initialize_from_emails(max_results=a.max_emails)
        except Exception:
            print("Email initialization incomplete; successfully indexed records were saved. Check Gmail with doctor.",
                  file=sys.stderr)
            return 1
        print(f"Imported {result['imported']} email confirmation(s): {result['linked']} with posting identities, "
              f"{result['unlinked']} unlinked (will not hide jobs).")
    return 0


def cmd_tui(a) -> int:
    from .tui import run
    return run()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        argv = ["tui"]
    p = argparse.ArgumentParser(prog="python -m agent", description="Internship auto-applier")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tui", help="open the terminal menu").set_defaults(fn=cmd_tui)

    sub.add_parser("fetch", help="pull listings from all sources").set_defaults(fn=cmd_fetch)
    hp = sub.add_parser("init-wal", aliases=["history-init"], help="initialize application history from saved records or emails")
    hp.add_argument("--source", choices=["records", "emails", "both"], default="records")
    hp.add_argument("--max-emails", type=_positive_int, help="maximum confirmations to scan (default: all pages)")
    hp.set_defaults(fn=cmd_init_wal)

    ap = sub.add_parser("apply", help="apply to queued jobs once")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true", help="fill everything, never submit")
    ap.add_argument("--mode", choices=["full", "hybrid", "fill_only"])
    ap.add_argument("--job", nargs="+", help="specific job ids")
    ap.add_argument("--ats", nargs="+", help="only these ATS (greenhouse lever ashby workday other ...)")
    ap.add_argument("--visible", action="store_true", help="show the browser window")
    ap.add_argument("--force", action="store_true", help="with --job: re-apply even if already submitted")
    ap.set_defaults(fn=cmd_apply)

    lp = sub.add_parser("loop", help="run forever on the config schedule")
    lp.add_argument("--every", help="override schedule.apply_every, e.g. 30m")
    lp.set_defaults(fn=cmd_loop)

    sub.add_parser("report", help="write and print tonight's report").set_defaults(fn=cmd_report)
    sub.add_parser("status", help="queue counts").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="check keys, connections, and setup").set_defaults(fn=cmd_doctor)

    rp = sub.add_parser("review", help="prefill queued jobs in visible tabs for manual finishing")
    rp.add_argument("--limit", type=int, default=10, help="maximum number of review jobs to open (default: 10)")
    rp.set_defaults(fn=cmd_review)

    pk = sub.add_parser("pick", help="interactively choose and apply to new postings since last apply")
    pk.add_argument("--limit", type=_positive_int, default=50, help="maximum postings to show (default: 50)")
    pk.add_argument("--from-date", type=_date_arg, help="earliest posting date, inclusive (YYYY-MM-DD)")
    pk.add_argument("--to-date", type=_date_arg, help="latest posting date, inclusive (YYYY-MM-DD)")
    pk.add_argument("--email-check", action=argparse.BooleanOptionalAction, default=None,
                    help="check email and cached confirmations before listing; overrides saved setting")
    pk.add_argument("--all", action="store_true", help="show all new postings, not just since last apply")
    pk.add_argument("--include-applied", action="store_true", help="include jobs that already have confirmation email")
    pk.add_argument("--list-only", action="store_true", help="only list postings without prompting to apply")
    pk.add_argument("--export", action="store_true", help="export the list to logs/new_jobs.md")
    pk.add_argument("--dry-run", action="store_true", help="fill everything, never submit")
    pk.add_argument("--mode", choices=["full", "hybrid", "fill_only"])
    pk.add_argument("--visible", action="store_true", help="show the browser window")
    pk.set_defaults(fn=cmd_pick)

    mp = sub.add_parser("mark", help="set job status manually")
    mp.add_argument("job_id", nargs="+")
    mp.add_argument("--status", default="submitted",
                    choices=["submitted", "needs_review", "skipped", "failed", "new"])
    mp.set_defaults(fn=cmd_mark)

    qp = sub.add_parser("requeue", help="put jobs of a status back in the queue")
    qp.add_argument("--status", default="failed")
    qp.set_defaults(fn=cmd_requeue)

    rsp = sub.add_parser(
        "resolve",
        help="record a user-confirmed outcome for unresolved application actions",
        description="Record a user-confirmed outcome for an unresolved final-submit attempt.",
    )
    rsp.add_argument("job_id")
    rsp.add_argument("--outcome", required=True, choices=["submitted", "not_submitted"],
                     help="the user-confirmed result of the final submit attempt")
    rsp.set_defaults(fn=cmd_resolve)

    acp = sub.add_parser("accounts", help="saved site accounts")
    acp.add_argument("action", choices=["list", "show"])
    acp.add_argument("tenant", nargs="?")
    acp.add_argument("--show-password", action="store_true", help="explicitly reveal the stored password")
    acp.set_defaults(fn=cmd_accounts)

    aup = sub.add_parser("auth", help="one-time logins")
    aup.add_argument("service", choices=["gmail"])
    aup.set_defaults(fn=cmd_auth)

    ivp = sub.add_parser("interview", help="interview user on unanswered questions to update RAG profile")
    ivp.add_argument("--job", nargs="+", help="specific job id(s) to interview for")
    ivp.add_argument("--reapply", action="store_true", help="automatically re-apply to unlocked jobs")
    ivp.set_defaults(fn=cmd_interview)

    a = p.parse_args(argv)
    _setup_logging(a.verbose)
    result = a.fn(a)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
