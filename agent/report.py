from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from . import db
from .config import LOG_DIR


def build(since: float | None = None) -> tuple[Path, str]:
    """Write logs/report-YYYY-MM-DD.md covering everything since `since` (default: tonight's 6 PM).
    Returns the path and a one-line summary."""
    since = since or db.night_start()
    rows = db.applications_since(since)
    latest: dict[str, object] = {}
    for r in rows:
        latest[r["job_id"]] = r
    apps_by_id = {r["id"]: r for r in latest.values()}
    for r in rows:
        if r["status"] == "submitted" and r["reason"] == db.RESOLVED_SUBMIT_INTENT_REASON:
            apps_by_id[r["id"]] = r
    apps = sorted(apps_by_id.values(), key=lambda r: r["created_at"])
    by_status: dict[str, list] = {}
    for r in apps:
        by_status.setdefault(r["status"], []).append(r)
    accounts = db.accounts_created_since(since)
    spend = db.captcha_spend_since(since)
    counts = db.status_counts()

    def line(r) -> str:
        shot = f" ([screenshot]({r['screenshot']}))" if r["screenshot"] else ""
        return f"- **{r['company']}**: [{r['title']}]({r['url']}) `{r['ats']}`{shot}"

    out = [f"# Apply report {time.strftime('%Y-%m-%d %H:%M')}", "",
           f"Since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}. "
           f"CAPTCHA spend: ${spend:.2f}. Accounts created: {len(accounts)}.", "",
           "Queue totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), ""]

    submitted = by_status.get("submitted", [])
    out += [f"## Submitted ({len(submitted)})", ""]
    for r in submitted:
        out.append(line(r))
        if r["reason"] == db.RESOLVED_SUBMIT_INTENT_REASON:
            clicked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))
            out.append(f"  - User-confirmed after apply run {r['run_id']} (original click: {clicked_at})")
        gen = json.loads(r["generated_json"] or "[]")
        if gen:
            answers = {a["key"]: a for a in json.loads(r["answers_json"] or "[]")}
            gen_vals = [a for a in answers.values() if a.get("source") == "generated"]
            for a in gen_vals[:6]:
                out.append(f"  - generated `{a['key']}`: {str(a['value'])[:300]}")
    out.append("")

    review = by_status.get("needs_review", [])
    reasons = Counter(r["reason"].split(":")[0] for r in review)
    out += [f"## Needs your review ({len(review)})", "",
            "Top reasons: " + ", ".join(f"{k} ({v})" for k, v in reasons.most_common(6)), ""]
    out += [line(r) + f": {r['reason']}" for r in review]
    out.append("")

    failed = by_status.get("failed", [])
    out += [f"## Failed ({len(failed)})", ""] + [line(r) + f": {r['reason']}" for r in failed] + [""]

    dry = by_status.get("dry_run_ok", [])
    if dry:
        out += [f"## Dry runs that would have submitted ({len(dry)})", ""] + [line(r) for r in dry] + [""]

    if accounts:
        out += ["## Accounts created", ""] + [f"- {a['company'] or a['tenant_key']} ({a['email']})"
                                             f"{' verified' if a['verified'] else ''}" for a in accounts] + [""]

    path = LOG_DIR / f"report-{time.strftime('%Y-%m-%d')}.md"
    path.write_text("\n".join(out))
    summary = (f"{len(submitted)} submitted, {len(review)} need review, {len(failed)} failed, "
               f"{len(accounts)} accounts, ${spend:.2f} captcha")
    return path, summary
