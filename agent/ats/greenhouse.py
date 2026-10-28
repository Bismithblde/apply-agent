"""Greenhouse: boards.greenhouse.io, job-boards.greenhouse.io, and company pages embedding grnhse_iframe."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from ..browser import human_pause
from ..form import fill_form, finish_application, page_text
from ..models import ApplyResult, ApplyStatus, Job
from .base import ApplyContext

log = logging.getLogger(__name__)

_FORM_SEL = "#application-form, #application_form, form#application-form, input#first_name"
_CLOSED = re.compile(r"no longer (accepting|available|open)|job (is )?not found|position has been filled"
                     r"|this job (posting )?(has )?(been )?closed|couldn.?t find (the|that) job", re.I)


def board_and_id(url: str) -> tuple[str | None, str | None]:
    p = urlparse(url)
    qs = parse_qs(p.query)
    if "for" in qs and "token" in qs:
        return qs["for"][0], qs["token"][0]
    if "greenhouse.io" in p.netloc:
        m = re.search(r"^/([\w-]+)/jobs/(\d+)", p.path)
        if m:
            return m.group(1), m.group(2)
    if "gh_jid" in qs:
        return None, qs["gh_jid"][0]
    return None, None


def _find_scope(page, wait: float = 8.0):
    deadline = time.time() + wait
    while True:
        try:
            if page.locator(_FORM_SEL).count():
                return page.main_frame
        except Exception:
            pass
        for fr in page.frames[1:]:
            if fr.name == "grnhse_iframe" or "greenhouse.io/embed/job_app" in (fr.url or ""):
                try:
                    if fr.locator(_FORM_SEL).count():
                        return fr
                except Exception:
                    continue
        if time.time() > deadline:
            return None
        time.sleep(0.5)


class GreenhouseAdapter:
    name = "greenhouse"

    def can_handle(self, job: Job) -> bool:
        return job.ats == "greenhouse" or "gh_jid=" in job.url

    def apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        try:
            return self._apply(job, ctx)
        except Exception as e:
            log.exception("greenhouse apply failed for %s", job.url)
            return ApplyResult(ApplyStatus.FAILED, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                               screenshot=ctx.screenshot(job, "error"), final_url=ctx.page.url)

    def _apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        page = ctx.page
        board, jid = board_and_id(job.url)
        page.goto(job.url, wait_until="domcontentloaded")
        human_pause(1.0, 2.0)
        scope = _find_scope(page)
        if scope is None and board and jid:
            page.goto(f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={jid}",
                      wait_until="domcontentloaded")
            scope = _find_scope(page)
        if scope is None:
            from ..form import click_text
            if click_text(page, ["Apply", "Apply now", "Apply for this job", "Apply Now"]):
                human_pause(1.0, 2.0)
                scope = _find_scope(page)
        if scope is None:
            txt = page_text(page, 3000)
            shot = ctx.screenshot(job, "no_form")
            if _CLOSED.search(txt) or "error=true" in page.url:
                return ApplyResult(ApplyStatus.SKIPPED, "posting closed", screenshot=shot, final_url=page.url)
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "Greenhouse application form not found",
                               screenshot=shot, final_url=page.url)
        scope.locator(_FORM_SEL).first.scroll_into_view_if_needed(timeout=5000)
        job_text = page_text(page)
        fields, answers, failed = fill_form(scope, job, ctx.profile, job_text=job_text)
        if not fields:
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "no fields found", screenshot=ctx.screenshot(job, "empty"),
                               final_url=page.url)
        return finish_application(job, ctx, scope, fields, answers, failed,
                                  submit_texts=["Submit application", "Submit Application", "Submit"],
                                  success_url=r"/confirmation|application_confirmation")


ADAPTER = GreenhouseAdapter()
