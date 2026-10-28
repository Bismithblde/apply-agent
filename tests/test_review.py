from __future__ import annotations

import builtins
import fcntl
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from agent import __main__ as cli
from agent import answers, db, vault
from agent.ats import generic, registry, workday
from agent.ats.base import ApplyContext
from agent.ats.generic import Run as GenericRun, State as GenericState
from agent.models import Answer, ApplyResult, ApplyStatus, Job
import agent.browser as browser_module


class FakePage:
    url = "about:blank"

    def __init__(self):
        self.navigations: list[tuple[str, str]] = []

    def goto(self, url: str, *, wait_until: str):
        self.url = url
        self.navigations.append((url, wait_until))


class FakeBrowser:
    def __init__(self, *, fail_page_number: int | None = None):
        self.pages: list[FakePage] = []
        self.new_page_calls = 0
        self.fail_page_number = fail_page_number

    def new_page(self) -> FakePage:
        self.new_page_calls += 1
        if self.new_page_calls == self.fail_page_number:
            raise RuntimeError("tab creation failed")
        page = FakePage()
        self.pages.append(page)
        return page


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "apply.db")
    config = {
        "auto_ats": ["greenhouse"],
        "accounts": {"create_in_dry_run": True, "create_in_fill_only": True},
        "submit_mode": "full",
    }
    jobs = {
        job_id: Job(job_id, f"Company {job_id}", "Software Engineer Intern",
                    f"https://example.test/{job_id}", [], "Software", 1, ats="greenhouse")
        for job_id in ("one", "two", "three")
    }
    rows = [
        {"id": job.id, "company": job.company, "title": job.title, "url": job.url}
        for job in jobs.values()
    ]
    statuses = {job_id: "needs_review" for job_id in jobs}
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "load_profile", lambda: {"personal": {"name": "Test Applicant"}})
    monkeypatch.setattr(db, "jobs_by_status", lambda status: rows if status == "needs_review" else [])
    monkeypatch.setattr(db, "get_job", jobs.get)
    monkeypatch.setattr(db, "latest_application", lambda job_id: {"reason": "queued for review"})
    status_mutations: list[tuple] = []
    monkeypatch.setattr(db, "set_job_status", lambda *args, **kwargs: status_mutations.append(args))
    monkeypatch.setattr(db, "mark_job_in_progress", lambda *args, **kwargs: status_mutations.append(args))
    monkeypatch.setattr(db, "record_application", lambda *args, **kwargs: status_mutations.append(args))
    return config, jobs, rows, statuses, status_mutations


def _install_browser(monkeypatch, browser: FakeBrowser, *, on_open=None):
    @contextmanager
    def fake_open_browser(config, *, visible=False):
        if on_open:
            on_open(config, visible)
        yield browser

    monkeypatch.setattr(browser_module, "open_browser", fake_open_browser)


