"""Adapter contract shared by every ATS module."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

from playwright.sync_api import BrowserContext, Page

from ..config import SCREENSHOT_DIR
from ..models import Answer, ApplyResult, ApplyStatus, Field, Job


@dataclass
class ApplyContext:
    page: Page
    browser: BrowserContext
    config: dict
    profile: dict
    # "full" | "hybrid" | "fill_only"
    submit_mode: str
    # dry_run: fill everything, never click the final submit, return DRY_RUN_OK.
    dry_run: bool = False
    run_id: int | None = None
    notes: list[str] = field(default_factory=list)
    scope_block_reason: str | None = field(default=None, init=False)

    def mark_submit_attempt(self, job_id: str) -> None:
        """Persist final-submit intent before the click, in case the worker dies afterwards."""
        if self.dry_run:
            raise RuntimeError("refusing final submit during a dry run")
        if not isinstance(self.submit_mode, str) or self.submit_mode not in ("full", "hybrid"):
            raise RuntimeError(f"refusing final submit in unsupported mode: {self.submit_mode!r}")
        if self.run_id is None:
            raise RuntimeError("refusing final submit without an active claimed run")
        from .. import db
        try:
            db.record_submit_intent(job_id, self.run_id, self.config)
        except db.ScopeEvidenceRejected as e:
            self.scope_block_reason = str(e)
            raise

    def screenshot(self, job: Job, tag: str) -> str | None:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{job.company}_{job.id}_{tag}")[:120]
        path = SCREENSHOT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None


def submit_decision(fields: list[Field], answers: list[Answer], mode: str, dry_run: bool) -> tuple[bool, str]:
    """Whether the adapter may click the final submit button.

    full:      every required field has an answer (profile, bank, or generated). Unknown blocks.
    hybrid:    every required field answered from profile/bank only.
    fill_only: never.
    dry_run:   never (adapter returns DRY_RUN_OK instead of NEEDS_REVIEW when it otherwise could submit).
    """
    if not isinstance(mode, str) or mode not in ("full", "hybrid", "fill_only"):
        return False, f"unsupported submit mode: {mode!r}"
    by_key = {a.key: a for a in answers}
    unknown = [f.label or f.key for f in fields if f.required and _missing(by_key.get(f.key))]
    generated = [f.label or f.key for f in fields if f.required and (a := by_key.get(f.key)) and a.source == "generated"]
    if unknown:
        return False, "unanswered required: " + "; ".join(unknown[:5])
    if mode == "fill_only":
        return False, "fill_only mode"
    if mode == "hybrid" and generated:
        return False, "hybrid mode, generated answers for: " + "; ".join(generated[:5])
    if dry_run:
        return False, "dry run"
    return True, "ok"


def _missing(a: Answer | None) -> bool:
    return a is None or a.source == "unknown" or a.value is None or a.value == "" or a.value == []


def blocked_result(fields: list[Field], answers: list[Answer], reason: str, dry_run: bool,
                   screenshot: str | None, final_url: str | None) -> ApplyResult:
    """Result for a fully filled form that we chose not to submit."""
    by_key = {a.key: a for a in answers}
    # DRY_RUN_OK means these inputs would be submittable in this mode.
    clean = (dry_run and bool(fields) and not [f for f in fields if f.required and _missing(by_key.get(f.key))]
             and "hybrid mode" not in reason and "fill_only mode" not in reason
             and "unsupported submit mode:" not in reason)
    return ApplyResult(
        status=ApplyStatus.DRY_RUN_OK if clean else ApplyStatus.NEEDS_REVIEW,
        reason=reason, screenshot=screenshot, final_url=final_url, fields_total=len(fields),
        unanswered_required=[f.label or f.key for f in fields if f.required and _missing(by_key.get(f.key))],
        generated_answers=[f.label or f.key for f in fields if (a := by_key.get(f.key)) and a.source == "generated"],
        answers=answers,
    )


class Adapter(Protocol):
    name: str

    def can_handle(self, job: Job) -> bool: ...

    def apply(self, job: Job, ctx: ApplyContext) -> ApplyResult:
        """Navigate to job.url, fill, (maybe) submit, and return a result. Must not raise for
        ordinary page problems; return ApplyResult(FAILED/NEEDS_REVIEW, reason) instead."""
        ...
