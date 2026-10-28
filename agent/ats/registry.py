from __future__ import annotations

import importlib
import logging

from ..models import Job
from .base import Adapter

log = logging.getLogger(__name__)

DEDICATED = ("greenhouse", "lever", "ashby", "workday")


def _load(name: str) -> Adapter | None:
    try:
        return importlib.import_module(f"agent.ats.{name}").ADAPTER
    except Exception as e:
        log.error("adapter %s failed to load: %s", name, e)
        return None


def adapter_for(job: Job, auto_ats: list[str]) -> Adapter | None:
    """Dedicated adapter when the ATS is enabled and the adapter accepts the job, else the generic one."""
    if job.ats in DEDICATED and job.ats in auto_ats:
        a = _load(job.ats)
        if a is not None and a.can_handle(job):
            return a
    if "generic" in auto_ats:
        return _load("generic")
    return None