def test_review_uses_locked_safe_adapter_tabs_and_keeps_queue_unchanged(
        review_env, monkeypatch):
    config, jobs, _, statuses, status_mutations = review_env
    browser = FakeBrowser()
    seen_contexts = []
    account_creations = []
    adapter_errors = []

    class Adapter:
        name = "mock-greenhouse"

        def apply(self, job, ctx):
            seen_contexts.append((job.id, ctx, list(browser.pages)))
            try:
                assert ctx.dry_run is True
                assert ctx.submit_mode == "fill_only"
                assert ctx.run_id is None
                assert ctx.config["submit_mode"] == "fill_only"
                assert ctx.config["accounts"]["create_in_dry_run"] is False
                assert ctx.config["accounts"]["create_in_fill_only"] is False
                generic_run = GenericRun(job=job, ctx=ctx, page=ctx.page, deadline=0)
                state = GenericState(job.url, "", None, [], [], "")
                blocked = generic.ADAPTER._account(generic_run, state, "create_account")
                assert blocked.status == ApplyStatus.NEEDS_REVIEW
                with pytest.raises(workday._Stop):
                    workday._Run(job, ctx)._check_account_creation_allowed()
            except Exception as error:
                adapter_errors.append(error)
                raise
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "filled for manual finishing", fields_total=2)

    def check_open(review_config, visible):
        assert visible is True
        assert review_config["accounts"]["create_in_dry_run"] is False

        def record_account_creation(*args, **kwargs):
            account_creations.append((args, kwargs))
            raise RuntimeError("account creation attempted")

        monkeypatch.setattr(vault, "create", record_account_creation)
        with open(db.DB_PATH.with_name("apply.lock"), "r") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    adapter = Adapter()

    def resolve_adapter(job, auto_ats):
        assert job.ats == "greenhouse"
        assert auto_ats == config["auto_ats"]
        return adapter

    monkeypatch.setattr(registry, "adapter_for", resolve_adapter)
    _install_browser(monkeypatch, browser, on_open=check_open)

    prompts = []

    def wait_for_manual_finish(prompt):
        prompts.append((prompt, len(browser.pages)))
        return ""

    monkeypatch.setattr(builtins, "input", wait_for_manual_finish)
    cli.cmd_review(SimpleNamespace(limit=3))

    assert [job_id for job_id, _, _ in seen_contexts] == list(jobs)
    assert [len(pages) for _, _, pages in seen_contexts] == [1, 2, 3]
    assert [ctx.page for _, ctx, _ in seen_contexts] == browser.pages
    assert all(ctx.page in pages for _, ctx, pages in seen_contexts)
    assert len(browser.pages) == 3
    assert config["submit_mode"] == "full"
    assert config["accounts"]["create_in_dry_run"] is True
    assert config["accounts"]["create_in_fill_only"] is True
    assert statuses == {job_id: "needs_review" for job_id in jobs}
    assert status_mutations == []
    assert account_creations == []
    assert adapter_errors == []
    assert len(prompts) == 1 and prompts[0][1] == 3
    assert "python -m agent mark <job_id> --status submitted" in prompts[0][0]


def test_review_continues_after_tab_and_adapter_failures(review_env, monkeypatch, capsys):
    _, jobs, rows, _, status_mutations = review_env
    browser = FakeBrowser(fail_page_number=1)
    adapter_calls: list[str] = []

    class Adapter:
        name = "mock-greenhouse"

        def apply(self, job, ctx):
            adapter_calls.append(job.id)
            if job.id == "two":
                raise RuntimeError("adapter page failure")
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "ready", fields_total=1)

    monkeypatch.setattr(db, "jobs_by_status", lambda status: rows)
    monkeypatch.setattr(registry, "adapter_for", lambda job, auto_ats: Adapter())
    _install_browser(monkeypatch, browser)

    def wait_for_manual_finish(_prompt):
        assert len(browser.pages) == 2
        return ""

    monkeypatch.setattr(builtins, "input", wait_for_manual_finish)
    cli.cmd_review(SimpleNamespace(limit=3))

    output = capsys.readouterr().out
    assert "could not create tab" in output
    assert "adapter/page failure" in output
    assert "result: needs_review: ready" in output
    assert adapter_calls == ["two", "three"]
    assert len(browser.pages) == 2
    assert status_mutations == []


def test_review_does_not_open_browser_when_apply_lock_is_busy(review_env, monkeypatch, capsys):
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db.DB_PATH.with_name("apply.lock")
    monkeypatch.setattr(db, "jobs_by_status", lambda _status: pytest.fail("queue read while lock is busy"))

    def forbidden_open(_config, *, visible=False):
        pytest.fail("browser opened while apply lock was busy")

    monkeypatch.setattr(browser_module, "open_browser", forbidden_open)
    with open(lock_path, "w") as held_lock:
        fcntl.flock(held_lock, fcntl.LOCK_EX)
        cli.cmd_review(SimpleNamespace(limit=3))

    assert "another apply or review run" in capsys.readouterr().out


