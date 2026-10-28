"""Community README lists (vanshb03, speedyapply). Tables may be markdown pipes or HTML <table>s."""
from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import httpx

from .. import db
from ..models import Job
from .common import USER_AGENT, client, explicit_internship_terms, explicit_season_terms, url_id

log = logging.getLogger(__name__)

CONTINUATION = ("↳", "&#8627;")
SHORTLINK_HOSTS = {"go.vansh.nyc", "cvrve.me", "redirect.cvrve.me"}
_TITLE_NOISE = re.compile(r"[🛂🇺🇸🔥🎓⭐️💰]|:(passport_control|us|fire|lock):")
_US_MARKER_RE = re.compile(r"(?<![a-z])(?:u\.s\.a?\.?|usa|us|united states)(?![a-z])", re.I)
_US_SCOPE_ROLE_RE = re.compile(r"\b(?:interns?|internships?|positions?|jobs?|roles?)\b", re.I)
_NONEXCLUSIVE_US_RE = re.compile(
    r"\b(?:non[\s-]?us|international|global|worldwide|canada|canadian|uk|u\.k\.|"
    r"united kingdom|india|indian|mexico|europe|asia|apac|emea|north america)\b",
    re.I,
)
_MIXED_SCOPE_RE = re.compile(r"(?:\b(?:and|or|with|plus)\b|[/&,;+])", re.I)

_COLUMNS = {
    "company": ("company",),
    "title": ("role", "position", "title", "job"),
    "location": ("location",),
    "link": ("application", "apply", "link", "posting"),
    "date": ("date", "age", "posted"),
}


# ---------- table parsing ----------

