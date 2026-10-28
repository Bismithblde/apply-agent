from __future__ import annotations

import fcntl
import time

import pytest

from agent import db
from agent.filters import filter_jobs, is_us, keep, policy_fingerprint
from agent.models import ApplyResult, ApplyStatus, Job
from agent.sources import ats_boards, github_lists, simplify

CONFIG = {
    "filters": {
        "terms": ["Summer 2027"],
        "us_only": True,
        "software_categories": ["Software", "Software Engineering"],
        "ai_categories": ["AI/ML/Data", "Data Science, AI & Machine Learning"],
        "ai_title_keywords": ["machine learning", " ml ", "ml ", " ai ", "ai ", "ai/", "llm", "research engineer",
                              "computer vision"],
        "exclude_title_keywords": ["phd", "master", "analyst", "sales", "hardware", "co-op", "fall 2027", "winter"],
    }
}


def job(title, category="", locations=("San Francisco, CA",), terms=("Summer 2027",), url=None, company="Acme", source="test"):
    j = Job(id=f"{source}:{title}", company=company, title=title, url=url or f"https://example.com/{title}",
            locations=list(locations), category=category, date_posted=0, source=source)
    if terms is not None:
        j.terms = terms
    return j


# ---------- filters ----------

def test_software_category_passes():
    assert keep(job("Software Engineer Intern", "Software", terms=["Summer 2027"]), CONFIG) == (True, "ok")


def test_ai_category_needs_ai_title():
    assert keep(job("Machine Learning Engineer Intern", "AI/ML/Data"), CONFIG)[0]
    assert keep(job("AI/ML Developer Intern", "AI/ML/Data"), CONFIG)[0]
    ok, reason = keep(job("Data Engineering Intern", "AI/ML/Data"), CONFIG)
    assert not ok and "AI" in reason


def test_ai_keywords_are_word_bounded():
    assert not keep(job("HTML Email Intern", "AI/ML/Data"), CONFIG)[0]
    assert not keep(job("Dubai Operations Intern", "AI/ML/Data"), CONFIG)[0]


def test_uncategorized_uses_title_heuristics():
    assert keep(job("Backend Developer Intern"), CONFIG)[0]
    assert keep(job("iOS Intern"), CONFIG)[0]
    assert keep(job("Research Engineer Intern, LLM"), CONFIG)[0]
    assert not keep(job("Product Design Intern"), CONFIG)[0]


def test_other_categories_need_swe_title():
    assert keep(job("Software Engineer Intern", "Quant"), CONFIG)[0]
    assert not keep(job("Quant Trader Intern", "Quant"), CONFIG)[0]


@pytest.mark.parametrize("title", [
    "PhD Software Engineer Intern", "Software Engineer Intern (Masters)", "Data Analyst Intern",
    "Hardware Engineer Intern", "Software Engineering Co-op Intern", "Software Intern - Fall 2027",
])
def test_excluded_titles(title):
    ok, reason = keep(job(title, "Software"), CONFIG)
    assert not ok and reason.startswith("excluded")


def test_salesforce_is_not_sales():
    assert keep(job("Salesforce Developer Intern"), CONFIG)[0]


def test_must_be_intern():
    assert keep(job("Software Engineer", "Software"), CONFIG) == (False, "not an internship")
    assert not keep(job("Software Engineer, Internal Tools", "Software"), CONFIG)[0]
    assert keep(job("Software Engineering Internship"), CONFIG)[0]


def test_terms():
    assert not keep(job("Software Engineer Intern", "Software", terms=["Summer 2026"]), CONFIG)[0]
    assert keep(job("Software Engineer Intern", "Software", terms=["Spring 2027", "Summer 2027"]), CONFIG)[0]
    assert not keep(job("Software Engineer Intern - Summer 2026"), CONFIG)[0]
    assert keep(job("Software Engineer Intern - Summer 2027"), CONFIG)[0]
    assert not keep(job("Software Engineer Intern - Grad Year: Spring 2028", terms=None), CONFIG)[0]
    assert keep(job("Software Engineer Intern (expected to graduate in Summer 2027)", terms=None), CONFIG)[1] == "term unknown"
    assert keep(job("Software Engineer Intern - Summer 2027", terms=["Winter 2027"]), CONFIG)[1] == "term unknown"


