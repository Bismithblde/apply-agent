"""Lever: jobs.lever.co/<company>/<id>/apply."""
from __future__ import annotations

import logging
import re
import time

from ..browser import human_pause
from ..form import fill_form, finish_application, page_text
from ..models import ApplyResult, ApplyStatus, Job
from .base import ApplyContext

log = logging.getLogger(__name__)

_CLOSED = re.compile(r"couldn.?t find anything here|no longer (accepting|available|open)|posting (has been )?closed", re.I)


def apply_url(url: str) -> str:
    base = re.sub(r"/apply/?$", "", url.split("?")[0].split("#")[0].rstrip("/"))
    return base + "/apply"


def _pick_location(page, fields, answers) -> None:
    """Lever's location box needs a suggestion picked from its dropdown, not just typed text."""
    loc = page.locator("input#location-input, input[name=location]").first
    if not loc.count():
        return
    try:
        if page.locator("#selected-location").count() and page.locator("#selected-location").input_value():
            return
        text = loc.input_value()
        if not text:
            return
        loc.click(timeout=3000)
        loc.press("End")
        loc.press("Backspace")
        loc.press_sequentially(text[-1], delay=60)
        opt = page.locator(".dropdown-results > div, .dropdown-location").first
        opt.wait_for(state="visible", timeout=6000)
        opt.click(timeout=3000)
        time.sleep(0.3)
    except Exception as e:
        log.info("lever location pick failed: %s", str(e).splitlines()[0][:120])


class LeverAdapter:
    name = "lever"

    def can_handle(self, job: Job) -> bool:
        return job.ats == "lever"

    def apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        try:
            return self._apply(job, ctx)
        except Exception as e:
            log.exception("lever apply failed for %s", job.url)
            return ApplyResult(ApplyStatus.FAILED, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                               screenshot=ctx.screenshot(job, "error"), final_url=ctx.page.url)

    def _apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        page = ctx.page
        base = apply_url(job.url)[: -len("/apply")]
        page.goto(base, wait_until="domcontentloaded")
        human_pause(0.8, 1.6)
        job_text = page_text(page)
        page.goto(apply_url(job.url), wait_until="domcontentloaded")
        human_pause(1.0, 2.0)
        try:
            page.locator("form#application-form, form[action*='apply'], input[name=email]").first.wait_for(timeout=10000)
        except Exception:
            shot = ctx.screenshot(job, "no_form")
            if _CLOSED.search(page_text(page, 3000)):
                return ApplyResult(ApplyStatus.SKIPPED, "posting closed", screenshot=shot, final_url=page.url)
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "Lever application form not found",
                               screenshot=shot, final_url=page.url)
        for sel in ("button.cc-allow", "button.cc-deny"):
            try:
                if page.locator(sel).first.is_visible():
                    page.locator(sel).first.click(timeout=2000)
                    break
            except Exception:
                pass
        scope = page.main_frame
        fields, answers, failed = fill_form(scope, job, ctx.profile, job_text=job_text)
        _pick_location(page, fields, answers)
        if not fields:
            return ApplyResult(ApplyStatus.NEEDS_REVIEW, "no fields found", screenshot=ctx.screenshot(job, "empty"),
                               final_url=page.url)
        return finish_application(job, ctx, scope, fields, answers, failed,
                                  submit_texts=["Submit application", "SUBMIT APPLICATION", "Submit"],
                                  success_url=r"/thanks")


ADAPTER = LeverAdapter()