def _split_md_row(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return [c.strip().replace("\\|", "|") for c in cells]


def _markdown_tables_with_context(text: str) -> list[tuple[list[list[str]], list[str]]]:
    out, current, heading = [], [], {}

    def flush() -> None:
        nonlocal current
        if not current:
            return
        rows = [_split_md_row(line) for line in current]
        rows = [row for row in rows if not all(re.fullmatch(r":?-{2,}:?", cell) for cell in row if cell)]
        if len(rows) >= 2:
            out.append((rows, [heading[level] for level in sorted(heading)]))
        current = []

    for line in text.splitlines():
        match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            flush()
            level = len(match.group(1))
            heading[level] = match.group(2).strip()
            heading = {n: value for n, value in heading.items() if n <= level}
            continue
        if line.lstrip().startswith("|"):
            current.append(line)
        elif current:
            flush()
    flush()
    return out


def _md_tables(text: str) -> list[list[list[str]]]:
    return [rows for rows, _ in _markdown_tables_with_context(text)]


def _html_tables(text: str) -> list[list[list[str]]]:
    out = []
    for table in re.findall(r"<table\b.*?</table>", text, flags=re.S | re.I):
        rows = [re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", tr, flags=re.S | re.I)
                for tr in re.findall(r"<tr\b.*?</tr>", table, flags=re.S | re.I)]
        rows = [[c.strip() for c in r] for r in rows if r]
        if len(rows) >= 2:
            out.append(rows)
    return out


def strip_tags(cell: str) -> str:
    s = re.sub(r"<br\s*/?>|</br>", "\n", cell, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("__", "")
    return html.unescape(s).strip()


def _header_map(header: list[str]) -> dict[str, int]:
    names = [strip_tags(h).lower() for h in header]
    found: dict[str, int] = {}
    for key, words in _COLUMNS.items():
        for i, n in enumerate(names):
            if any(w in n for w in words) and i not in found.values():
                found[key] = i
                break
    return found


def _exclusive_us_context(headings: list[str]) -> bool:
    if any(_NONEXCLUSIVE_US_RE.search(heading) for heading in headings):
        return False
    return any(_US_MARKER_RE.search(heading) and _US_SCOPE_ROLE_RE.search(heading)
               and not _MIXED_SCOPE_RE.search(heading) for heading in headings)


def link_of(cell: str) -> str | None:
    """The apply URL in a cell, or None when the row is closed or has no link."""
    if "🔒" in cell or ":lock:" in cell or strip_tags(cell).lower() == "closed":
        return None
    urls = re.findall(r'href="([^"]+)"', cell) + re.findall(r"\]\((https?://[^)\s]+)\)", cell)
    if not urls:
        urls = re.findall(r"https?://[^\s<>\"')]+", cell)
    urls = [html.unescape(u) for u in urls if u.startswith("http")]
    preferred = [u for u in urls if "simplify.jobs" not in urlparse(u).netloc and "imgur" not in u]
    return (preferred or urls or [None])[0]


def parse_locations(cell: str) -> list[str]:
    cell = re.sub(r"<summary>.*?</summary>", "", cell, flags=re.S | re.I)
    parts = re.split(r"\n|;|•", strip_tags(cell))
    out = []
    for p in parts:
        p = re.sub(r"\s*\+\d+\s*$", "", p).strip(" ,")
        if p and not re.fullmatch(r"\d+ locations?", p, flags=re.I):
            out.append(p)
    return out


def parse_date(text: str, now: float | None = None) -> int:
    now = now or time.time()
    t = strip_tags(text).strip().lower()
    if m := re.fullmatch(r"(\d+)\s*(h|d|w|mo|m|y)", t):
        n, unit = int(m.group(1)), m.group(2)
        secs = {"h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "m": 30 * 86400, "y": 365 * 86400}[unit]
        return int(now - n * secs)
    for fmt in ("%b %d", "%B %d"):
        try:
            today = datetime.fromtimestamp(now)
            d = datetime.strptime(f"{t.title()} {today.year}", f"{fmt} %Y")
            if d.timestamp() > now + 86400:
                d = d.replace(year=today.year - 1)
            return int(d.timestamp())
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return int(datetime.strptime(strip_tags(text).strip(), fmt).timestamp())
        except ValueError:
            continue
    return 0


def parse_readme(text: str, source: str, now: float | None = None) -> list[Job]:
    """Rows from every listings table (one with company, title and link columns). Closed rows are skipped."""
    jobs: list[Job] = []
    title_match = re.search(r"(?m)^\s*#\s+(.+?)\s*$", text)
    document_context = [title_match.group(1).strip()] if title_match else []
    tables = _markdown_tables_with_context(text)
    tables.extend((rows, document_context) for rows in _html_tables(text))
    for rows, context in tables:
        cols = _header_map(rows[0])
        if not {"company", "title", "link"} <= cols.keys():
            continue
        context_text = "\n".join(context)
        context_terms = explicit_internship_terms(context_text, explicit_season_terms(context_text))
        context_term = context_terms if len(context_terms) == 1 else []
        us_context = _exclusive_us_context(context)
        company = ""
        for row in rows[1:]:
            if len(row) <= max(cols.values()):
                continue
            raw_company = strip_tags(row[cols["company"]])
            if raw_company in CONTINUATION or raw_company.startswith("↳"):
                raw_company = company
            company = raw_company
            url = link_of(row[cols["link"]])
            if not url or not company:
                continue
            title = re.sub(r"\s+", " ", _TITLE_NOISE.sub("", strip_tags(row[cols["title"]]))).strip()
            locs = parse_locations(row[cols["location"]]) if "location" in cols else []
            posted = parse_date(row[cols["date"]], now) if "date" in cols else 0
            job = Job(id=url_id(source, url), company=company, title=title, url=url,
                      locations=locs, category="", date_posted=posted, source=source)
            row_terms = explicit_season_terms(title)
            job.terms = row_terms or context_term
            job.location_context_us = us_context
            jobs.append(job)
    return jobs


# ---------- shortlinks ----------

def _resolve_one(c: httpx.Client, url: str) -> str:
    key = f"shortlink:{url}"
    if cached := db.kv_get(key):
        return cached
    try:
        r = c.head(url)
        if not r.is_redirect:
            r = c.get(url)
        target = r.headers.get("location")
    except httpx.HTTPError as e:
        log.debug("shortlink %s failed: %s", url, e)
        return url
    if target and target.startswith("http"):
        db.kv_set(key, target)
        return target
    return url


def resolve_shortlinks(jobs: list[Job], source: str) -> list[Job]:
    todo = [j for j in jobs if urlparse(j.url).netloc.lower() in SHORTLINK_HOSTS]
    if not todo:
        return jobs
    with httpx.Client(timeout=15, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as c:
        with ThreadPoolExecutor(max_workers=16) as pool:
            targets = list(pool.map(lambda j: _resolve_one(c, j.url), todo))
    resolved = 0
    for j, target in zip(todo, targets):
        if target != j.url:
            resolved += 1
            j.url = target
            j.id = url_id(source, target)
            j.ats = "other"
            j.__post_init__()
    if resolved < len(todo):
        log.warning("%s: resolved %d/%d shortlinks", source, resolved, len(todo))
    return jobs


# ---------- sources ----------

def _fetch_readme(config: dict, source: str) -> list[Job]:
    url = config["sources"][source]["url"]
    with client() as c:
        r = c.get(url)
        r.raise_for_status()
    jobs = parse_readme(r.text, source)
    if not jobs:
        raise RuntimeError(f"{source}: no listing rows parsed from {url} (format changed?)")
    return resolve_shortlinks(jobs, source)


def fetch_vanshb03(config: dict) -> list[Job]:
    return _fetch_readme(config, "vanshb03")


def fetch_speedyapply(config: dict) -> list[Job]:
    return _fetch_readme(config, "speedyapply")


def fetch(config: dict) -> list[Job]:
    jobs: list[Job] = []
    for name, fn in (("vanshb03", fetch_vanshb03), ("speedyapply", fetch_speedyapply)):
        if config["sources"].get(name, {}).get("enabled", True):
            jobs += fn(config)
    return jobs