def test_policy_fingerprint_tracks_rules_and_revision(monkeypatch):
    baseline = policy_fingerprint(CONFIG)
    changed = {**CONFIG, "filters": {**CONFIG["filters"], "us_only": False}}
    assert policy_fingerprint(changed) != baseline
    monkeypatch.setattr("agent.filters.FILTER_POLICY_REVISION", 2)
    assert policy_fingerprint(CONFIG) != baseline


@pytest.mark.parametrize("locs,expected", [
    (["NYC"], True), (["Remote in USA"], True), (["United States"], True), (["Austin, TX"], True),
    (["Remote"], False), (["California"], True), (["Washington, D.C."], True), ([], False),
    (["Toronto, ON, Canada"], False), (["London, UK"], False), (["Bengaluru, Karnataka, India"], False),
    (["Remote in Canada"], False), (["Vancouver, BC"], False), (["Dublin"], False),
    (["London, UK", "Seattle, WA"], True), (["Remote (US)"], True), (["Remote, Canada"], False),
])
def test_us_only(locs, expected):
    assert is_us(locs) is expected


def test_us_scope_requires_evidence_or_explicit_source_context():
    assert is_us(["Remote"], us_context=True)
    assert not is_us(["London, UK"], us_context=True)


def test_non_us_job_dropped():
    ok, reason = keep(job("Software Engineer Intern", "Software", locations=["Toronto, ON, Canada"]), CONFIG)
    assert (ok, reason) == (False, "not in the US")


def test_filter_jobs():
    jobs = [job("Software Engineer Intern"), job("Sales Intern"), job("Software Engineer Intern 2", locations=["UK"])]
    assert [j.title for j in filter_jobs(jobs, CONFIG)] == ["Software Engineer Intern"]


# ---------- README parsing ----------

VANSH_MD = """
| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| Skydio | Autonomy Engineer Intern 🛂 | San Mateo, CA | <a href="https://jobs.ashbyhq.com/skydio/1"><img src="https://i.imgur.com/x.png" alt="Apply"></a> | Oct 25 |
| ↳ | Software Engineer Intern | Remote</br>United States | <a href="https://jobs.ashbyhq.com/skydio/2"><img src="https://i.imgur.com/x.png" alt="Apply"></a> | Oct 24 |
| ↳ | Closed Intern | San Mateo, CA | 🔒 | Oct 23 |
| **[MongoDB](https://mongodb.com)** | Software Engineer Intern | <details><summary>**2 locations**</summary>Palo Alto, CA</br>Seattle, WA</details> | <a href="https://boards.greenhouse.io/mongodb/jobs/3?utm_source=x">Apply</a> | Oct 23 |
| ↳ | SWE Intern \\| Infra | Austin, TX | <a href="https://jobs.lever.co/mongodb/4">Apply</a> | Oct 22 |
"""

SPEEDY_MD = """
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://www.amazon.com"><strong>Amazon</strong></a> | SDE Intern | Seattle, WA +2 | $53/hr | <a href="https://www.amazon.jobs/jobs/1/apply"><img src="https://i.imgur.com/J.png" alt="Apply" width="70"/></a> | 3d |

| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://astrolab.space"><strong>Astrolab</strong></a> | Software Engineering Intern | Hawthorne, CA | <a href="https://astrolab.pinpointhq.com/p/1"><img src="https://i.imgur.com/J.png" alt="Apply"/></a> | 0d |
"""

HTML_TABLE = """
<table><thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr></thead>
<tbody>
<tr><td><strong><a href="https://simplify.jobs/c/Stripe">Stripe</a></strong></td><td>Software Engineer Intern</td>
<td>SF</td><td><a href="https://stripe.com/jobs/1?utm_source=Simplify"><img alt="Apply"></a>
<a href="https://simplify.jobs/p/abc"><img alt="Simplify"></a></td><td>2d</td></tr>
<tr><td>↳</td><td>ML Intern</td><td>NYC</td><td>🔒</td><td>5d</td></tr>
<tr><td>↳</td><td>Backend Intern</td><td>NYC</td><td><a href="https://stripe.com/jobs/2">Apply</a></td><td>1w</td></tr>
</tbody></table>
"""


