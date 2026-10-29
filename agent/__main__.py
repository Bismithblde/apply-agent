from __future__ import annotations

import argparse
import fcntl
import logging
import sys
import time
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
            c.execute(
                "INSERT INTO applications (job_id, run_id, status, reason, created_at, submitted_at) "
                "VALUES (?, NULL, 'submitted', ?, ?, ?)",
                (job_id, "manually confirmed submitted", now, now),
            )
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
    from . import prior_apps
    from .runner import apply_batch

    # Sync any fresh confirmation emails from Gmail if available
    try:
        prior_apps.sync_email_confirmations(max_results=50)
    except Exception as exc:
        log.debug("prior application sync skipped: %s", exc)

    last_apply_val = db.kv_get("last_apply")
    since_ts = float(last_apply_val) if last_apply_val and not a.all else None

    # Fetch a candidate pool to display
    pool_limit = max(a.limit * 3, 100)
    with db.connect() as c:
        if since_ts:
            candidates = c.execute(
                "SELECT id, company, title, ats, url, locations, first_seen, date_posted FROM jobs "
                "WHERE status='new' AND first_seen>=? ORDER BY first_seen DESC, company ASC LIMIT ?",
                (since_ts, pool_limit),
            ).fetchall()
        else:
            candidates = c.execute(
                "SELECT id, company, title, ats, url, locations, first_seen, date_posted FROM jobs "
                "WHERE status='new' ORDER BY first_seen DESC, company ASC LIMIT ?",
                (pool_limit,),
            ).fetchall()

    # Filter out jobs that have prior application confirmation in email for the purely same role
    rows = []
    excluded_count = 0
    for cand in candidates:
        if not a.include_applied:
            prior = prior_apps.find_prior_application(
                cand["company"],
                cand["title"],
                date_posted=cand["date_posted"],
            )
            if prior:
                excluded_count += 1
                continue
        rows.append(cand)
        if len(rows) >= a.limit:
            break

    if not rows:
        msg = "No new unapplied postings found"
        if since_ts:
            msg += f" since last apply ({time.strftime('%Y-%m-%d %H:%M', time.localtime(since_ts))})"
        if excluded_count:
            msg += f" ({excluded_count} were filtered because you already applied via email)"
        print(msg + " (use --all or --include-applied to inspect).")
        return 0

    header = f"Found {len(rows)} new posting(s)"
    if since_ts:
        header += f" since last apply ({time.strftime('%Y-%m-%d %H:%M', time.localtime(since_ts))})"
    if excluded_count:
        header += f" [{excluded_count} previously applied via email omitted]"
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
            if not db.has_uncertain_submit_intent(a.job_id):
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
    print(f"resolved {resolved} submit intent(s) for {a.job_id!r} as {a.outcome}")
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agent", description="Internship auto-applier")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch", help="pull listings from all sources").set_defaults(fn=cmd_fetch)

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
    pk.add_argument("--limit", type=int, default=50, help="maximum postings to show (default: 50)")
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
        help="record a user-confirmed outcome for an unresolved submit intent",
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
