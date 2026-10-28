"""Ashby: jobs.ashbyhq.com/<company>/<id>, form on the Application tab."""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

from ..browser import human_pause
from ..form import click_text, fill_form, finish_application, page_text
from ..models import ApplyResult, ApplyStatus, Job
from .base import ApplyContext

log = logging.getLogger(__name__)

_FORM_SEL = ".ashby-application-form-container, #_systemfield_name, input[name=_systemfield_email]"
_CLOSED = re.compile(r"job not found|no longer (accepting|available|open)|posting (is )?(closed|unavailable)", re.I)


def posting_url(url: str) -> str:
    p = urlparse(url)
    qs = parse_qs(p.query)
    if "ashbyhq.com" not in p.netloc and "ashby_jid" in qs:
        return url
    return re.sub(r"/application/?$", "", f"{p.scheme or 'https'}://{p.netloc}{p.path}".rstrip("/"))


class AshbyAdapter:
    name = "ashby"

    def can_handle(self, job: Job) -> bool:
        return job.ats == "ashby" or "ashby_jid=" in job.url

    def apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        try:
            return self._apply(job, ctx)
        except Exception as e:
            log.exception("ashby apply failed for %s", job.url)
            return ApplyResult(ApplyStatus.FAILED, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                               screenshot=ctx.screenshot(job, "error"), final_url=ctx.page.url)

    def _apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        page = ctx.page
        url = posting_url(job.url)
        page.goto(url, wait_until="domcontentloaded")
        human_pause(1.5, 2.5)
        job_text = page_text(page)
        form = page.locator(_FORM_SEL).first
        if not form.count():
            if click_text(page, ["Application"], roles=("tab", "link", "button")):
                human_pause(1.0, 2.0)
        try:
            form.wait_for(timeout=8000)
        except Exception:
            if "ashbyhq.com" in url:
                page.goto(url + "/application", wait_until="domcontentloaded")
                human_pause(1.5, 2.5)
        try:
            form.wait_for(timeout=12000)
        except Exception:
            shot = ctx.screenshot(job, "no_form")
            if _CLOSED.search(page_text(page, 3000)):
                return ApplyResult(ApplyStatus.SKIPPED, "posting closed", screenshot=shot, final_url=page.url)
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "Ashby application form not found",
                               screenshot=shot, final_url=page.url)
        scope = page.main_frame
        fields, answers, failed = fill_form(scope, job, ctx.profile, job_text=job_text)
        if not fields:
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "no fields found", screenshot=ctx.screenshot(job, "empty"),
                               final_url=page.url)
        return finish_application(job, ctx, scope, fields, answers, failed,
                                  submit_texts=["Submit Application", "Submit application", "Submit"])


ADAPTER = AshbyAdapter()