def test_parse_markdown_with_continuations_and_closed_rows():
    now = time.mktime((2026, 10, 26, 12, 0, 0, 0, 0, -1))
    jobs = github_lists.parse_readme(VANSH_MD, "vanshb03", now=now)
    assert [(j.company, j.title) for j in jobs] == [
        ("Skydio", "Autonomy Engineer Intern"), ("Skydio", "Software Engineer Intern"),
        ("MongoDB", "Software Engineer Intern"), ("MongoDB", "SWE Intern | Infra"),
    ]
    assert jobs[1].locations == ["Remote", "United States"]
    assert jobs[2].locations == ["Palo Alto, CA", "Seattle, WA"]
    assert [j.ats for j in jobs] == ["ashby", "ashby", "greenhouse", "lever"]
    assert jobs[0].id.startswith("vanshb03:") and len(jobs[0].id) == len("vanshb03:") + 16
    assert time.localtime(jobs[0].date_posted)[:3] == (2026, 10, 25)


def test_parse_speedyapply_multiple_tables():
    now = 1_800_000_000
    jobs = github_lists.parse_readme(SPEEDY_MD, "speedyapply", now=now)
    assert [(j.company, j.title, j.locations) for j in jobs] == [
        ("Amazon", "SDE Intern", ["Seattle, WA"]),
        ("Astrolab", "Software Engineering Intern", ["Hawthorne, CA"]),
    ]
    assert jobs[0].url == "https://www.amazon.jobs/jobs/1/apply"
    assert jobs[0].date_posted == now - 3 * 86400


def test_parse_html_table_prefers_direct_link():
    jobs = github_lists.parse_readme(HTML_TABLE, "x", now=1_800_000_000)
    assert [(j.company, j.title) for j in jobs] == [("Stripe", "Software Engineer Intern"), ("Stripe", "Backend Intern")]
    assert jobs[0].url.startswith("https://stripe.com/jobs/1")
    assert jobs[1].date_posted == 1_800_000_000 - 7 * 86400


def test_readme_heading_context_establishes_only_explicit_term_and_us_scope():
    text = """
# Summer 2027 Tech Internships
## USA SWE Internships
| Company | Role | Location | Application |
|---|---|---|---|
| Acme | Software Engineer Intern | Remote | <a href="https://example.com/us">Apply</a> |
## International SWE Internships
| Company | Role | Location | Application |
|---|---|---|---|
| Other | Software Engineer Intern | London, UK | <a href="https://example.com/uk">Apply</a> |
## US and Canada SWE Internships
| Company | Role | Location | Application |
|---|---|---|---|
| Mixed | Software Engineer Intern | Remote | <a href="https://example.com/mixed">Apply</a> |
## US and UK SWE Internships
| Company | Role | Location | Application |
|---|---|---|---|
| Also Mixed | Software Engineer Intern | Remote | <a href="https://example.com/mixed-uk">Apply</a> |
"""
    us_job, international_job, mixed_job, mixed_uk_job = github_lists.parse_readme(text, "fixture")
    assert us_job.terms == ["Summer 2027"] and us_job.location_context_us
    assert keep(us_job, CONFIG) == (True, "ok")
    assert international_job.terms == ["Summer 2027"] and not international_job.location_context_us
    assert keep(international_job, CONFIG) == (False, "not in the US")
    assert not mixed_job.location_context_us
    assert keep(mixed_job, CONFIG) == (False, "US location unknown")
    assert not mixed_uk_job.location_context_us
    assert keep(mixed_uk_job, CONFIG) == (False, "US location unknown")


def test_ids_are_stable_across_tracking_params():
    a = github_lists.parse_readme(VANSH_MD, "vanshb03")[2]
    b = github_lists.parse_readme(VANSH_MD.replace("?utm_source=x", ""), "vanshb03")[2]
    assert a.id == b.id


# ---------- simplify mapping ----------

