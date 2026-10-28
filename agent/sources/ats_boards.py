"""Public job-board APIs for companies listed in sources.ats_boards. Only internship postings are kept."""
from __future__ import annotations

import logging
import re
import json
from typing import Any, Callable

import httpx

from ..models import Job
from .common import client, explicit_internship_terms, explicit_season_terms, iso_to_ts

log = logging.getLogger(__name__)

SOURCE = "ats_boards"
INTERN_RE = re.compile(r"\bintern(s|ship|ships)?\b", re.I)
_LOC_SPLIT = re.compile(r"\s*(?:;|•|\|)\s*")


class BoardNotFound(Exception):
    pass


def _get(c: httpx.Client, url: str) -> Any:
    r = c.get(url)
    if r.status_code == 404:
        raise BoardNotFound(url)
    r.raise_for_status()
    return r.json()


def _company(slug: str) -> str:
    return slug.replace("-", " ").title()


def _split_locs(text: str | None) -> list[str]:
    return [p for p in _LOC_SPLIT.split(text or "") if p]


def _country_location(value: Any) -> str:
    country = str(value or "").strip()
    if country.upper() in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "United States"
    if country.upper() in {"CA", "CAN", "CANADA"}:
        return "Canada"
    return country


_TERM_FIELD_RE = re.compile(r"(?:\bterm\b|\bseason\b|internship[\s_-]*(?:term|season|program))", re.I)


def _term_metadata_values(metadata: Any) -> list[str]:
    values = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            label = value.get("name") or value.get("label") or value.get("key")
            if label and _TERM_FIELD_RE.search(str(label)):
                payload = value.get("value", value.get("values", value.get("field_value")))
                if payload is not None:
                    values.append(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False))
            for key, nested in value.items():
                if _TERM_FIELD_RE.search(str(key)) and nested is not None:
                    values.append(nested if isinstance(nested, str) else json.dumps(nested, ensure_ascii=False))
                elif isinstance(nested, (dict, list)):
                    collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(metadata)
    return values


def _job_terms(title: str, metadata: Any = None, description: str = "",
               wanted_terms: list[str] | None = None) -> list[str]:
    terms = explicit_season_terms(title, *_term_metadata_values(metadata))
    for term in explicit_internship_terms(description, wanted_terms or []):
        if term not in terms:
            terms.append(term)
    return terms


def greenhouse(c: httpx.Client, slug: str, wanted_terms: list[str] | None = None) -> list[Job]:
    data = _get(c, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false")
    jobs = []
    for x in data.get("jobs", []):
        title = (x.get("title") or "").strip()
        if not INTERN_RE.search(title):
            continue
        job = Job(
            id=f"greenhouse:{slug}:{x['id']}", company=x.get("company_name") or _company(slug), title=title,
            url=f"https://job-boards.greenhouse.io/{slug}/jobs/{x['id']}",
            locations=_split_locs((x.get("location") or {}).get("name")), category="",
            date_posted=iso_to_ts(x.get("first_published") or x.get("updated_at")),
            source=SOURCE, ats="greenhouse",
        )
        job.terms = _job_terms(title, x.get("metadata"), wanted_terms=wanted_terms)
        jobs.append(job)
    return jobs


def lever(c: httpx.Client, slug: str, wanted_terms: list[str] | None = None) -> list[Job]:
    data = _get(c, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for x in data:
        title = (x.get("text") or "").strip()
        if not INTERN_RE.search(title):
            continue
        cats = x.get("categories") or {}
        locs = cats.get("allLocations") or _split_locs(cats.get("location"))
        locs = [locs] if isinstance(locs, str) else list(locs)
        country = _country_location(x.get("country") or cats.get("country"))
        if country and country not in locs:
            locs.append(country)
        job = Job(
            id=f"lever:{slug}:{x['id']}", company=_company(slug), title=title,
            url=x.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{x['id']}",
            locations=list(locs), category="", date_posted=int((x.get("createdAt") or 0) / 1000),
            source=SOURCE, ats="lever",
        )
        job.terms = _job_terms(title, x.get("categories"), x.get("descriptionPlain") or "", wanted_terms)
        jobs.append(job)
    return jobs


def _ashby_loc(name: str | None, address: dict | None) -> str:
    name = (name or "").strip()
    country = ((address or {}).get("postalAddress") or {}).get("addressCountry") or ""
    if country and country.lower() not in name.lower():
        return f"{name}, {country}" if name else country
    return name


def ashby(c: httpx.Client, slug: str, wanted_terms: list[str] | None = None) -> list[Job]:
    data = _get(c, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for x in data.get("jobs", []):
        title = (x.get("title") or "").strip()
        if not INTERN_RE.search(title) or x.get("isListed") is False:
            continue
        locs = [_ashby_loc(x.get("location"), x.get("address"))]
        locs += [_ashby_loc(s.get("location"), s.get("address")) for s in x.get("secondaryLocations") or []]
        job = Job(
            id=f"ashby:{slug}:{x['id']}", company=_company(slug), title=title,
            url=x.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{x['id']}",
            locations=[l for l in locs if l], category="", date_posted=iso_to_ts(x.get("publishedAt")),
            source=SOURCE, ats="ashby",
        )
        job.terms = _job_terms(title, x, x.get("descriptionPlain") or "", wanted_terms)
        jobs.append(job)
    return jobs


BOARDS: dict[str, Callable[[httpx.Client, str, list[str] | None], list[Job]]] = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
}


def fetch(config: dict) -> list[Job]:
    cfg = config["sources"].get("ats_boards") or {}
    wanted_terms = config.get("filters", {}).get("terms") or []
    jobs: list[Job] = []
    failures = 0
    attempted = 0
    with client() as c:
        for ats, fn in BOARDS.items():
            for slug in cfg.get(ats) or []:
                attempted += 1
                try:
                    jobs += fn(c, slug, wanted_terms)
                except BoardNotFound:
                    log.warning("%s board %r not found (404); skipping", ats, slug)
                except (httpx.HTTPError, ValueError) as e:
                    failures += 1
                    log.warning("%s board %r failed: %s", ats, slug, e)
    if attempted and failures == attempted:
        raise RuntimeError(f"all {attempted} ATS boards failed")
    return jobs
