"""SimplifyJobs listings.json (~13 MB). Conditional GET with the ETag kept in kv and the body in data/."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .. import db
from ..config import DATA_DIR
from ..models import Job
from .common import client

log = logging.getLogger(__name__)

SOURCE = "simplify"
CACHE_FILE = DATA_DIR / "simplify_listings.json"
_ETAG_KEY, _LASTMOD_KEY = "simplify:etag", "simplify:last_modified"


def download(url: str) -> list[dict[str, Any]]:
    """Return the raw listings, re-downloading only when the server says the file changed."""
    headers = {}
    if CACHE_FILE.exists():
        if etag := db.kv_get(_ETAG_KEY):
            headers["If-None-Match"] = etag
        if lastmod := db.kv_get(_LASTMOD_KEY):
            headers["If-Modified-Since"] = lastmod
    try:
        with client(timeout=120) as c:
            r = c.get(url, headers=headers)
    except Exception as e:
        if CACHE_FILE.exists():
            log.warning("simplify download failed (%s); using cached copy", e)
            return json.loads(CACHE_FILE.read_text())
        raise
    if r.status_code == 304:
        log.info("simplify listings unchanged (304)")
        return json.loads(CACHE_FILE.read_text())
    r.raise_for_status()
    data = r.json()
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(CACHE_FILE)
    if etag := r.headers.get("etag"):
        db.kv_set(_ETAG_KEY, etag)
    if lastmod := r.headers.get("last-modified"):
        db.kv_set(_LASTMOD_KEY, lastmod)
    return data


def to_jobs(listings: list[dict[str, Any]], max_age_days: int = 0, now: float | None = None) -> list[Job]:
    cutoff = (now or time.time()) - max_age_days * 86400 if max_age_days else 0
    jobs = []
    for x in listings:
        if not (x.get("active") and x.get("is_visible")) or not x.get("url"):
            continue
        posted = int(x.get("date_posted") or 0)
        if posted < cutoff:
            continue
        job = Job(
            id=f"{SOURCE}:{x['id']}", company=(x.get("company_name") or "").strip(),
            title=(x.get("title") or "").strip(), url=x["url"].strip(),
            locations=list(x.get("locations") or []), category=x.get("category") or "",
            date_posted=posted, source=SOURCE,
        )
        job.terms = [t for t in (x.get("terms") or []) if t and t != "N/A"]
        jobs.append(job)
    return jobs


def fetch(config: dict) -> list[Job]:
    cfg = config["sources"]["simplify"]
    return to_jobs(download(cfg["url"]), int(cfg.get("max_age_days") or 0))