def test_simplify_to_jobs():
    now = 1_800_000_000
    base = {"source": "Simplify", "category": "Software", "company_name": "Acme", "title": "SWE Intern",
            "url": "https://boards.greenhouse.io/acme/jobs/1", "locations": ["NYC"], "terms": ["Summer 2027"],
            "date_posted": now - 86400, "active": True, "is_visible": True}
    listings = [
        {**base, "id": "a"},
        {**base, "id": "b", "active": False},
        {**base, "id": "c", "is_visible": False},
        {**base, "id": "d", "date_posted": now - 200 * 86400},
        {**base, "id": "e", "terms": ["N/A"]},
    ]
    jobs = simplify.to_jobs(listings, max_age_days=120, now=now)
    assert [j.id for j in jobs] == ["simplify:a", "simplify:e"]
    assert jobs[0].terms == ["Summer 2027"] and jobs[1].terms == []
    assert jobs[0].ats == "greenhouse" and jobs[0].company == "Acme"


# ---------- ATS boards ----------

class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeClient:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url):
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return FakeResponse(404, {})


def test_ats_boards_keep_interns_only():
    c = FakeClient({
        "greenhouse.io/v1/boards/acme": FakeResponse(200, {"jobs": [
            {"id": 1, "title": "Software Engineer, Intern", "location": {"name": "SF; New York, NY"},
             "absolute_url": "https://acme.com/jobs?gh_jid=1", "first_published": "2026-10-01T00:00:00-04:00"},
            {"id": 2, "title": "Internal Tools Engineer", "location": {"name": "SF"}},
        ]}),
        "lever.co/v0/postings/acme": FakeResponse(200, [{
            "id": "remote", "text": "Software Engineer Intern", "categories": {"location": "Remote"},
            "country": "US", "descriptionPlain": "Summer 2027 software internship",
            "hostedUrl": "https://jobs.lever.co/acme/remote", "createdAt": 0,
        }, {
            "id": "grad", "text": "Software Engineer Intern", "categories": {"location": "Remote"},
            "country": "US", "descriptionPlain": "Summer 2027 interns are expected to graduate in this year",
            "hostedUrl": "https://jobs.lever.co/acme/grad", "createdAt": 0,
        }]),
    })
    jobs = ats_boards.greenhouse(c, "acme")
    assert len(jobs) == 1
    assert jobs[0].url == "https://job-boards.greenhouse.io/acme/jobs/1"
    assert jobs[0].ats == "greenhouse" and jobs[0].locations == ["SF", "New York, NY"]
    lever_jobs = ats_boards.lever(c, "acme", ["Summer 2027"])
    assert lever_jobs[0].locations == ["Remote", "United States"]
    assert lever_jobs[0].terms == ["Summer 2027"]
    assert keep(lever_jobs[0], CONFIG)[0]
    assert lever_jobs[1].terms == []
    assert keep(lever_jobs[1], CONFIG)[1] == "term unknown"
    with pytest.raises(ats_boards.BoardNotFound):
        ats_boards.lever(c, "missing")


def test_ashby_locations_include_country():
    c = FakeClient({"job-board/acme": FakeResponse(200, {"jobs": [
        {"id": "x", "title": "Software Engineering Intern", "location": "San Francisco", "isListed": True,
         "address": {"postalAddress": {"addressCountry": "United States"}},
         "secondaryLocations": [{"location": "Remote (Canada)", "address": {"postalAddress": {"addressCountry": "Canada"}}}],
         "jobUrl": "https://jobs.ashbyhq.com/acme/x", "publishedAt": "2026-10-01T00:00:00+00:00",
         "descriptionPlain": "Summer 2027 software engineering internship"},
        {"id": "y", "title": "Design Intern", "location": "SF", "isListed": False},
    ]})})
    jobs = ats_boards.ashby(c, "acme", ["Summer 2027"])
    assert [j.locations for j in jobs] == [["San Francisco, United States", "Remote (Canada)"]]
    assert jobs[0].terms == ["Summer 2027"]


# ---------- dedupe through the DB ----------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    return tmp_path / "test.db"


