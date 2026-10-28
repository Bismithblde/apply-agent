from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

AtsName = Literal["greenhouse", "lever", "ashby", "workday", "smartrecruiters", "icims", "other"]

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source", "gh_src", "lever-source"}


def normalize_url(url: str) -> str:
    """Canonical form used for dedupe: lowercase host, no tracking params, no fragment or trailing slash."""
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING_PARAMS])
    path = p.path.rstrip("/") or "/"
    return urlunparse(((p.scheme or "https").lower(), p.netloc.lower(), path, "", query, ""))


def dedupe_key(company: str, title: str) -> str:
    norm = lambda s: " ".join(s.lower().replace("-", " ").split())
    return f"{norm(company)}|{norm(title)}"
FieldKind = Literal[
    "text", "textarea", "email", "tel", "url", "number", "date",
    "select", "combobox", "radio", "checkbox", "file",
]
AnswerSource = Literal["profile", "bank", "generated", "unknown"]


def detect_ats(url: str) -> AtsName:
    p = urlparse(url)
    host = p.netloc.lower()
    # Company career pages that embed a Greenhouse/Ashby form (e.g. stripe.com/jobs/...?gh_jid=123).
    if "gh_jid=" in p.query:
        return "greenhouse"
    if "ashby_jid=" in p.query:
        return "ashby"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host or "workday" in host:
        return "workday"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "icims.com" in host:
        return "icims"
    return "other"


@dataclass
class Job:
    id: str
    company: str
    title: str
    url: str
    locations: list[str]
    category: str
    date_posted: int
    source: str = "simplify"
    ats: AtsName = "other"

    def __post_init__(self) -> None:
        if self.ats == "other":
            self.ats = detect_ats(self.url)


@dataclass
class Field:
    """One fillable control. `key` is the value of the data-aa-key attribute stamped on the element."""
    key: str
    label: str
    kind: FieldKind
    required: bool
    options: list[str] = field(default_factory=list)
    name: str = ""
    placeholder: str = ""
    current_value: str = ""


@dataclass
class Answer:
    key: str
    # str for text/select/radio/combobox, list[str] for multi-checkbox, bool for single checkbox,
    # a file path for file inputs, None when there is no answer.
    value: str | list[str] | bool | None
    source: AnswerSource
    note: str = ""


class ApplyStatus(str, Enum):
    SUBMITTED = "submitted"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    SKIPPED = "skipped"
    DRY_RUN_OK = "dry_run_ok"


@dataclass
class ApplyResult:
    status: ApplyStatus
    reason: str
    screenshot: str | None = None
    final_url: str | None = None
    fields_total: int = 0
    unanswered_required: list[str] = field(default_factory=list)
    generated_answers: list[str] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)