def test_review_prefills_local_greenhouse_form_without_submitting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "apply.db")
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.init()
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"local fixture resume")
    profile = {"resume_path": str(resume)}
    config = {
        "auto_ats": ["greenhouse"],
        "accounts": {"create_in_dry_run": True, "create_in_fill_only": True},
        "submit_mode": "full",
        "page_timeout_seconds": 3,
    }
    job = Job(
        "fixture:greenhouse-review", "Fixture Co", "Software Engineer Intern",
        "https://boards.greenhouse.test/acme/jobs/123", ["New York"], "Software", 1,
        source="test", ats="greenhouse",
    )
    db.upsert_jobs([job])
    db.set_job_status(job.id, "needs_review")
    db.record_application(job, ApplyResult(ApplyStatus.NEEDS_REVIEW, "local review fixture"), None)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "load_profile", lambda: profile)
    monkeypatch.setattr(ApplyContext, "screenshot", lambda self, job, tag: None)
    monkeypatch.setattr("agent.ats.greenhouse.human_pause", lambda *args: None)

    values = {
        "First Name": "Ada",
        "Last Name": "Lovelace",
        "Email": "ada@example.test",
        "Phone": "555-0100",
        "Resume/CV": str(resume),
        "Degree": "Bachelor's",
        "Are you legally authorized to work in the United States?": "Yes",
        "Languages (check all that apply)": ["English"],
        "School": "Stony Brook University",
        "Requesting visa sponsorship?": "No",
        "Education: Field of Study": "Computer Science",
        "Why do you want to join us?": "I want to build reliable software.",
        "I agree to the privacy policy": True,
    }

    def answer_local_fixture(fields, _job, _profile, **_kwargs):
        return [Answer(field.key, values[field.label], "profile") for field in fields if field.label in values]

    monkeypatch.setattr(answers, "answer_fields", answer_local_fixture)
    fixture_html = (Path(__file__).parent / "fixtures" / "form_basic.html").read_text()
    request_methods: list[str] = []
    observed: dict = {}

    @contextmanager
    def local_browser(review_config, *, visible=False):
        assert visible is True
        assert review_config["submit_mode"] == "fill_only"
        assert review_config["accounts"]["create_in_dry_run"] is False
        assert review_config["accounts"]["create_in_fill_only"] is False
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            observed["context"] = context

            def serve_fixture(route):
                request_methods.append(route.request.method)
                route.fulfill(status=200, body=fixture_html, content_type="text/html")

            context.route("https://boards.greenhouse.test/**", serve_fixture)
            try:
                yield context
            finally:
                context.close()
                browser.close()

    monkeypatch.setattr(browser_module, "open_browser", local_browser)

    def inspect_tabs(_prompt):
        pages = observed["context"].pages
        observed["pages_at_prompt"] = len(pages)
        if pages:
            page = pages[0]
            try:
                observed["url"] = page.url
                observed["fields"] = {
                    "first_name": page.input_value("#first_name"),
                    "last_name": page.input_value("#last_name"),
                    "email": page.input_value("#email"),
                    "phone": page.input_value("#phone"),
                    "school": page.input_value("#school"),
                    "degree": page.eval_on_selector("#degree", "e => e.options[e.selectedIndex].text"),
                    "authorized": page.is_checked("input[name=auth][value=y]"),
                    "sponsorship": page.get_attribute(".yesno button:nth-child(2)", "aria-pressed"),
                    "resume_files": page.eval_on_selector("#resume", "e => e.files.length"),
                    "submitted": page.evaluate("window.__submitted === true"),
                }
                observed["status"] = db.get_job_status(job.id)
            except Exception as error:
                observed["inspection_error"] = f"{type(error).__name__}: {error}"
        return ""
    monkeypatch.setattr(builtins, "input", inspect_tabs)

    cli.cmd_review(SimpleNamespace(limit=1))

    output = capsys.readouterr().out
    assert "adapter: greenhouse" in output
    assert "result: needs_review: fill_only mode" in output
    assert "filled fields: 13" in output
    assert observed["pages_at_prompt"] == 1
    assert "inspection_error" not in observed
    assert observed["url"] == job.url
    assert observed["fields"] == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.test",
        "phone": "555-0100",
        "school": "Stony Brook University",
        "degree": "Bachelor's Degree",
        "authorized": True,
        "sponsorship": "true",
        "resume_files": 1,
        "submitted": False,
    }
    assert "POST" not in request_methods
    assert observed["status"] == "needs_review"
    assert db.get_job_status(job.id) == "needs_review"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications WHERE job_id=?", (job.id,)).fetchone()[0] == 1