def test_upsert_dedupes_by_url_and_company_title(temp_db):
    a = job("Software Engineer Intern", url="https://boards.greenhouse.io/acme/jobs/1", source="simplify")
    same_url = job("SWE Intern (renamed)", url="https://boards.greenhouse.io/acme/jobs/1/?utm_source=x",
                   source="vanshb03")
    same_title = job("software engineer  intern", url="https://acme.com/careers/1", source="speedyapply",
                     company="ACME")
    other = job("Backend Intern", url="https://boards.greenhouse.io/acme/jobs/2", source="simplify")
    assert db.upsert_jobs([a, same_url, same_title, other]) == 2
    assert db.upsert_jobs([a, other]) == 0
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_fetch_all_isolates_failures(temp_db, monkeypatch):
    from agent import fetch

    def boom(config):
        raise RuntimeError("network down")

    monkeypatch.setattr(fetch, "SOURCES", [
        ("simplify", boom),
        ("vanshb03", lambda config: [job("Software Engineer Intern", source="vanshb03"), job("Sales Intern")]),
    ])
    config = {**CONFIG, "sources": {"simplify": {"enabled": True}, "vanshb03": {"enabled": True},
                                    "speedyapply": {"enabled": False}}}
    stats = fetch.fetch_all(config)
    assert (stats["fetched"], stats["matched"], stats["new"]) == (2, 1, 1)
    assert "network down" in stats["by_source"]["simplify"]["error"]
    assert stats["by_source"]["vanshb03"]["new"] == 1
    assert fetch.fetch_all(config)["new"] == 0


def test_fetch_lock_contention_defers_scheduler_without_changing_queue(temp_db, monkeypatch):
    from agent import fetch, scheduler

    pending = job("Software Engineer Intern", locations=[], terms=None, source="fixture")
    verified = job("Backend Intern", company="Other", source="fixture")
    db.upsert_jobs([pending])
    db.kv_set("last_fetch", "123")
    db.kv_set(f"filter_policy:{pending.id}", "old-policy")
    db.kv_set(f"filter_evidence:{pending.id}", "old-evidence")
    lock_path = temp_db.with_name("apply.lock")
    calls = []

    def assert_locked():
        with open(lock_path, "r+") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def source(config):
        assert_locked()
        calls.append(True)
        return [pending, verified]

    upsert = db.upsert_jobs

    def checked_upsert(jobs):
        assert_locked()
        return upsert(jobs)

    monkeypatch.setattr(db, "upsert_jobs", checked_upsert)
    monkeypatch.setattr(fetch, "SOURCES", [("fixture", source)])
    config = {**CONFIG, "sources": {"fixture": {"enabled": True}}}

    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(fetch.FetchLockBusy, match="fetch deferred"):
            scheduler.fetch_once(config)
        assert calls == []
        assert db.get_job_status(pending.id) == "new"
        assert db.get_job_status(verified.id) is None
        assert db.latest_application(pending.id) is None
        assert db.kv_get(f"filter_policy:{pending.id}") == "old-policy"
        assert db.kv_get(f"filter_evidence:{pending.id}") == "old-evidence"
        assert db.kv_get("last_fetch") == "123"

    stats = scheduler.fetch_once(config)
    assert calls == [True]
    assert stats["fetched"] == 2 and stats["held_for_review"] == 1
    assert db.get_job_status(pending.id) == "needs_review"
    assert db.get_job_status(verified.id) == "new"
    assert db.kv_get(f"filter_policy:{verified.id}") == policy_fingerprint(config)
    assert db.kv_get("last_fetch") != "123"
    with open(lock_path, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_fetch_holds_unknown_evidence_for_review(temp_db, monkeypatch):
    from agent import fetch

    unknown = job("Software Engineer Intern", locations=[], terms=None, source="unknown-source")
    excluded = job("Software Engineer Intern", locations=["Toronto, Canada"], terms=["Summer 2026"],
                   source="excluded-source", company="Other")
    monkeypatch.setattr(fetch, "SOURCES", [("unknown-source", lambda config: [unknown, excluded])])
    config = {**CONFIG, "sources": {"unknown-source": {"enabled": True}}}
    stats = fetch.fetch_all(config)
    assert stats["matched"] == 0 and stats["held_for_review"] == 1
    assert stats["by_source"]["unknown-source"]["excluded"] == 1
    assert db.get_job_status(unknown.id) == "needs_review"
    assert db.get_job_status(excluded.id) is None
    assert db.next_jobs(10) == []
    fetch.fetch_all(config)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM applications WHERE job_id=?", (unknown.id,)).fetchone()[0] == 1


def test_verified_us_posting_cannot_promote_same_title_at_different_url(temp_db, monkeypatch):
    from agent import fetch

    foreign_unknown = job("Software Engineer Intern", locations=[], terms=None, url="https://example.com/foreign",
                          company="Acme", source="old-source")
    config = {**CONFIG, "sources": {"old-source": {"enabled": True}, "new-source": {"enabled": True}}}
    monkeypatch.setattr(fetch, "SOURCES", [("old-source", lambda config: [foreign_unknown])])
    fetch.fetch_all(config)
    assert db.get_job_status(foreign_unknown.id) == "needs_review"
    previous_reason = db.latest_application(foreign_unknown.id)["reason"]

    verified_us = job("Software Engineer Intern", locations=["Seattle, WA"], terms=["Summer 2027"],
                      url="https://example.com/us", company="Acme", source="new-source")
    monkeypatch.setattr(fetch, "SOURCES", [("new-source", lambda config: [verified_us])])
    stats = fetch.fetch_all(config)

    assert db.get_job_status(foreign_unknown.id) == "needs_review"
    assert db.latest_application(foreign_unknown.id)["reason"] == previous_reason
    assert db.get_job_status(verified_us.id) is None
    assert stats["by_source"]["new-source"]["unrepresented_verified"] == 1
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM kv WHERE key=?", (f"filter_policy:{foreign_unknown.id}",)).fetchone() is None
        assert c.execute("SELECT 1 FROM kv WHERE key=?", (f"filter_attestation_orphan:{verified_us.id}",)).fetchone()


def test_same_url_verified_source_wins_over_unknown_duplicate_regardless_of_order(temp_db, monkeypatch):
    from agent import fetch

    weak = job("Software Engineer Intern", locations=["Remote"], terms=None, url="https://example.com/same",
               company="Acme", source="weak-source")
    verified = job("Software Engineer Intern", locations=["Seattle, WA"], terms=["Summer 2027"],
                   url="https://example.com/same", company="Acme", source="verified-source")
    monkeypatch.setattr(fetch, "SOURCES", [
        ("weak-source", lambda config: [weak]),
        ("verified-source", lambda config: [verified]),
    ])
    config = {**CONFIG, "sources": {"weak-source": {"enabled": True}, "verified-source": {"enabled": True}}}

    stats = fetch.fetch_all(config)

    assert stats["held_for_review"] == 0
    assert stats["by_source"]["weak-source"]["suppressed_by_verified"] == 1
    assert db.get_job_status(verified.id) == "new"
    assert db.latest_application(verified.id) is None


def test_unknown_source_does_not_replace_unrelated_review_reason(temp_db, monkeypatch):
    from agent import fetch

    pending = job("Software Engineer Intern", locations=[], terms=None, source="same-source")
    db.upsert_jobs([pending])
    db.set_job_status(pending.id, "needs_review")
    db.record_application(pending, ApplyResult(ApplyStatus.NEEDS_REVIEW, "unresolved account issue"), None)
    monkeypatch.setattr(fetch, "SOURCES", [("same-source", lambda config: [pending])])
    config = {**CONFIG, "sources": {"same-source": {"enabled": True}}}

    fetch.fetch_all(config)

    assert db.latest_application(pending.id)["reason"] == "unresolved account issue"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM applications WHERE job_id=?", (pending.id,)).fetchone()[0] == 1


def test_verified_posting_does_not_clear_hold_with_unresolved_submit_intent(temp_db, monkeypatch):
    from agent import fetch

    pending = job("Software Engineer Intern", locations=[], terms=None, url="https://example.com/job",
                  source="same-source")
    config = {**CONFIG, "sources": {"same-source": {"enabled": True}}}
    monkeypatch.setattr(fetch, "SOURCES", [("same-source", lambda config: [pending])])
    fetch.fetch_all(config)
    run_id = db.start_run("apply")
    with db.connect() as c:
        c.execute("INSERT INTO submit_intents (job_id, run_id, created_at) VALUES (?, ?, ?)",
                  (pending.id, run_id, time.time()))

    verified = job("Software Engineer Intern", locations=["Seattle, WA"], terms=["Summer 2027"],
                   url="https://example.com/job", source="same-source")
    monkeypatch.setattr(fetch, "SOURCES", [("same-source", lambda config: [verified])])
    fetch.fetch_all(config)

    assert db.get_job_status(pending.id) == "needs_review"
    assert db.latest_application(pending.id)["reason"] == "source filter: term unknown"
